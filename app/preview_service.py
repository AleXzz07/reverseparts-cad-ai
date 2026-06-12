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


def unavailable_preview(reason: str) -> dict[str, Any]:
    return {
        "image_png_base64": None,
        "available": False,
        "views": [],
        "warnings": [f"Preview generation skipped or failed: {reason}"],
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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class PreviewSettings:
    enabled: bool
    timeout_sec: float
    max_file_size_mb: float
    max_render_views: int
    max_render_views_high_complexity: int
    max_output_mb: float

    @classmethod
    def from_env(cls) -> "PreviewSettings":
        return cls(
            enabled=_env_bool("PREVIEW_ENABLED", True),
            timeout_sec=max(1.0, _env_float("PREVIEW_TIMEOUT_SEC", 15.0)),
            max_file_size_mb=max(
                0.1,
                _env_float("PREVIEW_MAX_FILE_SIZE_MB", 20.0),
            ),
            max_render_views=max(
                1,
                min(_env_int("PREVIEW_MAX_RENDER_VIEWS", 4), 4),
            ),
            max_render_views_high_complexity=max(
                1,
                min(
                    _env_int(
                        "PREVIEW_MAX_RENDER_VIEWS_HIGH_COMPLEXITY",
                        1,
                    ),
                    4,
                ),
            ),
            max_output_mb=max(
                1.0,
                _env_float("PREVIEW_MAX_OUTPUT_MB", 25.0),
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


def _run_worker(
    step_path: Path,
    *,
    max_views: int,
    lightweight: bool,
    timeout_sec: float,
    max_output_mb: float,
) -> dict[str, Any]:
    output_file = tempfile.NamedTemporaryFile(
        suffix=".json",
        delete=False,
    )
    output_path = Path(output_file.name)
    output_file.close()
    environment = os.environ.copy()
    if lightweight:
        environment.update(
            {
                "PREVIEW_WIDTH_PX": "1000",
                "PREVIEW_HEIGHT_PX": "750",
                "PREVIEW_RENDER_SCALE": "1",
            }
        )

    command = [
        sys.executable,
        "-m",
        "app.preview_worker",
        str(step_path),
        str(output_path),
        "--max-views",
        str(max_views),
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
            _, stderr = process.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            _stop_worker(process)
            return unavailable_preview(
                f"renderer timed out after {timeout_sec:g} seconds."
            )
        if process.returncode != 0:
            detail = (stderr or "").strip().splitlines()
            message = detail[-1] if detail else f"worker exited with code {process.returncode}."
            return unavailable_preview(message)
        if not output_path.is_file():
            return unavailable_preview("renderer produced no output.")
        if output_path.stat().st_size > max_output_mb * 1024 * 1024:
            return unavailable_preview("renderer output exceeded the configured size limit.")
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return unavailable_preview("renderer returned an invalid payload.")
        return payload
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return unavailable_preview(str(exc))
    finally:
        output_path.unlink(missing_ok=True)


def generate_safe_step_preview(
    step_path: str,
    *,
    complexity_score: str = "unknown",
    settings: PreviewSettings | None = None,
) -> dict[str, Any]:
    source = Path(step_path)
    active_settings = settings or PreviewSettings.from_env()
    if not active_settings.enabled:
        return unavailable_preview("preview is disabled by PREVIEW_ENABLED.")
    if not source.is_file():
        return unavailable_preview("STEP file does not exist.")

    file_size_mb = source.stat().st_size / (1024 * 1024)
    if file_size_mb > active_settings.max_file_size_mb:
        return unavailable_preview(
            f"file size {file_size_mb:.1f} MB exceeds "
            f"PREVIEW_MAX_FILE_SIZE_MB={active_settings.max_file_size_mb:g}."
        )

    normalized_complexity = str(complexity_score).strip().lower()
    lightweight = normalized_complexity == "high"
    max_views = (
        active_settings.max_render_views_high_complexity
        if lightweight
        else active_settings.max_render_views
    )
    timeout_sec = (
        min(active_settings.timeout_sec, 12.0)
        if lightweight
        else active_settings.timeout_sec
    )
    return _run_worker(
        source,
        max_views=max_views,
        lightweight=lightweight,
        timeout_sec=timeout_sec,
        max_output_mb=active_settings.max_output_mb,
    )
