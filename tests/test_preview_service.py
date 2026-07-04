from __future__ import annotations

import subprocess
from pathlib import Path

import app.preview_service as preview_service


def _settings(**overrides) -> preview_service.PreviewSettings:
    values = {
        "enabled": True,
        "timeout_sec": 12.0,
        "light_timeout_sec": 8.0,
        "ultra_light_timeout_sec": 5.0,
        "max_file_size_mb": 20.0,
        "max_render_views": 4,
        "max_render_views_high_complexity": 1,
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


def test_safe_preview_attempts_complex_parts_in_lightweight_mode(
    tmp_path,
    monkeypatch,
):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    captured = {}

    def fake_worker(source: Path, **kwargs):
        captured.update(kwargs)
        return {
            "image_png_base64": "image",
            "available": True,
            "mode": "light",
            "views": [{"name": "isometric", "image_png_base64": "image"}],
            "warnings": [],
        }

    monkeypatch.setattr(preview_service, "_run_worker", fake_worker)
    result = preview_service.generate_safe_step_preview(
        str(step_path),
        complexity_score="high",
        settings=_settings(),
    )

    assert result["available"] is True
    assert result["mode"] == "light"
    assert "High complexity part: using light preview mode" in result["warnings"]
    assert captured["mode"] == "light"
    assert captured["max_views"] == 1
    assert captured["timeout_sec"] == 8.0


def test_high_complexity_uses_one_lightweight_view(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    captured = {}

    def fake_worker(source: Path, **kwargs):
        captured.update(kwargs)
        return {
            "image_png_base64": "image",
            "available": True,
            "mode": "light",
            "views": [{"name": "isometric", "image_png_base64": "image"}],
            "warnings": [],
        }

    monkeypatch.setattr(preview_service, "_run_worker", fake_worker)
    result = preview_service.generate_safe_step_preview(
        str(step_path),
        complexity_score="high",
        settings=_settings(max_render_views_high_complexity=1),
    )

    assert result["available"] is True
    assert captured["max_views"] == 1
    assert captured["mode"] == "light"


def test_simple_part_falls_back_from_full_to_light(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    attempts = []

    def fake_worker(source: Path, **kwargs):
        attempts.append(kwargs)
        if kwargs["mode"] == "full":
            return preview_service.unavailable_preview("renderer timed out")
        return {
            "image_png_base64": "image",
            "available": True,
            "mode": "light",
            "views": [{"name": "isometric", "image_png_base64": "image"}],
            "warnings": [],
        }

    monkeypatch.setattr(preview_service, "_run_worker", fake_worker)
    result = preview_service.generate_safe_step_preview(
        str(step_path),
        complexity_score="medium",
        settings=_settings(),
    )

    assert result["available"] is True
    assert result["mode"] == "light"
    assert [attempt["mode"] for attempt in attempts] == ["full", "light"]
    assert "Full preview timed out or failed, light preview used" in result["warnings"]


def test_preview_falls_back_to_ultra_light(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    attempts = []

    def fake_worker(source: Path, **kwargs):
        attempts.append(kwargs)
        if kwargs["mode"] != "ultra_light":
            return preview_service.unavailable_preview(f"{kwargs['mode']} failed")
        return {
            "image_png_base64": "image",
            "available": True,
            "mode": "ultra_light",
            "views": [{"name": "isometric", "image_png_base64": "image"}],
            "warnings": [],
        }

    monkeypatch.setattr(preview_service, "_run_worker", fake_worker)
    result = preview_service.generate_safe_step_preview(
        str(step_path),
        complexity_score="medium",
        settings=_settings(),
    )

    assert result["available"] is True
    assert result["mode"] == "ultra_light"
    assert [attempt["mode"] for attempt in attempts] == [
        "full",
        "light",
        "ultra_light",
    ]
    assert (
        "Full/light preview timed out or failed, ultra-light preview used"
        in result["warnings"]
    )


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
        mode="light",
        timeout_sec=0.1,
        max_output_mb=1.0,
    )

    assert result["available"] is False
    assert result["views"] == []
    assert "timed out" in result["warnings"][0]
