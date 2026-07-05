from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
STANDARD_VIEW_ORDER = ("isometric", "top", "front", "right")


def unavailable_preview(reason: str) -> dict[str, Any]:
    return {
        "image_png_base64": None,
        "available": False,
        "mode": "failed",
        "partial": False,
        "views": [],
        "warnings": [f"Preview generation skipped or failed: {reason}"],
    }


def not_generated_preview() -> dict[str, Any]:
    return {
        "image_png_base64": None,
        "available": False,
        "mode": "not_generated",
        "partial": False,
        "views": [],
        "warnings": ["Preview not generated automatically. Use /generate-preview."],
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
    on_demand_only: bool
    timeout_sec: float
    light_timeout_sec: float
    ultra_light_timeout_sec: float
    high_complexity_timeout_sec: float
    max_file_size_mb: float
    max_render_views: int
    max_render_views_high_complexity: int
    max_output_mb: float

    @classmethod
    def from_env(cls) -> "PreviewSettings":
        return cls(
            enabled=_env_bool("PREVIEW_ENABLED", True),
            on_demand_only=_env_bool("PREVIEW_ON_DEMAND_ONLY", True),
            timeout_sec=max(1.0, _env_float("PREVIEW_TIMEOUT_SEC", 12.0)),
            light_timeout_sec=max(
                1.0,
                _env_float("PREVIEW_LIGHT_TIMEOUT_SEC", 8.0),
            ),
            ultra_light_timeout_sec=max(
                1.0,
                _env_float("PREVIEW_ULTRA_LIGHT_TIMEOUT_SEC", 5.0),
            ),
            high_complexity_timeout_sec=max(
                1.0,
                _env_float("PREVIEW_HIGH_COMPLEXITY_TIMEOUT_SEC", 30.0),
            ),
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
                        4,
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
    max_views: int | None = None,
    view_names: list[str] | None = None,
    mode: str,
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
    if mode == "full":
        environment.update(
            {
                "PREVIEW_WIDTH_PX": "1000",
                "PREVIEW_HEIGHT_PX": "750",
                "PREVIEW_RENDER_SCALE": "1",
                "PREVIEW_RENDER_MODE": os.getenv(
                    "PREVIEW_FULL_RENDER_MODE",
                    "light",
                ),
            }
        )
    elif mode == "light":
        environment.update(
            {
                "PREVIEW_WIDTH_PX": "1000",
                "PREVIEW_HEIGHT_PX": "750",
                "PREVIEW_RENDER_SCALE": "1",
                "PREVIEW_RENDER_MODE": "light",
            }
        )
    else:
        environment.update(
            {
                "PREVIEW_WIDTH_PX": "900",
                "PREVIEW_HEIGHT_PX": "650",
                "PREVIEW_RENDER_SCALE": "1",
                "PREVIEW_RENDER_MODE": "ultra_light",
            }
        )

    command = [
        sys.executable,
        "-m",
        "app.preview_worker",
        str(step_path),
        str(output_path),
    ]
    if view_names:
        command.extend(["--views", *view_names])
    else:
        command.extend(["--max-views", str(max_views or 1)])
    logger.info(
        "Preview worker start: mode=%s requested_views=%s timeout_sec=%s file=%s",
        mode,
        view_names or list(STANDARD_VIEW_ORDER[: max_views or 1]),
        timeout_sec,
        step_path.name,
    )
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
        stderr_lines = [
            line.strip()
            for line in (stderr or "").splitlines()
            if line.strip()
        ]
        for line in stderr_lines:
            logger.info("%s", line)
        if process.returncode != 0:
            message = (
                stderr_lines[-1]
                if stderr_lines
                else f"worker exited with code {process.returncode}."
            )
            return unavailable_preview(message)
        if not output_path.is_file():
            return unavailable_preview("renderer produced no output.")
        if output_path.stat().st_size > max_output_mb * 1024 * 1024:
            return unavailable_preview("renderer output exceeded the configured size limit.")
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return unavailable_preview("renderer returned an invalid payload.")
        generated_views = [
            view.get("name")
            for view in payload.get("views", [])
            if isinstance(view, dict)
        ]
        failed_views = [
            warning
            for warning in payload.get("warnings", [])
            if isinstance(warning, str)
            and warning.startswith("Preview view ")
        ]
        logger.info(
            "[preview] worker finished: mode=%s generated ok=%s failed=%s returned views=%s",
            mode,
            generated_views,
            failed_views,
            generated_views,
        )
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
    if normalized_complexity == "high":
        selected_views = STANDARD_VIEW_ORDER[
            : active_settings.max_render_views_high_complexity
        ]
        logger.info(
            "Preview selection: complexity_score=high per_view_mode=ultra_light requested_views=%s",
            list(selected_views),
        )
        views: list[dict[str, Any]] = []
        failed_warnings: list[str] = []
        for view_name in selected_views:
            timeout_sec = (
                active_settings.high_complexity_timeout_sec
                if view_name == "isometric"
                else max(
                    active_settings.ultra_light_timeout_sec,
                    min(active_settings.high_complexity_timeout_sec, 8.0),
                )
            )
            result = _run_worker(
                source,
                view_names=[view_name],
                mode="ultra_light",
                timeout_sec=timeout_sec,
                max_output_mb=active_settings.max_output_mb,
            )
            if result.get("available"):
                views.extend(result.get("views") or [])
            else:
                failed_warnings.extend(result.get("warnings") or [])
        if views:
            primary = next(
                (
                    view["image_png_base64"]
                    for view in views
                    if view.get("name") == "isometric"
                ),
                views[0]["image_png_base64"],
            )
            logger.info(
                "Preview high complexity finished: generated_views=%s failed_count=%s",
                [view.get("name") for view in views],
                len(failed_warnings),
            )
            return {
                "image_png_base64": primary,
                "available": True,
                "partial": len(views) < len(selected_views),
                "mode": "ultra_light",
                "views": views,
                "warnings": failed_warnings,
            }
        failed = unavailable_preview(
            "Preview generation failed after all high-complexity view attempts."
        )
        failed["warnings"] = [
            *failed_warnings,
            "Preview generation failed after all high-complexity view attempts",
        ]
        return failed
    else:
        attempts = [
            (
                "full",
                active_settings.max_render_views,
                active_settings.timeout_sec,
            ),
            (
                "light",
                active_settings.max_render_views,
                active_settings.light_timeout_sec,
            ),
            (
                "ultra_light",
                active_settings.max_render_views,
                active_settings.ultra_light_timeout_sec,
            ),
        ]
        leading_warnings = []

    logger.info(
        "Preview selection: complexity_score=%s attempts=%s",
        normalized_complexity or "unknown",
        [
            {"mode": mode, "max_views": max_views, "timeout_sec": timeout_sec}
            for mode, max_views, timeout_sec in attempts
        ],
    )

    failed_warnings: list[str] = []
    for mode, max_views, timeout_sec in attempts:
        result = _run_worker(
            source,
            max_views=max_views,
            mode=mode,
            timeout_sec=timeout_sec,
            max_output_mb=active_settings.max_output_mb,
        )
        result["mode"] = mode if result.get("available") else "failed"
        if result.get("available"):
            warnings = [
                *leading_warnings,
                *failed_warnings,
                *(result.get("warnings") or []),
            ]
            if failed_warnings and mode == "light":
                warnings.append("Full preview timed out or failed, light preview used")
            if failed_warnings and mode == "ultra_light":
                warnings.append(
                    "Full/light preview timed out or failed, ultra-light preview used"
                )
            result["warnings"] = warnings
            return result
        failed_warnings.extend(result.get("warnings") or [])

    failed = unavailable_preview("Preview generation failed after all fallback modes.")
    failed["mode"] = "failed"
    failed["warnings"] = [
        *leading_warnings,
        *failed_warnings,
        "Preview generation failed after all fallback modes",
    ]
    return failed
