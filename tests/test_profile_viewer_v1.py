import argparse
import json
import subprocess
from pathlib import Path

import scripts.profile_viewer_v1 as profiler


class _BoundingBox:
    XLength = 3.0
    YLength = 4.0
    ZLength = 12.0


class _Shape:
    BoundBox = _BoundingBox()

    def __init__(self):
        self.loaded_path = None

    def read(self, path):
        self.loaded_path = path

    def isNull(self):
        return False


class _Part:
    @staticmethod
    def Shape():
        return _Shape()


def test_load_shape_initializes_freecad_before_part(monkeypatch, tmp_path):
    step_path = tmp_path / "fixture.step"
    step_path.write_text("ISO-10303-21;", encoding="ascii")
    imports = []
    phases = []

    monkeypatch.setattr(profiler, "_configure_freecad_path", lambda: None)

    def fake_import(name):
        imports.append(name)
        return object() if name == "FreeCAD" else _Part

    monkeypatch.setattr(profiler.importlib, "import_module", fake_import)

    shape, diagonal, elapsed = profiler._load_shape(step_path, phases.append)

    assert imports == ["FreeCAD", "Part"]
    assert phases == [
        "configure_freecad_path",
        "import_freecad",
        "import_part",
        "load_step",
    ]
    assert shape.loaded_path == str(step_path)
    assert diagonal == 13.0
    assert elapsed >= 0.0


def test_run_one_preserves_native_crash_phase_and_exit_details(
    monkeypatch,
    tmp_path,
):
    step_path = tmp_path / "fixture.step"
    step_path.write_text("ISO-10303-21;", encoding="ascii")

    def fake_run(command, **_kwargs):
        output_path = Path(command[command.index("--worker-output") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "case": "fixture",
                    "mode": "v1",
                    "status": "running",
                    "phase": "import_part",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command,
            -11,
            stdout="worker output",
            stderr="native loader failed",
        )

    monkeypatch.setattr(profiler.subprocess, "run", fake_run)

    result = profiler._run_one(
        "fixture",
        step_path,
        "normal",
        "v1",
        10.0,
    )

    assert result["status"] == "failed"
    assert result["phase"] == "import_part"
    assert result["worker_exit_code"] == -11
    assert result["worker_signal"] == 11
    assert result["worker_signal_name"] == "SIGSEGV"
    assert "SIGSEGV" in result["error"]
    assert "import_part" in result["error"]
    assert result["worker_stdout"] == "worker output"
    assert result["worker_stderr"] == "native loader failed"


def test_worker_records_python_traceback_and_failure_phase(tmp_path):
    output_path = tmp_path / "worker-result.json"
    missing_step = tmp_path / "missing.step"
    args = argparse.Namespace(
        case="missing",
        step=str(missing_step),
        worker_output=str(output_path),
        complexity="normal",
        mode="legacy",
    )

    exit_code = profiler._worker(args)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["phase"] == "validate_step_path"
    assert payload["step_exists"] is False
    assert payload["error"].startswith("FileNotFoundError:")
    assert "FileNotFoundError" in payload["traceback"]
    assert str(missing_step) in payload["traceback"]
