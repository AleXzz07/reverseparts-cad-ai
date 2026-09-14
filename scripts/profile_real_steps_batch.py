from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.request


MIB = 1024 * 1024
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CANONICAL_CASES = (
    "SWPR-Moderate- Mounting Bracket.STEP",
    "SWPR-Moderate Sheetmetal 4.STEP",
    "SWPR-moderate sheetmetal part 1.STEP",
    "SWPR-Simple Sheet Metal Part 9.STEP",
    "SWPR-Complex Sheet Metal Part 4.STEP",
    "SWPR-Complex Sheet Metal Part 6.STEP",
    "SWPR-complex sheetmetal part 2.STEP",
    "SWPR-Moderate-Correct-and-Flatten-Imported-Sheet-Metal.STEP",
    "User Library-Part1-451.STEP",
    "User Library-LogMaxEnclosure.STEP",
)
NEGATIVE_CASE = "User Library-LogMaxEnclosure.STEP"
REFERENCE_CASE = {
    "name": "tests/dataset/staffa_16_pieghe_stress_test/input.stp",
    "worker_peak_mib": 1641.34,
    "api_plus_worker_peak_mib": 1703.61,
    "analyze_step_file_sec": 153.20,
    "source": "previous measured Docker profile",
}
CSV_FIELDS = (
    "file_name",
    "negative_case",
    "file_size_bytes",
    "file_size_mib",
    "step_load_sec",
    "analyze_step_file_sec",
    "total_worker_sec",
    "worker_before_freecad_mib",
    "worker_after_freecad_part_mib",
    "worker_after_geometry_load_mib",
    "peak_worker_mib",
    "peak_api_plus_worker_mib",
    "api_after_worker_exit_mib",
    "residual_process_count",
    "residual_process_pids",
    "outcome",
    "classification",
    "detected_thickness_mm",
    "bend_count",
    "opening_count",
    "flat_pattern_status",
    "usable_for_costing",
    "error",
    "timeout",
    "ram_limit_exceeded",
)


def _project_environment() -> dict[str, str]:
    environment = os.environ.copy()
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        str(PROJECT_ROOT)
        if not current
        else str(PROJECT_ROOT) + os.pathsep + current
    )
    return environment


def _proc_status(pid: int | str) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        lines = Path(f"/proc/{pid}/status").read_text().splitlines()
        for line in lines:
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
            command = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
            processes[int(entry.name)] = (ppid, pgrp, command)
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
    return sum(
        _proc_status(child).get("VmRSS", 0)
        for child in _descendants(pid, processes)
    )


def _process_group_members(pgid: int) -> list[int]:
    return sorted(
        pid for pid, (_, process_group, _) in _all_processes().items()
        if process_group == pgid
    )


def _signal_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, sig)
        elif sig == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


def _terminate_group(process: subprocess.Popen[str], grace_sec: float = 2.0) -> None:
    pgid = process.pid
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if _process_group_members(pgid):
        try:
            if os.name == "posix":
                os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        time.sleep(0.1)


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


def _worker_result(result) -> dict:
    return {
        "classification": result.part_classification.category,
        "detected_thickness_mm": result.detected_thickness_mm,
        "bend_count": result.bends.count,
        "opening_count": result.holes.physical_openings_total,
        "flat_pattern_status": result.flat_pattern.status,
        "usable_for_costing": result.flat_pattern.usable_for_costing,
    }


