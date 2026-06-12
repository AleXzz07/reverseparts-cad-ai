from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
            enabled=_env_bool("VIEWER_MODEL_ENABLED", False),
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

    output_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    output_path = Path(output_file.name)
    output_file.close()
    environment = os.environ.copy()
    if str(complexity_score).lower() == "high":
        environment.setdefault("VIEWER_MODEL_MAX_TRIANGLES", "50000")
        environment.setdefault("VIEWER_MODEL_TESSELLATION_RATIO", "300")

    command = [
        sys.executable,
        "-m",
        "app.model_worker",
        str(source),
        str(output_path),
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
        process = subprocess.Popen(command, **popen_kwargs)
        try:
            _, stderr = process.communicate(timeout=active_settings.timeout_sec)
        except subprocess.TimeoutExpired:
            _stop_worker(process)
            return unavailable_viewer_model(
                f"export timed out after {active_settings.timeout_sec:g} seconds."
            )
        if process.returncode != 0:
            details = (stderr or "").strip().splitlines()
            message = details[-1] if details else (
                f"worker exited with code {process.returncode}."
            )
            return unavailable_viewer_model(message)
        if not output_path.is_file():
            return unavailable_viewer_model("exporter produced no output.")
        if output_path.stat().st_size > active_settings.max_output_mb * 1024 * 1024:
            return unavailable_viewer_model(
                "exporter output exceeded the configured size limit."
            )
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return unavailable_viewer_model("exporter returned an invalid payload.")
        return payload
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return unavailable_viewer_model(str(exc))
    finally:
        output_path.unlink(missing_ok=True)
