import subprocess

import app.model_service as model_service


def _settings(**overrides):
    values = {
        "enabled": True,
        "timeout_sec": 20.0,
        "max_file_size_mb": 20.0,
        "max_output_mb": 20.0,
    }
    values.update(overrides)
    return model_service.ViewerModelSettings(**values)


def test_viewer_model_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("VIEWER_MODEL_ENABLED", raising=False)

    assert model_service.ViewerModelSettings.from_env().enabled is True


def test_viewer_model_can_be_disabled(tmp_path):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")

    result = model_service.generate_safe_viewer_model(
        str(step_path),
        settings=_settings(enabled=False),
    )

    assert result["available"] is False
    assert result["model_base64"] is None
    assert result["format"] is None
    assert result["warnings"] == ["3D viewer model generation disabled"]


def test_viewer_model_rejects_oversized_input(tmp_path):
    step_path = tmp_path / "part.step"
    step_path.write_bytes(b"x" * 2048)

    result = model_service.generate_safe_viewer_model(
        str(step_path),
        settings=_settings(max_file_size_mb=0.001),
    )

    assert result["available"] is False
    assert "VIEWER_MODEL_MAX_FILE_SIZE_MB" in result["warnings"][0]


def test_viewer_model_timeout_is_controlled(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")

    class TimedOutWorker:
        pid = 123
        returncode = None

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("model-worker", timeout)

        def poll(self):
            return None

    monkeypatch.setattr(
        model_service.subprocess,
        "Popen",
        lambda *args, **kwargs: TimedOutWorker(),
    )
    monkeypatch.setattr(model_service, "_stop_worker", lambda process: None)

    result = model_service.generate_safe_viewer_model(
        str(step_path),
        settings=_settings(timeout_sec=0.1),
    )

    assert result["available"] is False
    assert result["model_base64"] is None
    assert "timed out" in result["warnings"][0]
