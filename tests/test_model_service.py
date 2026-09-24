import subprocess
import json
import hashlib

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


def test_sha256_file_works_without_file_digest_and_across_chunks(tmp_path, monkeypatch):
    step_path = tmp_path / "private-customer-part.step"
    content = b"STEP" * (1024 * 1024 // 4 + 1) + b"end"
    step_path.write_bytes(content)

    with monkeypatch.context() as compat:
        compat.delattr(model_service.hashlib, "file_digest", raising=False)
        assert model_service._sha256_file(step_path) == hashlib.sha256(content).hexdigest()


def test_viewer_model_success_without_file_digest(tmp_path, monkeypatch, caplog):
    step_path = tmp_path / "private-customer-part.step"
    step_path.write_bytes(b"STEP")

    class SuccessfulWorker:
        returncode = 0

        def communicate(self, timeout):
            return "", ""

    def launch(command, **_kwargs):
        with open(command[4], "w", encoding="utf-8") as output:
            json.dump({"available": True, "model_base64": "Z2xURg==",
                       "format": "glb", "warnings": []}, output)
        return SuccessfulWorker()

    monkeypatch.setattr(model_service.subprocess, "Popen", launch)
    with caplog.at_level("INFO", logger="uvicorn.error"):
        with monkeypatch.context() as compat:
            compat.delattr(model_service.hashlib, "file_digest", raising=False)
            result = model_service.generate_safe_viewer_model(
                str(step_path), settings=_settings(),
            )
    assert result["available"] is True
    assert result["format"] == "glb"
    assert "private-customer-part" not in caplog.text
    assert str(tmp_path) not in caplog.text


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


def test_timeout_log_keeps_last_worker_phase_without_private_path(tmp_path, monkeypatch, caplog):
    step_path = tmp_path / "private-customer-part.step"
    step_path.write_text("STEP", encoding="ascii")

    class TimedOutWorker:
        pid = 987654
        returncode = None

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("worker", timeout)

        def poll(self):
            return self.returncode

    def launch(command, **_kwargs):
        status_path = command[command.index("--status-path") + 1]
        with open(status_path, "w", encoding="utf-8") as output:
            json.dump({"phase": "occ_normals", "face_index": 42,
                       "step_load_sec": 2.1, "worker_peak_rss_mib": 103.2,
                       "private_path": str(step_path)}, output)
        return worker

    worker = TimedOutWorker()
    monkeypatch.setattr(model_service.subprocess, "Popen", launch)
    monkeypatch.setattr(model_service, "_stop_worker",
                        lambda process: setattr(process, "returncode", -9))
    with caplog.at_level("INFO", logger="uvicorn.error"):
        with monkeypatch.context() as compat:
            compat.delattr(model_service.hashlib, "file_digest", raising=False)
            result = model_service.generate_safe_viewer_model(
                str(step_path), settings=_settings(timeout_sec=.1),
            )
    assert result["available"] is False
    entry = next(json.loads(record.message.split("viewer_model_diagnostic ", 1)[1])
                 for record in caplog.records if "viewer_model_diagnostic " in record.message)
    assert entry["outcome"] == "timeout"
    assert entry["worker"]["phase"] == "occ_normals"
    assert entry["worker"]["face_index"] == 42
    assert entry["worker_exit_code"] == -9
    assert entry["worker_terminated"] is True
    assert entry["peak_worker_rss_mib"] == 103.2
    assert "private-customer-part" not in caplog.text
    assert str(tmp_path) not in caplog.text
