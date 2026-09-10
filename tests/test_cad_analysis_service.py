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
    CadAnalysisSuperseded,
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
    with service._JOB_CONDITION:
        service._CURRENT_JOB = None
        service._LATEST_ANALYSIS_BY_SESSION.clear()
        service._JOB_CONDITION.notify_all()
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
        "cancellation_grace_sec": 0.05,
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


def test_supersede_uses_term_then_force_kill_without_holding_registry_lock(
    monkeypatch,
):
    signals = []

    class StubbornProcess(FakeProcess):
        def __init__(self):
            super().__init__(["worker"], returncode=None)
            self.wait_calls = 0

        def wait(self, timeout):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("worker", timeout)
            self.returncode = -9
            return self.returncode

        def poll(self):
            return self.returncode

    process = StubbornProcess()
    monkeypatch.setattr(
        service.os,
        "killpg",
        lambda pid, sent_signal: signals.append(sent_signal),
    )

    service._terminate_worker_gracefully(process, 0.01)

    assert signals == [service.signal.SIGTERM, service.signal.SIGKILL]
    assert process.poll() == -9


def test_active_job_cancellation_forces_kill_when_owner_does_not_finish(
    monkeypatch,
):
    process = FakeProcess(["worker"], returncode=None)
    job = service.ActiveCadJob(
        session_id="same-session",
        analysis_id="analysis-a",
        process=process,
    )
    signals = []

    def record_signal(target, *, force):
        assert target is process
        signals.append(force)
        if force:
            target.returncode = -9

    monkeypatch.setattr(service, "_signal_worker", record_signal)

    service._cancel_active_job(
        job,
        _settings(cancellation_grace_sec=0.01, queue_timeout_sec=0.01),
    )

    assert signals == [False, True]
    assert process.poll() == -9


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


def test_different_session_cannot_cancel_active_analysis(monkeypatch):
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
    first = threading.Thread(
        target=lambda: first_result.append(
            run_isolated_cad_analysis(
                file_bytes=b"STEP-A",
                source_file="a.step",
                analysis_session_id="session-a",
                analysis_id="analysis-a",
                settings=settings,
            )
        )
    )
    first.start()
    assert first_started.wait(timeout=1.0)

    try:
        with pytest.raises(CadAnalysisBusy):
            run_isolated_cad_analysis(
                file_bytes=b"STEP-B",
                source_file="b.step",
                analysis_session_id="session-b",
                analysis_id="analysis-b",
                settings=settings,
            )
    finally:
        release_first.set()
        first.join(timeout=2.0)

    assert first_result[0].part_name == "first"
    assert maximum_active == 1


def test_latest_analysis_wins_for_same_session_and_cleans_previous(monkeypatch):
    first_started = threading.Event()
    first_terminated = threading.Event()
    processes = []
    commands = []

    class LatestWinsProcess(FakeProcess):
        def __init__(self, command, index):
            super().__init__(command, returncode=None)
            self.index = index
            self.terminated = False

        def communicate(self, timeout):
            if self.index == 0:
                first_started.set()
                first_terminated.wait(timeout=2.0)
                self.returncode = -15
                return "", "superseded"
            analysis = CadAnalysisResponse(part_name="B", source_file="b.step")
            with open(self.command[-1], "w", encoding="utf-8") as output_file:
                json.dump(
                    {"status": "ok", "analysis": analysis.model_dump()},
                    output_file,
                )
            self.returncode = 0
            return "", ""

        def poll(self):
            return self.returncode

    def fake_popen(command, **kwargs):
        process = LatestWinsProcess(command, len(processes))
        processes.append(process)
        commands.append(command)
        return process

    def signal_previous(process, *, force):
        assert process is processes[0]
        assert service._JOB_CONDITION.acquire(blocking=False) is True
        service._JOB_CONDITION.release()
        assert force is False
        process.terminated = True
        process.returncode = -15
        first_terminated.set()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service, "_signal_worker", signal_previous)
    settings = _settings(queue_timeout_sec=1.0)
    first_error = []

    def run_first():
        try:
            run_isolated_cad_analysis(
                file_bytes=b"STEP-A",
                source_file="a.step",
                analysis_session_id="same-session",
                analysis_id="analysis-a",
                settings=settings,
            )
        except Exception as exc:
            first_error.append(exc)

    first = threading.Thread(target=run_first)
    first.start()
    assert first_started.wait(timeout=1.0)

    second_result = run_isolated_cad_analysis(
        file_bytes=b"STEP-B",
        source_file="b.step",
        analysis_session_id="same-session",
        analysis_id="analysis-b",
        settings=settings,
    )
    first.join(timeout=2.0)

    assert second_result.part_name == "B"
    assert len(first_error) == 1
    assert isinstance(first_error[0], CadAnalysisSuperseded)
    assert first_error[0].analysis_id == "analysis-a"
    assert processes[0].terminated is True
    assert all(process.poll() is not None for process in processes)
    assert all(not service.Path(command[-2]).exists() for command in commands)
    assert all(not service.Path(command[-1]).exists() for command in commands)


