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
import uuid
from dataclasses import dataclass, field
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


class CadAnalysisSuperseded(CadAnalysisServiceError):
    code = "cad_analysis_superseded"

    def __init__(self, analysis_id: str):
        super().__init__("Analysis superseded by a newer request.")
        self.analysis_id = analysis_id


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
    cancellation_grace_sec: float

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
            cancellation_grace_sec=max(
                0.1,
                _env_float("CAD_ANALYSIS_CANCEL_GRACE_SEC", 2.0),
            ),
        )


@dataclass
class ActiveCadJob:
    session_id: str | None
    analysis_id: str
    process: subprocess.Popen[str] | None = None
    superseded: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


_JOB_CONDITION = threading.Condition()
_CURRENT_JOB: ActiveCadJob | None = None
_LATEST_ANALYSIS_BY_SESSION: dict[str, str] = {}
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


def _normalise_session_id(session_id: str | None, analysis_id: str) -> str:
    value = str(session_id or "").strip()
    return value[:128] if value else f"anonymous:{analysis_id}"


def _finish_job(job: ActiveCadJob) -> None:
    global _CURRENT_JOB
    with _JOB_CONDITION:
        if _CURRENT_JOB is job:
            _CURRENT_JOB = None
        if _LATEST_ANALYSIS_BY_SESSION.get(job.session_id or "") == job.analysis_id:
            _LATEST_ANALYSIS_BY_SESSION.pop(job.session_id or "", None)
        job.finished.set()
        _JOB_CONDITION.notify_all()


def _job_is_current(job: ActiveCadJob) -> bool:
    with _JOB_CONDITION:
        return (
            _CURRENT_JOB is job
            and not job.superseded.is_set()
            and _LATEST_ANALYSIS_BY_SESSION.get(job.session_id or "")
            == job.analysis_id
        )


def _raise_if_superseded(job: ActiveCadJob) -> None:
    if not _job_is_current(job):
        raise CadAnalysisSuperseded(job.analysis_id)


def _set_job_process(job: ActiveCadJob, process: subprocess.Popen[str]) -> bool:
    with _JOB_CONDITION:
        job.process = process
        return (
            _CURRENT_JOB is job
            and not job.superseded.is_set()
            and _LATEST_ANALYSIS_BY_SESSION.get(job.session_id or "")
            == job.analysis_id
        )


def _update_diagnostic_for_current_job(
    job: ActiveCadJob,
    status: str,
    error: str | None = None,
) -> None:
    with _JOB_CONDITION:
        if (
            _CURRENT_JOB is job
            and not job.superseded.is_set()
            and _LATEST_ANALYSIS_BY_SESSION.get(job.session_id or "")
            == job.analysis_id
        ):
            _set_freecad_diagnostic(status, error)


def _commit_successful_job(job: ActiveCadJob) -> None:
    global _CURRENT_JOB
    with _JOB_CONDITION:
        if (
            _CURRENT_JOB is not job
            or job.superseded.is_set()
            or _LATEST_ANALYSIS_BY_SESSION.get(job.session_id or "")
            != job.analysis_id
        ):
            raise CadAnalysisSuperseded(job.analysis_id)
        _set_freecad_diagnostic("available", None)
        _CURRENT_JOB = None
        _LATEST_ANALYSIS_BY_SESSION.pop(job.session_id or "", None)
        job.finished.set()
        _JOB_CONDITION.notify_all()


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


def _terminate_worker_gracefully(
    process: subprocess.Popen[str],
    grace_sec: float,
) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    _stop_worker(process)


def _signal_worker(process: subprocess.Popen[str], *, force: bool) -> None:
    try:
        if os.name == "posix":
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        elif force:
            process.kill()
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass


def _cancel_active_job(job: ActiveCadJob, settings: CadAnalysisSettings) -> None:
    process = job.process
    if process is not None:
        _signal_worker(process, force=False)
    if job.finished.wait(timeout=settings.cancellation_grace_sec):
        return
    if process is not None:
        _signal_worker(process, force=True)
    # The owner thread reaps the child and removes its temporary directory.
    job.finished.wait(timeout=min(5.0, settings.queue_timeout_sec))


