from __future__ import annotations

import argparse
import math
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid


MIB = 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _project_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(PROJECT_ROOT)
        if not existing
        else str(PROJECT_ROOT) + os.pathsep + existing
    )
    return environment


def _proc_status(pid: int | str) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, value = line.split(":", 1)
                result[key] = int(value.strip().split()[0]) * 1024
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return result


def _all_processes() -> dict[int, tuple[int, int, str]]:
    processes: dict[int, tuple[int, int, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            end = stat.rfind(")")
            fields = stat[end + 2 :].split()
            ppid = int(fields[1])
            pgrp = int(fields[2])
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            processes[int(entry.name)] = (ppid, pgrp, cmdline)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
    return processes


def _descendants(root_pid: int, processes: dict[int, tuple[int, int, str]]) -> set[int]:
    found = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _, _) in processes.items():
            if pid not in found and ppid in found:
                found.add(pid)
                changed = True
    return found


def _tree_rss(pid: int) -> int:
    processes = _all_processes()
    return sum(_proc_status(child).get("VmRSS", 0) for child in _descendants(pid, processes))


def _cad_worker_pids() -> list[int]:
    return sorted(
        pid
        for pid, (_, _, cmdline) in _all_processes().items()
        if "app.cad_analysis_worker" in cmdline and pid != os.getpid()
    )


def _cgroup_bytes(name: str) -> int | None:
    for root in (Path("/sys/fs/cgroup"), Path("/sys/fs/cgroup/memory")):
        path = root / name
        try:
            value = path.read_text().strip()
            return None if value == "max" else int(value)
        except (FileNotFoundError, PermissionError, ValueError):
            continue
    return None


def _marker(phase: str) -> None:
    status = _proc_status("self")
    print(
        "MEMORY_MARKER "
        + json.dumps(
            {
                "phase": phase,
                "pid": os.getpid(),
                "rss_bytes": status.get("VmRSS", 0),
                "hwm_bytes": status.get("VmHWM", 0),
                "monotonic": time.monotonic(),
            }
        ),
        flush=True,
    )


def _profile_worker(step_path: Path) -> int:
    _marker("worker_python_started")
    from app.cad_analyzer import analyze_step_file, get_freecad_status

    status = get_freecad_status()
    if not status.available:
        raise RuntimeError(status.error or "FreeCAD unavailable")
    import FreeCAD  # noqa: F401
    import Part  # noqa: F401

    _marker("freecad_part_imported")
    target_file = str(Path(sys.modules[analyze_step_file.__module__].__file__).resolve())
    source_lines = Path(target_file).read_text(encoding="utf-8").splitlines()
    read_line = next(
        index + 1
        for index, line in enumerate(source_lines)
        if "shape.read(temp_path)" in line
    )
    after_read_line = read_line + 2
    emitted = False

    def tracer(frame, event, arg):
        nonlocal emitted
        if (
            not emitted
            and event == "line"
            and str(Path(frame.f_code.co_filename).resolve()) == target_file
            and frame.f_lineno >= after_read_line
        ):
            emitted = True
            _marker("step_shape_loaded")
        return tracer

    file_bytes = step_path.read_bytes()
    _marker("before_analyze_step_file")
    sys.settrace(tracer)
    try:
        result = analyze_step_file(file_bytes=file_bytes, source_file=step_path.name)
    finally:
        sys.settrace(None)
    _marker("analysis_completed")
    payload = result.model_dump() if hasattr(result, "model_dump") else result.dict()
    serialized = json.dumps(payload)
    print(f"SERIALIZED_BYTES {len(serialized.encode('utf-8'))}", flush=True)
    _marker("result_serialized")
    return 0


def _wait_for_health(process: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"API exited with code {process.returncode}")
        try:
            with urllib.request.urlopen("http://127.0.0.1:18763/healthz", timeout=0.25) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise TimeoutError("API did not become healthy")


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _run_case(api_pid: int, step_path: Path, interval: float) -> dict:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(step_path)]
    worker = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=PROJECT_ROOT,
        env=_project_environment(),
    )
    worker_pgid = worker.pid
    markers: dict[str, dict] = {}
    stderr_lines: list[str] = []
    output_done = threading.Event()

    def read_output() -> None:
        assert worker.stdout is not None
        for line in worker.stdout:
            if line.startswith("MEMORY_MARKER "):
                item = json.loads(line.removeprefix("MEMORY_MARKER "))
                markers[item["phase"]] = item
        output_done.set()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    peak_worker = 0
    peak_during_analysis = 0
    peak_total = 0
    api_at_peak = 0
    while worker.poll() is None:
        worker_rss = _tree_rss(worker.pid)
        api_rss = _tree_rss(api_pid)
        peak_worker = max(peak_worker, worker_rss)
        if "before_analyze_step_file" in markers and "analysis_completed" not in markers:
            peak_during_analysis = max(peak_during_analysis, worker_rss)
        if api_rss + worker_rss > peak_total:
            peak_total = api_rss + worker_rss
            api_at_peak = api_rss
        time.sleep(interval)
    output_done.wait(timeout=2)
    reader.join(timeout=2)
    if worker.stderr is not None:
        stderr_lines = worker.stderr.read().splitlines()
    process_snapshot = _all_processes()
    residual_group = [pid for pid, (_, pgrp, _) in process_snapshot.items() if pgrp == worker_pgid]
    if worker.returncode != 0:
        raise RuntimeError("Profiler worker failed: " + " | ".join(stderr_lines[-5:]))
    return {
        "case": step_path.name,
        "file_size_bytes": step_path.stat().st_size,
        "markers": markers,
        "peak_worker_tree_bytes": peak_worker,
        "peak_during_analyze_step_file_bytes": peak_during_analysis,
        "api_rss_at_combined_peak_bytes": api_at_peak,
        "peak_api_plus_worker_bytes": peak_total,
        "api_rss_after_worker_exit_bytes": _tree_rss(api_pid),
        "residual_process_group_pids": residual_group,
    }