def test_same_session_http_request_a_returns_409_and_b_completes(monkeypatch):
    first_started = threading.Event()
    first_stopped = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    processes = []

    class HttpProcess(FakeProcess):
        def __init__(self, command, index):
            super().__init__(command, returncode=None)
            self.index = index

        def communicate(self, timeout):
            if self.index == 0:
                first_started.set()
                first_stopped.wait(timeout=3.0)
                self.returncode = -15
                return "", "superseded"
            second_started.set()
            release_second.wait(timeout=3.0)
            analysis = CadAnalysisResponse(part_name="B", source_file="b.step")
            with open(self.command[-1], "w", encoding="utf-8") as output_file:
                json.dump(
                    {"status": "ok", "analysis": analysis.model_dump()},
                    output_file,
                )
            self.returncode = 0
            return "", ""

        def poll(self):
            return self.returncode

    def fake_popen(command, **kwargs):
        process = HttpProcess(command, len(processes))
        processes.append(process)
        return process

    def signal_worker(process, *, force):
        assert process is processes[0]
        process.returncode = -9 if force else -15
        first_stopped.set()

    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(service, "_signal_worker", signal_worker)
    monkeypatch.setattr(
        api,
        "quote_from_cad",
        lambda *args, **kwargs: {"part_name": "B", "quantity": 1},
    )
    monkeypatch.setenv("CAD_ANALYSIS_QUEUE_TIMEOUT_SEC", "2")

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            common_data = {
                "material": "acciaio",
                "quantity": "1",
                "analysis_session_id": "browser-session",
            }
            first_task = asyncio.create_task(
                client.post(
                    "/analyze-and-quote",
                    data={**common_data, "analysis_id": "analysis-a"},
                    files={"file": ("a.step", b"STEP-A", "application/step")},
                )
            )
            assert await asyncio.to_thread(first_started.wait, 1.0)
            second_task = asyncio.create_task(
                client.post(
                    "/analyze-and-quote",
                    data={**common_data, "analysis_id": "analysis-b"},
                    files={"file": ("b.step", b"STEP-B", "application/step")},
                )
            )
            assert await asyncio.to_thread(second_started.wait, 2.0)
            healthz = await asyncio.wait_for(client.get("/healthz"), timeout=1.0)
            release_second.set()
            return await first_task, await second_task, healthz

    try:
        first_response, second_response, healthz_response = asyncio.run(exercise())
    finally:
        first_stopped.set()
        release_second.set()

    assert first_response.status_code == 409
    assert first_response.json()["detail"]["code"] == "cad_analysis_superseded"
    assert first_response.json()["detail"]["analysis_id"] == "analysis-a"
    assert second_response.status_code == 200
    assert second_response.json()["analysis"]["part_name"] == "B"
    assert healthz_response.status_code == 200
    assert all(process.poll() is not None for process in processes)


def test_health_endpoints_respond_during_slow_analysis(monkeypatch):
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
            healthz_response = await asyncio.wait_for(
                client.get("/healthz"),
                timeout=1.0,
            )
            elapsed = time.monotonic() - before
            release_analysis.set()
            analysis_response = await analysis_task
            return health_response, healthz_response, elapsed, analysis_response

    try:
        health_response, healthz_response, elapsed, analysis_response = asyncio.run(
            exercise()
        )
    finally:
        release_analysis.set()

    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"
    assert healthz_response.status_code == 200
    assert healthz_response.json() == {"status": "ok"}
    assert elapsed < 1.0
    assert analysis_response.status_code == 200


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (CadAnalysisTimeout("slow"), 504, "cad_analysis_timeout"),
        (CadAnalysisBusy("busy"), 503, "cad_analysis_busy"),
        (
            CadAnalysisSuperseded("old-analysis"),
            409,
            "cad_analysis_superseded",
        ),
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
