import asyncio
import json
import subprocess
import threading
import time

import httpx
import pytest

import app.cad_analysis_service as service
import app.main as api
from app.cad_analysis_service import (
    CadAnalysisBusy,
    CadAnalysisInvalidOutput,
    CadAnalysisSettings,
    CadAnalysisTimeout,
    CadAnalysisWorkerCrash,
    CadAnalysisWorkerError,
    run_isolated_cad_analysis,
)
from app.main import app
from app.schemas import CadAnalysisResponse


@pytest.fixture(autouse=True)
def restore_service_state():
    diagnostic = service.get_cached_freecad_diagnostic()
    yield
    service._set_freecad_diagnostic(
        diagnostic["status"],
        diagnostic.get("error"),
    )


def _settings(**overrides):
    values = {
        "timeout_sec": 5.0,
        "queue_timeout_sec": 0.05,
        "max_concurrency": 1,
        "max_output_mb": 2.0,
        "diagnostic_timeout_sec": 2.0,
    }
    values.update(overrides)
    return CadAnalysisSettings(**values)


class FakeProcess:
    def __init__(self, command, *, payload=None, returncode=0, stderr=""):
        self.command = command
        self.payload = payload
        self.returncode = returncode
        self.stderr = stderr
        self.pid = 123456

    def communicate(self, timeout):
        if self.payload is not None:
            output_path = self.command[-1]
            with open(output_path, "w", encoding="utf-8") as output_file:
                json.dump(self.payload, output_file)
        return "", self.stderr

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _invoke(settings=None):
    return run_isolated_cad_analysis(
        file_bytes=b"STEP",
        source_file="part.step",
        material="acciaio",
        density_g_cm3=7.85,
        quantity=1,
        settings=settings or _settings(),
    )


def test_isolated_analysis_uses_python_module_without_shell(monkeypatch):
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        analysis = CadAnalysisResponse(part_name="isolated", source_file="part.step")
        return FakeProcess(
            command,
            payload={"status": "ok", "analysis": analysis.model_dump()},
        )

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)

    result = _invoke()

    assert result.part_name == "isolated"
    assert captured["command"][0] == service.sys.executable
    assert captured["command"][1:3] == ["-m", "app.cad_analysis_worker"]
    assert "shell" not in captured["kwargs"]
    assert "part.step" not in captured["command"]
    assert not service.Path(captured["command"][-2]).exists()
    assert not service.Path(captured["command"][-1]).exists()


def test_worker_timeout_is_distinct_and_worker_is_stopped(monkeypatch):
    stopped = []

    class TimeoutProcess(FakeProcess):
        def communicate(self, timeout):
            raise subprocess.TimeoutExpired(self.command, timeout)

        def poll(self):
            return None

    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda command, **kwargs: TimeoutProcess(command),
    )
    monkeypatch.setattr(service, "_stop_worker", lambda process: stopped.append(process))

    with pytest.raises(CadAnalysisTimeout, match="timed out"):
        _invoke(_settings(timeout_sec=0.01))

    assert len(stopped) == 1


def test_worker_crash_is_distinct(monkeypatch):
    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda command, **kwargs: FakeProcess(
            command,
            returncode=9,
            stderr="native worker abort",
        ),
    )

    with pytest.raises(CadAnalysisWorkerCrash, match="code 9"):
        _invoke()


def test_worker_invalid_output_is_distinct(monkeypatch):
    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda command, **kwargs: FakeProcess(command),
    )

    with pytest.raises(CadAnalysisInvalidOutput, match="no output"):
        _invoke()


def test_worker_reported_technical_error_is_distinct(monkeypatch):
    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda command, **kwargs: FakeProcess(
            command,
            payload={
                "status": "error",
                "error_type": "technical_error",
                "message": "OpenCascade failed",
            },
        ),
    )

    with pytest.raises(CadAnalysisWorkerError) as error:
        _invoke()

    assert error.value.worker_error_type == "technical_error"


