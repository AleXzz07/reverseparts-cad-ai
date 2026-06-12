from __future__ import annotations

import subprocess
from pathlib import Path

import app.preview_service as preview_service


def _settings(**overrides) -> preview_service.PreviewSettings:
    values = {
        "enabled": True,
        "timeout_sec": 20.0,
        "max_file_size_mb": 20.0,
        "max_complexity_score": "medium",
        "max_render_views": 4,
        "max_output_mb": 25.0,
    }
    values.update(overrides)
    return preview_service.PreviewSettings(**values)


def test_safe_preview_can_be_disabled(tmp_path):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")

    result = preview_service.generate_safe_step_preview(
        str(step_path),
        settings=_settings(enabled=False),
    )

    assert result["available"] is False
    assert result["views"] == []
    assert "PREVIEW_ENABLED" in result["warnings"][0]


def test_safe_preview_skips_complex_parts_by_default(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    worker_called = False

    def fake_worker(*args, **kwargs):
        nonlocal worker_called
        worker_called = True
        return {}

    monkeypatch.setattr(preview_service, "_run_worker", fake_worker)
    result = preview_service.generate_safe_step_preview(
        str(step_path),
        complexity_score="high",
        settings=_settings(),
    )

    assert result["available"] is False
    assert worker_called is False
    assert "PREVIEW_MAX_COMPLEXITY_SCORE=medium" in result["warnings"][0]


def test_high_complexity_uses_one_lightweight_view(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    captured = {}

    def fake_worker(source: Path, **kwargs):
        captured.update(kwargs)
        return {
            "image_png_base64": "image",
            "available": True,
            "views": [{"name": "isometric", "image_png_base64": "image"}],
            "warnings": [],
        }

    monkeypatch.setattr(preview_service, "_run_worker", fake_worker)
    result = preview_service.generate_safe_step_preview(
        str(step_path),
        complexity_score="high",
        settings=_settings(
            max_complexity_score="high",
            max_render_views=4,
        ),
    )

    assert result["available"] is True
    assert captured["max_views"] == 1
    assert captured["lightweight"] is True


def test_worker_timeout_returns_controlled_fallback(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")

    class TimedOutWorker:
        pid = 123
        returncode = None

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("preview-worker", timeout)

        def poll(self):
            return None

    monkeypatch.setattr(
        preview_service.subprocess,
        "Popen",
        lambda *args, **kwargs: TimedOutWorker(),
    )
    monkeypatch.setattr(preview_service, "_stop_worker", lambda process: None)

    result = preview_service._run_worker(
        step_path,
        max_views=1,
        lightweight=True,
        timeout_sec=0.1,
        max_output_mb=1.0,
    )

    assert result["available"] is False
    assert result["views"] == []
    assert "timed out" in result["warnings"][0]
