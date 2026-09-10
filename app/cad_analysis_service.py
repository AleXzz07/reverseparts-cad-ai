from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schemas import CadAnalysisResponse


logger = logging.getLogger(__name__)


class CadAnalysisServiceError(RuntimeError):
    code = "cad_analysis_error"


class CadAnalysisTimeout(CadAnalysisServiceError):
    code = "cad_analysis_timeout"


class CadAnalysisBusy(CadAnalysisServiceError):
    code = "cad_analysis_busy"


class CadAnalysisWorkerCrash(CadAnalysisServiceError):
    code = "cad_analysis_worker_crash"


class CadAnalysisInvalidOutput(CadAnalysisServiceError):
    code = "cad_analysis_invalid_output"


class CadAnalysisWorkerError(CadAnalysisServiceError):
    code = "cad_analysis_worker_error"

    def __init__(self, message: str, *, worker_error_type: str = "technical_error"):
        super().__init__(message)
        self.worker_error_type = worker_error_type


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class CadAnalysisSettings:
    timeout_sec: float
    queue_timeout_sec: float
    max_concurrency: int
    max_output_mb: float
    diagnostic_timeout_sec: float

    @classmethod
    def from_env(cls) -> "CadAnalysisSettings":
        return cls(
            timeout_sec=max(1.0, _env_float("CAD_ANALYSIS_TIMEOUT_SEC", 300.0)),
            queue_timeout_sec=max(
                0.0,
                _env_float("CAD_ANALYSIS_QUEUE_TIMEOUT_SEC", 30.0),
            ),
            max_concurrency=max(
                1,
                _env_int("CAD_ANALYSIS_MAX_CONCURRENCY", 1),
            ),
            max_output_mb=max(
                1.0,
                _env_float("CAD_ANALYSIS_MAX_OUTPUT_MB", 50.0),
            ),
            diagnostic_timeout_sec=max(
                1.0,
                _env_float("CAD_DIAGNOSTIC_TIMEOUT_SEC", 20.0),
            ),
        )


_CONCURRENCY_CONDITION = threading.Condition()
_ACTIVE_ANALYSES = 0
_DIAGNOSTIC_LOCK = threading.Lock()
_FREECAD_DIAGNOSTIC: dict[str, Any] = {
    "status": "unknown",
    "error": None,
}


def get_cached_freecad_diagnostic() -> dict[str, Any]:
    with _DIAGNOSTIC_LOCK:
        return dict(_FREECAD_DIAGNOSTIC)


def _set_freecad_diagnostic(status: str, error: str | None = None) -> None:
    with _DIAGNOSTIC_LOCK:
        _FREECAD_DIAGNOSTIC["status"] = status
        _FREECAD_DIAGNOSTIC["error"] = error


def _acquire_analysis_slot(settings: CadAnalysisSettings) -> bool:
    global _ACTIVE_ANALYSES
    deadline = time.monotonic() + settings.queue_timeout_sec
    with _CONCURRENCY_CONDITION:
        while _ACTIVE_ANALYSES >= settings.max_concurrency:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _CONCURRENCY_CONDITION.wait(timeout=remaining)
        _ACTIVE_ANALYSES += 1
        return True


def _release_analysis_slot() -> None:
    global _ACTIVE_ANALYSES
    with _CONCURRENCY_CONDITION:
        _ACTIVE_ANALYSES = max(0, _ACTIVE_ANALYSES - 1)
        _CONCURRENCY_CONDITION.notify_all()