def _profile_worker(step_path: Path) -> int:
    try:
        _marker("worker_python_started")
        from app.cad_analyzer import analyze_step_file, get_freecad_status

        status = get_freecad_status()
        if not status.available:
            raise RuntimeError(status.error or "FreeCAD unavailable")
        import FreeCAD  # noqa: F401
        import Part  # noqa: F401

        _marker("freecad_part_imported")
        analyzer_file = Path(sys.modules[analyze_step_file.__module__].__file__).resolve()
        source_lines = analyzer_file.read_text(encoding="utf-8").splitlines()
        shape_create_line = next(
            index + 1 for index, line in enumerate(source_lines)
            if "shape = Part.Shape()" in line
        )
        shape_read_line = next(
            index + 1 for index, line in enumerate(source_lines)
            if "shape.read(temp_path)" in line
        )
        emitted_start = False
        emitted_loaded = False

        def analyze_tracer(frame, event, arg):
            nonlocal emitted_start, emitted_loaded
            if event != "line":
                return analyze_tracer
            if not emitted_start and frame.f_lineno >= shape_create_line:
                emitted_start = True
                _marker("step_shape_load_started")
            if not emitted_loaded and frame.f_lineno >= shape_read_line + 2:
                emitted_loaded = True
                _marker("step_shape_loaded")
            return analyze_tracer

        def global_tracer(frame, event, arg):
            if event == "call" and frame.f_code is analyze_step_file.__code__:
                return analyze_tracer
            return None

        file_bytes = step_path.read_bytes()
        _marker("before_analyze_step_file")
        sys.settrace(global_tracer)
        try:
            result = analyze_step_file(file_bytes=file_bytes, source_file=step_path.name)
        finally:
            sys.settrace(None)
        _marker("analysis_completed")
        print("ANALYSIS_RESULT " + json.dumps(_worker_result(result)), flush=True)
        return 0
    except Exception as exc:
        print(
            "ANALYSIS_ERROR "
            + json.dumps({"type": type(exc).__name__, "message": str(exc)}),
            flush=True,
        )
        return 1


def _wait_for_health(process: subprocess.Popen, timeout_sec: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"API exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:18764/healthz", timeout=0.25
            ) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise TimeoutError("API did not become healthy")


def _empty_row(step_path: Path, report_name: str, negative_case: bool) -> dict:
    return {
        "file_name": report_name,
        "negative_case": negative_case,
        "file_size_bytes": step_path.stat().st_size,
        "file_size_mib": round(step_path.stat().st_size / MIB, 3),
        "step_load_sec": None,
        "analyze_step_file_sec": None,
        "total_worker_sec": None,
        "worker_before_freecad_mib": None,
        "worker_after_freecad_part_mib": None,
        "worker_after_geometry_load_mib": None,
        "peak_worker_mib": None,
        "peak_api_plus_worker_mib": None,
        "api_after_worker_exit_mib": None,
        "residual_process_count": 0,
        "residual_process_pids": "",
        "outcome": "failed",
        "classification": None,
        "detected_thickness_mm": None,
        "bend_count": None,
        "opening_count": None,
        "flat_pattern_status": None,
        "usable_for_costing": None,
        "error": None,
        "timeout": False,
        "ram_limit_exceeded": False,
    }