def _latest_wins_check(complex_step: Path, simple_step: Path, interval: float) -> dict:
    from app.cad_analysis_service import CadAnalysisSettings, run_isolated_cad_analysis

    settings = CadAnalysisSettings.from_env()
    session_id = f"memory-profile-{uuid.uuid4()}"
    outcomes: dict[str, str] = {}
    max_workers = 0
    samples: list[list[int]] = []

    def invoke(label: str, step_path: Path) -> None:
        try:
            run_isolated_cad_analysis(
                file_bytes=step_path.read_bytes(),
                source_file=step_path.name,
                analysis_session_id=session_id,
                analysis_id=label,
                settings=settings,
            )
            outcomes[label] = "completed"
        except Exception as exc:
            outcomes[label] = type(exc).__name__

    first = threading.Thread(target=invoke, args=("A", complex_step))
    first.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not _cad_worker_pids():
        time.sleep(interval)
    second = threading.Thread(target=invoke, args=("B", simple_step))
    second.start()
    while first.is_alive() or second.is_alive():
        pids = _cad_worker_pids()
        max_workers = max(max_workers, len(pids))
        if pids:
            samples.append(pids)
        time.sleep(interval)
    first.join()
    second.join()
    time.sleep(0.2)
    return {
        "outcomes": outcomes,
        "max_simultaneous_cad_workers": max_workers,
        "overlap_observed": max_workers > 1,
        "residual_cad_worker_pids": _cad_worker_pids(),
        "sampled_worker_pid_sets": samples[-20:],
    }


def _mib(value: int) -> float:
    return round(value / MIB, 2)


def _humanize(report: dict) -> dict:
    converted = json.loads(json.dumps(report))
    for case in converted["cases"]:
        case["peak_worker_tree_mib"] = _mib(case.pop("peak_worker_tree_bytes"))
        case["peak_during_analyze_step_file_mib"] = _mib(
            case.pop("peak_during_analyze_step_file_bytes")
        )
        case["api_rss_at_combined_peak_mib"] = _mib(case.pop("api_rss_at_combined_peak_bytes"))
        case["peak_api_plus_worker_mib"] = _mib(case.pop("peak_api_plus_worker_bytes"))
        case["api_rss_after_worker_exit_mib"] = _mib(case.pop("api_rss_after_worker_exit_bytes"))
        for marker in case["markers"].values():
            marker["rss_mib"] = _mib(marker.pop("rss_bytes"))
            marker["hwm_mib"] = _mib(marker.pop("hwm_bytes"))
    converted["api_idle_rss_mib"] = _mib(converted.pop("api_idle_rss_bytes"))
    converted["api_final_rss_mib"] = _mib(converted.pop("api_final_rss_bytes"))
    for key in ("cgroup_limit_bytes", "cgroup_peak_bytes"):
        value = converted.pop(key, None)
        converted[key.removesuffix("_bytes") + "_mib"] = None if value is None else _mib(value)
    latest_peak = converted.pop("cgroup_peak_after_latest_wins_bytes", None)
    converted["cgroup_peak_after_latest_wins_mib"] = (
        None if latest_peak is None else _mib(latest_peak)
    )
    measured_peak = max(
        [case["peak_api_plus_worker_mib"] for case in converted["cases"]]
        + ([converted["cgroup_peak_mib"]] if converted["cgroup_peak_mib"] is not None else [])
    )
    converted["recommended_ram_mib_25pct"] = math.ceil(measured_peak * 1.25 / 64) * 64
    converted["recommended_ram_mib_30pct"] = math.ceil(measured_peak * 1.30 / 64) * 64
    return converted


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Docker-only CAD memory profiler")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--simple", type=Path)
    parser.add_argument("--complex", type=Path)
    parser.add_argument("--sample-interval", type=float, default=0.02)
    parser.add_argument("--skip-latest-wins", action="store_true")
    args = parser.parse_args()
    os.chdir(PROJECT_ROOT)
    os.environ.update(_project_environment())
    if args.worker:
        return _profile_worker(args.worker.resolve())
    if not args.simple or not args.complex:
        parser.error("--simple and --complex are required")

    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "18763",
            "--workers",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=PROJECT_ROOT,
        env=_project_environment(),
    )
    try:
        _wait_for_health(api)
        report = {
            "api_pid": api.pid,
            "api_idle_rss_bytes": _tree_rss(api.pid),
            "cgroup_limit_bytes": _cgroup_bytes("memory.max") or _cgroup_bytes("memory.limit_in_bytes"),
            "cases": [
                _run_case(api.pid, args.simple.resolve(), args.sample_interval),
                _run_case(api.pid, args.complex.resolve(), args.sample_interval),
            ],
        }
        report["cgroup_peak_bytes"] = _cgroup_bytes("memory.peak") or _cgroup_bytes("memory.max_usage_in_bytes")
        if not args.skip_latest_wins:
            report["latest_analysis_wins"] = _latest_wins_check(
                args.complex.resolve(), args.simple.resolve(), args.sample_interval
            )
            report["cgroup_peak_after_latest_wins_bytes"] = (
                _cgroup_bytes("memory.peak") or _cgroup_bytes("memory.max_usage_in_bytes")
            )
        report["api_final_rss_bytes"] = _tree_rss(api.pid)
        print(json.dumps(_humanize(report), indent=2))
    finally:
        _terminate(api)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