def test_only_one_cad_analysis_runs_at_a_time(monkeypatch):
    first_started = threading.Event()
    release_first = threading.Event()
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()

    class BlockingProcess(FakeProcess):
        def communicate(self, timeout):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            first_started.set()
            release_first.wait(timeout=2.0)
            with state_lock:
                active -= 1
            analysis = CadAnalysisResponse(part_name="first", source_file="part.step")
            with open(self.command[-1], "w", encoding="utf-8") as output_file:
                json.dump({"status": "ok", "analysis": analysis.model_dump()}, output_file)
            return "", ""

    monkeypatch.setattr(
        service.subprocess,
        "Popen",
        lambda command, **kwargs: BlockingProcess(command),
    )
    settings = _settings(queue_timeout_sec=0.02)
    first_result = []
    first = threading.Thread(target=lambda: first_result.append(_invoke(settings)))
    first.start()
    assert first_started.wait(timeout=1.0)

    try:
        with pytest.raises(CadAnalysisBusy):
            _invoke(settings)
    finally:
        release_first.set()
        first.join(timeout=2.0)

    assert first_result[0].part_name == "first"
    assert maximum_active == 1


def test_health_responds_during_slow_analysis(monkeypatch):
    analysis_started = threading.Event()
    release_analysis = threading.Event()

    def slow_isolated_analysis(**kwargs):
        analysis_started.set()
        release_analysis.wait(timeout=3.0)
        return CadAnalysisResponse(part_name="slow", source_file=kwargs["source_file"])

    monkeypatch.setattr(api, "run_isolated_cad_analysis", slow_isolated_analysis)

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            analysis_task = asyncio.create_task(
                client.post(
                    "/analyze-cad",
                    files={"file": ("slow.step", b"STEP", "application/step")},
                )
            )
            started = await asyncio.to_thread(analysis_started.wait, 1.0)
            assert started is True
            before = time.monotonic()
            health_response = await asyncio.wait_for(client.get("/health"), timeout=1.0)
            elapsed = time.monotonic() - before
            release_analysis.set()
            analysis_response = await analysis_task
            return health_response, elapsed, analysis_response

    try:
        health_response, elapsed, analysis_response = asyncio.run(exercise())
    finally:
        release_analysis.set()

    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"
    assert elapsed < 1.0
    assert analysis_response.status_code == 200


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (CadAnalysisTimeout("slow"), 504, "cad_analysis_timeout"),
        (CadAnalysisBusy("busy"), 503, "cad_analysis_busy"),
        (CadAnalysisWorkerCrash("crash"), 502, "cad_analysis_worker_crash"),
        (CadAnalysisInvalidOutput("bad output"), 502, "cad_analysis_invalid_output"),
    ],
)
def test_api_survives_worker_failure(monkeypatch, error, expected_status, expected_code):
    def fail_analysis(**kwargs):
        raise error

    monkeypatch.setattr(api, "run_isolated_cad_analysis", fail_analysis)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        analysis_response = client.post(
            "/analyze-cad",
            files={"file": ("part.step", b"STEP", "application/step")},
        )
        health_response = client.get("/health")

    assert analysis_response.status_code == expected_status
    assert analysis_response.json()["detail"]["code"] == expected_code
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"


@pytest.mark.parametrize(
    ("worker_error_type", "expected_status"),
    [("technical_error", 500), ("freecad_unavailable", 503)],
)
def test_api_distinguishes_worker_reported_errors(
    monkeypatch,
    worker_error_type,
    expected_status,
):
    def fail_analysis(**kwargs):
        raise CadAnalysisWorkerError(
            "worker report",
            worker_error_type=worker_error_type,
        )

    monkeypatch.setattr(api, "run_isolated_cad_analysis", fail_analysis)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/analyze-cad",
            files={"file": ("part.step", b"STEP", "application/step")},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == "cad_analysis_worker_error"
    assert response.json()["detail"]["worker_error_type"] == worker_error_type