def _run_case(
    api_pid: int,
    step_path: Path,
    report_name: str,
    negative_case: bool,
    timeout_sec: float,
    max_worker_ram_mib: float,
    sample_interval_sec: float,
) -> dict:
    row = _empty_row(step_path, report_name, negative_case)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        str(step_path),
    ]
    started = time.monotonic()
    worker = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=str(PROJECT_ROOT),
        env=_project_environment(),
    )
    pgid = worker.pid
    markers: dict[str, dict] = {}
    analysis_result: dict = {}
    worker_error: dict = {}

    def read_output() -> None:
        assert worker.stdout is not None
        for line in worker.stdout:
            if line.startswith("MEMORY_MARKER "):
                item = json.loads(line.removeprefix("MEMORY_MARKER "))
                markers[item["phase"]] = item
            elif line.startswith("ANALYSIS_RESULT "):
                analysis_result.update(json.loads(line.removeprefix("ANALYSIS_RESULT ")))
            elif line.startswith("ANALYSIS_ERROR "):
                worker_error.update(json.loads(line.removeprefix("ANALYSIS_ERROR ")))

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    peak_worker_bytes = 0
    peak_total_bytes = 0
    termination_reason: str | None = None
    try:
        while worker.poll() is None:
            elapsed = time.monotonic() - started
            worker_bytes = _tree_rss(worker.pid)
            api_bytes = _tree_rss(api_pid)
            peak_worker_bytes = max(peak_worker_bytes, worker_bytes)
            peak_total_bytes = max(peak_total_bytes, worker_bytes + api_bytes)
            if timeout_sec > 0 and elapsed >= timeout_sec:
                termination_reason = "timeout"
                break
            if max_worker_ram_mib > 0 and worker_bytes / MIB >= max_worker_ram_mib:
                termination_reason = "ram_limit"
                break
            time.sleep(sample_interval_sec)
        if termination_reason:
            _terminate_group(worker)
        else:
            worker.wait()
    except KeyboardInterrupt:
        _terminate_group(worker)
        raise
    finally:
        reader.join(timeout=2)

    residual = _process_group_members(pgid)
    if residual:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        time.sleep(0.1)
        residual = _process_group_members(pgid)

    row["total_worker_sec"] = round(time.monotonic() - started, 3)
    row["peak_worker_mib"] = round(peak_worker_bytes / MIB, 2)
    row["peak_api_plus_worker_mib"] = round(peak_total_bytes / MIB, 2)
    row["api_after_worker_exit_mib"] = round(_tree_rss(api_pid) / MIB, 2)
    row["residual_process_count"] = len(residual)
    row["residual_process_pids"] = ",".join(map(str, residual))

    def marker_mib(name: str) -> float | None:
        value = markers.get(name, {}).get("rss_bytes")
        return None if value is None else round(value / MIB, 2)

    def marker_time(name: str) -> float | None:
        value = markers.get(name, {}).get("monotonic")
        return None if value is None else float(value)

    row["worker_before_freecad_mib"] = marker_mib("worker_python_started")
    row["worker_after_freecad_part_mib"] = marker_mib("freecad_part_imported")
    row["worker_after_geometry_load_mib"] = marker_mib("step_shape_loaded")
    load_started = marker_time("step_shape_load_started")
    load_finished = marker_time("step_shape_loaded")
    analyze_started = marker_time("before_analyze_step_file")
    analyze_finished = marker_time("analysis_completed")
    if load_started is not None and load_finished is not None:
        row["step_load_sec"] = round(load_finished - load_started, 3)
    if analyze_started is not None and analyze_finished is not None:
        row["analyze_step_file_sec"] = round(analyze_finished - analyze_started, 3)

    if termination_reason == "timeout":
        row.update(outcome="timeout", timeout=True, error=f"Timeout after {timeout_sec:g} s")
    elif termination_reason == "ram_limit":
        row.update(
            outcome="ram_limit",
            ram_limit_exceeded=True,
            error=f"Worker RAM threshold exceeded: {max_worker_ram_mib:g} MiB",
        )
    elif worker.returncode != 0 or worker_error:
        detail = worker_error.get("message")
        if not detail and worker.stderr is not None:
            detail = " | ".join(worker.stderr.read().splitlines()[-5:])
        row.update(outcome="failed", error=detail or f"Worker exit code {worker.returncode}")
    else:
        row.update(outcome="completed", **analysis_result)
    return row


def _numeric(rows: list[dict], field: str) -> list[float]:
    return [float(row[field]) for row in rows if row.get(field) is not None]


def _summary(rows: list[dict]) -> dict:
    valid = [row for row in rows if not row["negative_case"]]
    ram = _numeric(valid, "peak_api_plus_worker_mib")
    times = _numeric(valid, "analyze_step_file_sec")
    completed = sum(row["outcome"] == "completed" for row in rows)
    timeout = sum(row["outcome"] == "timeout" for row in rows)
    ram_limits = sum(row["outcome"] == "ram_limit" for row in rows)
    failed = len(rows) - completed - timeout
    return {
        "tested_total": len(rows),
        "valid_cases": len(valid),
        "negative_cases": len(rows) - len(valid),
        "completed": completed,
        "failed": failed,
        "timeout": timeout,
        "ram_limit_terminated": ram_limits,
        "api_plus_worker_ram_min_mib": round(min(ram), 2) if ram else None,
        "api_plus_worker_ram_median_mib": round(statistics.median(ram), 2) if ram else None,
        "api_plus_worker_ram_max_mib": round(max(ram), 2) if ram else None,
        "analyze_time_median_sec": round(statistics.median(times), 3) if times else None,
        "analyze_time_max_sec": round(max(times), 3) if times else None,
        "over_512_mib": sum(value > 512 for value in ram),
        "over_1_gib": sum(value > 1024 for value in ram),
        "over_2_gib": sum(value > 2048 for value in ram),
        "valid_flat_count": sum(
            row.get("flat_pattern_status") in {"exact", "validated_estimate"}
            for row in valid
        ),
        "usable_for_costing_count": sum(
            row.get("usable_for_costing") is True for row in valid
        ),
    }