def _stop_worker(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": os.environ.copy(),
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return kwargs


def _last_stderr_line(stderr: str | None) -> str | None:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else None


def _read_payload(output_path: Path, max_output_mb: float) -> dict[str, Any]:
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise CadAnalysisInvalidOutput("CAD worker produced no output.")
    if output_path.stat().st_size > max_output_mb * 1024 * 1024:
        raise CadAnalysisInvalidOutput(
            "CAD worker output exceeded CAD_ANALYSIS_MAX_OUTPUT_MB."
        )
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CadAnalysisInvalidOutput("CAD worker returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise CadAnalysisInvalidOutput("CAD worker returned an invalid payload.")
    return payload


def run_isolated_cad_analysis(
    *,
    file_bytes: bytes,
    source_file: str,
    material: str | None = None,
    density_g_cm3: float | None = None,
    declared_thickness_mm: float | None = None,
    quantity: int = 1,
    k_factor: float | None = None,
    settings: CadAnalysisSettings | None = None,
) -> CadAnalysisResponse:
    active_settings = settings or CadAnalysisSettings.from_env()
    if not _acquire_analysis_slot(active_settings):
        raise CadAnalysisBusy(
            "Another CAD analysis is already running; retry after it completes."
        )

    try:
        with tempfile.TemporaryDirectory(prefix="reverseparts-cad-") as temp_dir:
            started_at = time.monotonic()
            work_dir = Path(temp_dir)
            step_path = work_dir / "input.step"
            request_path = work_dir / "request.json"
            output_path = work_dir / "output.json"
            step_path.write_bytes(file_bytes)
            request_path.write_text(
                json.dumps(
                    {
                        "step_path": str(step_path),
                        "source_file": source_file,
                        "material": material,
                        "density_g_cm3": density_g_cm3,
                        "declared_thickness_mm": declared_thickness_mm,
                        "quantity": quantity,
                        "k_factor": k_factor,
                    }
                ),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                "-m",
                "app.cad_analysis_worker",
                str(request_path),
                str(output_path),
            ]
            try:
                process = subprocess.Popen(command, **_popen_kwargs())
            except OSError as exc:
                _set_freecad_diagnostic("unknown", str(exc))
                raise CadAnalysisWorkerCrash(
                    f"CAD worker could not start: {exc}"
                ) from exc
            logger.info(
                "CAD analysis worker started: pid=%s timeout_sec=%s",
                process.pid,
                active_settings.timeout_sec,
            )
            try:
                _, stderr = process.communicate(timeout=active_settings.timeout_sec)
            except subprocess.TimeoutExpired as exc:
                _stop_worker(process)
                _set_freecad_diagnostic("unknown", "CAD analysis worker timed out.")
                logger.warning(
                    "CAD analysis worker timed out: pid=%s elapsed_sec=%.3f",
                    process.pid,
                    time.monotonic() - started_at,
                )
                raise CadAnalysisTimeout(
                    "CAD analysis timed out after "
                    f"{active_settings.timeout_sec:g} seconds."
                ) from exc
            except (OSError, subprocess.SubprocessError) as exc:
                _stop_worker(process)
                _set_freecad_diagnostic("unknown", str(exc))
                raise CadAnalysisWorkerCrash(
                    f"CAD worker communication failed: {exc}"
                ) from exc

            if process.returncode != 0:
                detail = _last_stderr_line(stderr)
                message = f"CAD worker exited with code {process.returncode}."
                if detail:
                    message = f"{message} {detail}"
                logger.error("%s", message)
                _set_freecad_diagnostic("unknown", message)
                raise CadAnalysisWorkerCrash(message)

            try:
                payload = _read_payload(output_path, active_settings.max_output_mb)
            except CadAnalysisInvalidOutput as exc:
                _set_freecad_diagnostic("unknown", str(exc))
                raise
            worker_status = payload.get("status")
            if worker_status == "error":
                error_type = str(payload.get("error_type") or "technical_error")
                message = str(payload.get("message") or "CAD worker reported an error.")
                if error_type == "freecad_unavailable":
                    _set_freecad_diagnostic("unavailable", message)
                else:
                    _set_freecad_diagnostic("available", None)
                raise CadAnalysisWorkerError(
                    message,
                    worker_error_type=error_type,
                )
            if worker_status != "ok" or not isinstance(payload.get("analysis"), dict):
                _set_freecad_diagnostic(
                    "unknown",
                    "CAD worker returned an invalid result envelope.",
                )
                raise CadAnalysisInvalidOutput(
                    "CAD worker returned an invalid result envelope."
                )
            try:
                result = CadAnalysisResponse.model_validate(payload["analysis"])
            except Exception as exc:
                _set_freecad_diagnostic(
                    "unknown",
                    "CAD worker result does not match the analysis schema.",
                )
                raise CadAnalysisInvalidOutput(
                    "CAD worker result does not match the analysis schema."
                ) from exc
            _set_freecad_diagnostic("available", None)
            logger.info(
                "CAD analysis worker completed: pid=%s elapsed_sec=%.3f",
                process.pid,
                time.monotonic() - started_at,
            )
            return result
    finally:
        _release_analysis_slot()


def probe_freecad_status(
    settings: CadAnalysisSettings | None = None,
) -> dict[str, Any]:
    active_settings = settings or CadAnalysisSettings.from_env()
    if not _acquire_analysis_slot(active_settings):
        return get_cached_freecad_diagnostic()
    try:
        with tempfile.TemporaryDirectory(prefix="reverseparts-cad-probe-") as temp_dir:
            output_path = Path(temp_dir) / "probe.json"
            command = [
                sys.executable,
                "-m",
                "app.cad_analysis_worker",
                "--probe",
                str(output_path),
            ]
            try:
                process = subprocess.Popen(command, **_popen_kwargs())
                try:
                    _, stderr = process.communicate(
                        timeout=active_settings.diagnostic_timeout_sec
                    )
                except subprocess.TimeoutExpired:
                    _stop_worker(process)
                    _set_freecad_diagnostic("unknown", "FreeCAD diagnostic timed out.")
                    return get_cached_freecad_diagnostic()
                if process.returncode != 0:
                    detail = _last_stderr_line(stderr)
                    _set_freecad_diagnostic(
                        "unknown",
                        detail or f"Diagnostic worker exited with code {process.returncode}.",
                    )
                    return get_cached_freecad_diagnostic()
                payload = _read_payload(output_path, 1.0)
                status = str(payload.get("status") or "unknown")
                if status not in {"available", "unavailable", "unknown"}:
                    status = "unknown"
                _set_freecad_diagnostic(
                    status,
                    payload.get("error"),
                )
            except (OSError, CadAnalysisInvalidOutput) as exc:
                _set_freecad_diagnostic("unknown", str(exc))
        return get_cached_freecad_diagnostic()
    finally:
        _release_analysis_slot()