def _claim_analysis_job(
    *,
    session_id: str | None,
    analysis_id: str,
    settings: CadAnalysisSettings,
) -> ActiveCadJob:
    global _CURRENT_JOB
    owned_session_id = _normalise_session_id(session_id, analysis_id)
    job = ActiveCadJob(
        session_id=owned_session_id,
        analysis_id=analysis_id,
    )
    job_to_cancel: ActiveCadJob | None = None

    with _JOB_CONDITION:
        current = _CURRENT_JOB
        if current is None:
            _LATEST_ANALYSIS_BY_SESSION[owned_session_id] = analysis_id
            _CURRENT_JOB = job
            return job
        if current.session_id != owned_session_id:
            raise CadAnalysisBusy(
                "Another CAD analysis is already running; retry after it completes."
            )
        _LATEST_ANALYSIS_BY_SESSION[owned_session_id] = analysis_id
        current.superseded.set()
        job_to_cancel = current

    # Never wait for FreeCAD or process cleanup while holding the registry lock.
    if job_to_cancel is not None:
        _cancel_active_job(job_to_cancel, settings)

    deadline = time.monotonic() + settings.queue_timeout_sec
    with _JOB_CONDITION:
        while True:
            if _LATEST_ANALYSIS_BY_SESSION.get(owned_session_id) != analysis_id:
                raise CadAnalysisSuperseded(analysis_id)
            if _CURRENT_JOB is None:
                _CURRENT_JOB = job
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if _LATEST_ANALYSIS_BY_SESSION.get(owned_session_id) == analysis_id:
                    _LATEST_ANALYSIS_BY_SESSION.pop(owned_session_id, None)
                raise CadAnalysisBusy(
                    "Previous CAD analysis cancellation is still completing; retry shortly."
                )
            _JOB_CONDITION.wait(timeout=remaining)


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
    analysis_session_id: str | None = None,
    analysis_id: str | None = None,
    settings: CadAnalysisSettings | None = None,
) -> CadAnalysisResponse:
    active_settings = settings or CadAnalysisSettings.from_env()
    request_analysis_id = str(analysis_id or uuid.uuid4())[:128]
    job = _claim_analysis_job(
        session_id=analysis_session_id,
        analysis_id=request_analysis_id,
        settings=active_settings,
    )

    try:
        _raise_if_superseded(job)
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
                _raise_if_superseded(job)
                _update_diagnostic_for_current_job(job, "unknown", str(exc))
                raise CadAnalysisWorkerCrash(
                    f"CAD worker could not start: {exc}"
                ) from exc
            if not _set_job_process(job, process):
                _terminate_worker_gracefully(
                    process,
                    active_settings.cancellation_grace_sec,
                )
                raise CadAnalysisSuperseded(job.analysis_id)
            logger.info(
                "CAD analysis worker started: analysis_id=%s pid=%s timeout_sec=%s",
                job.analysis_id,
                process.pid,
                active_settings.timeout_sec,
            )
            try:
                _, stderr = process.communicate(timeout=active_settings.timeout_sec)
            except subprocess.TimeoutExpired as exc:
                _stop_worker(process)
                _raise_if_superseded(job)
                _update_diagnostic_for_current_job(
                    job,
                    "unknown",
                    "CAD analysis worker timed out.",
                )
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
                _raise_if_superseded(job)
                _update_diagnostic_for_current_job(job, "unknown", str(exc))
                raise CadAnalysisWorkerCrash(
                    f"CAD worker communication failed: {exc}"
                ) from exc

            _raise_if_superseded(job)
            if process.returncode != 0:
                detail = _last_stderr_line(stderr)
                message = f"CAD worker exited with code {process.returncode}."
                if detail:
                    message = f"{message} {detail}"
                logger.error("%s", message)
                _update_diagnostic_for_current_job(job, "unknown", message)
                raise CadAnalysisWorkerCrash(message)

            try:
                payload = _read_payload(output_path, active_settings.max_output_mb)
            except CadAnalysisInvalidOutput as exc:
                _raise_if_superseded(job)
                _update_diagnostic_for_current_job(job, "unknown", str(exc))
                raise
            worker_status = payload.get("status")
            if worker_status == "error":
                error_type = str(payload.get("error_type") or "technical_error")
                message = str(payload.get("message") or "CAD worker reported an error.")
                if error_type == "freecad_unavailable":
                    _update_diagnostic_for_current_job(job, "unavailable", message)
                else:
                    _update_diagnostic_for_current_job(job, "available", None)
                raise CadAnalysisWorkerError(
                    message,
                    worker_error_type=error_type,
                )
            if worker_status != "ok" or not isinstance(payload.get("analysis"), dict):
                _update_diagnostic_for_current_job(
                    job,
                    "unknown",
                    "CAD worker returned an invalid result envelope.",
                )
                raise CadAnalysisInvalidOutput(
                    "CAD worker returned an invalid result envelope."
                )
            try:
                result = CadAnalysisResponse.model_validate(payload["analysis"])
            except Exception as exc:
                _update_diagnostic_for_current_job(
                    job,
                    "unknown",
                    "CAD worker result does not match the analysis schema.",
                )
                raise CadAnalysisInvalidOutput(
                    "CAD worker result does not match the analysis schema."
                ) from exc
            _raise_if_superseded(job)
        _commit_successful_job(job)
        logger.info(
            "CAD analysis worker completed: analysis_id=%s pid=%s elapsed_sec=%.3f",
            job.analysis_id,
            process.pid,
            time.monotonic() - started_at,
        )
        return result
    finally:
        _finish_job(job)


def probe_freecad_status(
    settings: CadAnalysisSettings | None = None,
) -> dict[str, Any]:
    active_settings = settings or CadAnalysisSettings.from_env()
    try:
        job = _claim_analysis_job(
            session_id=None,
            analysis_id=f"diagnostic:{uuid.uuid4()}",
            settings=active_settings,
        )
    except CadAnalysisBusy:
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
        _finish_job(job)
