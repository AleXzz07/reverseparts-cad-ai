from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("uvicorn.error")


def _sha256_file(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as step_file:
        while chunk := step_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _worker_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _process_peak_rss_mib(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmHWM:"):
                return round(int(line.split()[1]) / 1024, 2)
    except (OSError, ValueError, IndexError):
        pass
    return None


def unavailable_viewer_model(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "model_base64": None,
        "format": None,
        "warnings": [f"3D model export skipped or failed: {reason}"],
    }


def deferred_viewer_model(complexity_score: str = "unknown") -> dict[str, Any]:
    settings = ViewerModelSettings.from_env()
    warnings = []
    if not settings.enabled:
        warnings.append("3D viewer model generation disabled")
    else:
        warnings.append("3D viewer model available on request")
    if str(complexity_score).strip().lower() == "high":
        warnings.append(
            "Modello complesso: vista 3D caricabile solo su richiesta"
        )
    return {
        "available": False,
        "model_base64": None,
        "format": None,
        "warnings": warnings,
    }


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ViewerModelSettings:
    enabled: bool
    timeout_sec: float
    max_file_size_mb: float
    max_output_mb: float

    @classmethod
    def from_env(cls) -> "ViewerModelSettings":
        return cls(
            enabled=_env_bool("VIEWER_MODEL_ENABLED", True),
            timeout_sec=max(1.0, _env_float("VIEWER_MODEL_TIMEOUT_SEC", 20.0)),
            max_file_size_mb=max(
                0.1,
                _env_float("VIEWER_MODEL_MAX_FILE_SIZE_MB", 10.0),
            ),
            max_output_mb=max(
                1.0,
                _env_float("VIEWER_MODEL_MAX_OUTPUT_MB", 20.0),
            ),
        )


def _stop_worker(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def generate_safe_viewer_model(
    step_path: str,
    *,
    complexity_score: str = "unknown",
    settings: ViewerModelSettings | None = None,
) -> dict[str, Any]:
    source = Path(step_path)
    active_settings = settings or ViewerModelSettings.from_env()
    if not active_settings.enabled:
        return {
            "available": False,
            "model_base64": None,
            "format": None,
            "warnings": ["3D viewer model generation disabled"],
        }
    if not source.is_file():
        return unavailable_viewer_model("STEP file does not exist.")

    file_size_mb = source.stat().st_size / (1024 * 1024)
    if file_size_mb > active_settings.max_file_size_mb:
        return unavailable_viewer_model(
            f"file size {file_size_mb:.1f} MB exceeds "
            f"VIEWER_MODEL_MAX_FILE_SIZE_MB={active_settings.max_file_size_mb:g}."
        )

    started = time.monotonic()
    input_sha256 = _sha256_file(source)
    output_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    output_path = Path(output_file.name)
    output_file.close()
    status_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    status_path = Path(status_file.name)
    status_file.close()
    process: subprocess.Popen[str] | None = None
    outcome = "error"
    terminated = False
    peak_at_timeout = None
    spawn_sec = None
    worker_wait_sec = None
    status = {}
    environment = os.environ.copy()
    if str(complexity_score).lower() == "high":
        environment.setdefault("VIEWER_MODEL_MAX_TRIANGLES", "50000")
        environment.setdefault("VIEWER_MODEL_TESSELLATION_RATIO", "300")
        environment.setdefault("VIEWER_MODEL_CURVED_FACE_REFINEMENT", "0.8")

    command = [
        sys.executable,
        "-m",
        "app.model_worker",
        str(source),
        str(output_path),
        "--status-path",
        str(status_path),
    ]
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "env": environment,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        spawn_start = time.monotonic()
        process = subprocess.Popen(command, **popen_kwargs)
        spawn_sec = round(time.monotonic() - spawn_start, 4)
        wait_start = time.monotonic()
        try:
            _, stderr = process.communicate(timeout=active_settings.timeout_sec)
        except subprocess.TimeoutExpired:
            worker_wait_sec = round(time.monotonic() - wait_start, 4)
            status = _worker_status(status_path)
            peak_at_timeout = _process_peak_rss_mib(process.pid)
            _stop_worker(process)
            terminated = True
            outcome = "timeout"
            return unavailable_viewer_model(
                f"export timed out after {active_settings.timeout_sec:g} seconds."
            )
        worker_wait_sec = round(time.monotonic() - wait_start, 4)
        status = _worker_status(status_path)
        if process.returncode != 0:
            outcome = "worker_exit"
            details = (stderr or "").strip().splitlines()
            message = details[-1] if details else (
                f"worker exited with code {process.returncode}."
            )
            return unavailable_viewer_model(message)
        if not output_path.is_file():
            outcome = "missing_output"
            return unavailable_viewer_model("exporter produced no output.")
        if output_path.stat().st_size > active_settings.max_output_mb * 1024 * 1024:
            outcome = "oversize_output"
            return unavailable_viewer_model(
                "exporter output exceeded the configured size limit."
            )
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            outcome = "invalid_output"
            return unavailable_viewer_model("exporter returned an invalid payload.")
        outcome = "completed" if payload.get("available") else "export_unavailable"
        return payload
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        outcome = "service_error"
        return unavailable_viewer_model(str(exc))
    finally:
        if not status:
            status = _worker_status(status_path)
        allowed = ("phase", "worker_elapsed_sec", "worker_peak_rss_mib",
                   "freecad_import_sec", "step_load_sec", "tessellation_sec", "occ_normals_sec",
                   "brep_edges_sec", "glb_assembly_sec", "base64_sec",
                   "face_count", "edge_count", "diagonal_mm", "attempt",
                   "face_index",
                   "deflection_mm", "vertex_count", "triangle_count",
                   "edge_segment_count", "glb_size_bytes", "result_json_bytes",
                   "result_serialization_sec",
                   "available", "error_type")
        safe_status = {key: status[key] for key in allowed if key in status}
        LOGGER.info("viewer_model_diagnostic %s", json.dumps({
            "outcome": outcome,
            "timeout_sec": active_settings.timeout_sec,
            "complexity_score": str(complexity_score).lower() if str(complexity_score).lower() in
                                {"low", "medium", "high"} else "unknown",
            "input_size_bytes": source.stat().st_size,
            "input_sha256": input_sha256,
            "max_triangles": environment.get("VIEWER_MODEL_MAX_TRIANGLES", "120000"),
            "tessellation_ratio": environment.get("VIEWER_MODEL_TESSELLATION_RATIO", "650"),
            "curved_face_refinement": environment.get("VIEWER_MODEL_CURVED_FACE_REFINEMENT", "0.65"),
            "spawn_sec": spawn_sec,
            "worker_wait_sec": worker_wait_sec,
            "service_total_sec": round(time.monotonic() - started, 4),
            "worker_exit_code": process.returncode if process is not None else None,
            "worker_terminated": terminated,
            "peak_worker_rss_mib": max((value for value in
                (peak_at_timeout, safe_status.get("worker_peak_rss_mib"))
                if isinstance(value, (int, float))), default=None),
            "worker": safe_status,
        }, separators=(",", ":")))
        output_path.unlink(missing_ok=True)
        status_path.unlink(missing_ok=True)
        status_path.with_suffix(".partial").unlink(missing_ok=True)