def _write_outputs(output_dir: Path, rows: list[dict], settings: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "real_step_performance_profile.json"
    csv_path = output_dir / "real_step_performance_profile.csv"
    payload = {
        "profile_version": "1.0",
        "settings": settings,
        "reference_case": REFERENCE_CASE,
        "summary": _summary(rows),
        "results": rows,
    }
    temporary_json = json_path.with_suffix(".json.tmp")
    temporary_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_json.replace(json_path)
    temporary_csv = csv_path.with_suffix(".csv.tmp")
    with temporary_csv.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in CSV_FIELDS} for row in rows)
    temporary_csv.replace(csv_path)


def _resolve_input_file(input_dir: Path, canonical_name: str) -> Path | None:
    exact = input_dir / canonical_name
    if exact.is_file():
        return exact
    uploaded_copy = input_dir / canonical_name.replace(".STEP", "(1).STEP")
    if uploaded_copy.is_file():
        return uploaded_copy
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequential profiler for ten real STEP files")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--input-dir", type=Path, default=Path("/input"))
    parser.add_argument("--output-dir", type=Path, default=Path("/output"))
    parser.add_argument("--timeout-sec", type=float, default=300.0)
    parser.add_argument("--max-worker-ram-mib", type=float, default=2048.0)
    parser.add_argument("--sample-interval-sec", type=float, default=0.02)
    args = parser.parse_args()
    os.chdir(PROJECT_ROOT)
    os.environ.update(_project_environment())
    if args.worker:
        return _profile_worker(args.worker.resolve())

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    resolved_inputs = {
        name: _resolve_input_file(input_dir, name) for name in CANONICAL_CASES
    }
    missing = [name for name, path in resolved_inputs.items() if path is None]
    if missing:
        parser.error("Missing canonical STEP files: " + "; ".join(missing))
    settings = {
        "sequential": True,
        "timeout_sec": args.timeout_sec,
        "max_worker_ram_mib": args.max_worker_ram_mib,
        "sample_interval_sec": args.sample_interval_sec,
        "input_dir": str(input_dir),
        "negative_case_excluded_from_aggregates": NEGATIVE_CASE,
    }
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "18764",
            "--workers",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(PROJECT_ROOT),
        env=_project_environment(),
    )
    rows: list[dict] = []
    try:
        _wait_for_health(api)
        settings["api_idle_rss_mib"] = round(_tree_rss(api.pid) / MIB, 2)
        for index, name in enumerate(CANONICAL_CASES, start=1):
            print(f"[{index}/{len(CANONICAL_CASES)}] Profiling {name}", file=sys.stderr, flush=True)
            row = _run_case(
                api.pid,
                resolved_inputs[name],
                name,
                name == NEGATIVE_CASE,
                args.timeout_sec,
                args.max_worker_ram_mib,
                args.sample_interval_sec,
            )
            rows.append(row)
            _write_outputs(output_dir, rows, settings)
            print(
                f"  {row['outcome']} | {row['peak_worker_mib']} MiB | "
                f"{row['analyze_step_file_sec']} s",
                file=sys.stderr,
                flush=True,
            )
    except KeyboardInterrupt:
        print("Batch interrupted safely; completed rows were preserved.", file=sys.stderr)
        _write_outputs(output_dir, rows, settings)
        return 130
    finally:
        _terminate_group(api)
    _write_outputs(output_dir, rows, settings)
    print(json.dumps({"output_dir": str(output_dir), "summary": _summary(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
