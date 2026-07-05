from __future__ import annotations

import subprocess
from pathlib import Path

import app.preview_service as preview_service


def _settings(**overrides) -> preview_service.PreviewSettings:
    values = {
        "enabled": True,
        "on_demand_only": True,
        "timeout_sec": 12.0,
        "light_timeout_sec": 8.0,
        "ultra_light_timeout_sec": 5.0,
        "high_complexity_timeout_sec": 30.0,
        "max_file_size_mb": 20.0,
        "max_render_views": 4,
        "max_render_views_high_complexity": 4,
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


def test_safe_preview_attempts_complex_parts_in_ultra_light_mode(
    tmp_path,
    monkeypatch,
):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    captured = []

    def fake_worker(source: Path, **kwargs):
        captured.append(kwargs)
        view_name = kwargs["view_names"][0]
        return {
            "image_png_base64": "image",
            "available": True,
            "mode": "ultra_light",
            "views": [{"name": view_name, "image_png_base64": f"image-{view_name}"}],
            "warnings": [],
        }

    monkeypatch.setattr(preview_service, "_run_worker", fake_worker)
    result = preview_service.generate_safe_step_preview(
        str(step_path),
        complexity_score="high",
        settings=_settings(),
    )

    assert result["available"] is True
    assert result["mode"] == "ultra_light"
    assert result["partial"] is False
    assert [view["name"] for view in result["views"]] == [
        "isometric",
        "top",
        "front",
        "right",
    ]
    assert [attempt["mode"] for attempt in captured] == ["ultra_light"] * 4
    assert [attempt["view_names"] for attempt in captured] == [
        ["isometric"],
        ["top"],
        ["front"],
        ["right"],
    ]
    assert captured[0]["timeout_sec"] == 30.0


def test_high_complexity_uses_one_ultra_light_view(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    captured = []

    def fake_worker(source: Path, **kwargs):
        captured.append(kwargs)
        if kwargs["view_names"][0] not in {"isometric"}:
            return preview_service.unavailable_preview("secondary view failed")
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
        complexity_score="high",
        settings=_settings(max_render_views_high_complexity=1),
    )

    assert result["available"] is True
    assert result["partial"] is False
    assert [view["name"] for view in result["views"]] == ["isometric"]
    assert [attempt["mode"] for attempt in captured] == ["ultra_light"]


def test_high_complexity_failure_returns_controlled_fallback(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    attempts = []

    def fake_worker(source: Path, **kwargs):
        attempts.append(kwargs)
        return preview_service.unavailable_preview("ultra-light renderer failed")

    monkeypatch.setattr(preview_service, "_run_worker", fake_worker)
    result = preview_service.generate_safe_step_preview(
        str(step_path),
        complexity_score="high",
        settings=_settings(),
    )

    assert result["available"] is False
    assert result["mode"] == "failed"
    assert [attempt["mode"] for attempt in attempts] == ["ultra_light"] * 4
    assert (
        "Preview generation failed after all high-complexity view attempts"
        in result["warnings"]
    )


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
    assert [attempt["max_views"] for attempt in attempts] == [4, 4]
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
    assert [attempt["max_views"] for attempt in attempts] == [4, 4, 4]
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
