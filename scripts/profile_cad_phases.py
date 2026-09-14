from __future__ import annotations

import argparse
import csv
import functools
import hashlib
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

NEGATIVE_CASE = "User Library-LogMaxEnclosure.STEP"
REFERENCE_NAME = "staffa_16_pieghe_stress_test/input.stp"
REFERENCE_PATH = PROJECT_ROOT / "tests/dataset/staffa_16_pieghe_stress_test/input.stp"
STEP_SUFFIXES = {".step", ".stp"}
PHASE_CSV_FIELDS = (
    "file_name",
    "negative_case",
    "reference_case",
    "phase",
    "function",
    "call_index",
    "completed",
    "rss_before_mib",
    "rss_after_mib",
    "rss_delta_mib",
    "peak_rss_mib",
    "duration_sec",
    "details_json",
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


def _source_sha256(relative_path: str) -> str:
    return hashlib.sha256((PROJECT_ROOT / relative_path).read_bytes()).hexdigest()


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
        pid
        for pid, (_, process_group, _) in _all_processes().items()
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


def _emit(kind: str, payload: dict) -> None:
    print(kind + " " + json.dumps(payload, default=str), flush=True)


def _rss_self() -> int:
    return _proc_status("self").get("VmRSS", 0)


def _result_details(value) -> dict:
    if value is None:
        return {"result_type": "None"}
    details: dict = {"result_type": type(value).__name__}
    if isinstance(value, (list, tuple, set, dict)):
        details["result_count"] = len(value)
    if hasattr(value, "panels"):
        details["panel_count"] = len(getattr(value, "panels", []))
    if hasattr(value, "bends"):
        bends = getattr(value, "bends", None)
        if isinstance(bends, (list, tuple, set, dict)):
            details["bend_zone_count"] = len(bends)
        elif bends is not None and hasattr(bends, "count"):
            details["bend_zone_count"] = int(bends.count)
    for field in (
        "connected",
        "direct",
        "category",
        "confidence",
        "reason",
        "status",
        "method",
        "usable_for_costing",
        "propagated_opening_count",
        "panel_count",
        "bend_zone_count",
    ):
        if hasattr(value, field):
            details[field] = getattr(value, field)
    validation = getattr(value, "validation", None)
    if validation is not None:
        details["validation_passed"] = getattr(validation, "passed", None)
        details["graph_connected"] = getattr(validation, "graph_connected", None)
        details["all_openings_propagated"] = getattr(
            validation, "all_openings_propagated", None
        )
    return details


class WorkerPhaseRecorder:
    def __init__(self) -> None:
        self.call_number = 0

    def start(self, phase: str, function: str, details: dict | None = None) -> str:
        self.call_number += 1
        call_id = f"{phase}:{self.call_number}"
        _emit(
            "PHASE_START",
            {
                "call_id": call_id,
                "phase": phase,
                "function": function,
                "rss_bytes": _rss_self(),
                "monotonic": time.monotonic(),
                "details": details or {},
            },
        )
        return call_id

    def end(self, call_id: str, result=None, details: dict | None = None) -> None:
        merged = _result_details(result)
        if details:
            merged.update(details)
        _emit(
            "PHASE_END",
            {
                "call_id": call_id,
                "rss_bytes": _rss_self(),
                "monotonic": time.monotonic(),
                "details": merged,
            },
        )

    def wrap(self, module, attribute: str, phase: str) -> object:
        original = getattr(module, attribute)

        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            call_id = self.start(phase, f"{module.__name__}.{attribute}")
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                self.end(
                    call_id,
                    details={"exception": type(exc).__name__, "message": str(exc)},
                )
                raise
            self.end(call_id, result=result)
            return result

        setattr(module, attribute, wrapped)
        return wrapped


def _install_phase_wrappers(recorder: WorkerPhaseRecorder, analyzer, unfolder) -> None:
    analyzer_targets = (
        ("_detect_sheet_thickness", "thickness_detection"),
        ("_classify_part_geometry", "sheet_metal_classification"),
        ("_detect_circular_holes", "opening_circular_detection"),
        ("_annotate_countersunk_holes", "opening_countersink_detection"),
        ("_detect_elongated_holes", "opening_slot_detection"),
        ("_detect_rounded_rectangular_holes", "opening_rounded_rectangle_detection"),
        ("_detect_polygonal_holes", "opening_polygonal_detection"),
        ("_detect_formed_holes", "opening_formed_detection"),
        ("_detect_unknown_holes", "opening_unknown_detection"),
        ("_annotate_hole_edge_distances", "hole_to_edge_analysis"),
        ("_annotate_hole_to_hole_distances", "hole_to_hole_analysis"),
        ("_analyze_assembly", "assembly_analysis"),
        ("_detect_bends", "bend_detection"),
        ("_detect_cutting_lengths", "cutting_detection"),
        ("_estimate_flat_pattern", "flat_pattern_orchestration"),
    )
    for attribute, phase in analyzer_targets:
        recorder.wrap(analyzer, attribute, phase)

    unfolder_targets = (
        ("build_sheet_topology_context", "topology_context_build"),
        ("_pair_planar_faces", "face_graph_planar_pairing"),
        ("_pair_bend_faces", "face_graph_bend_pairing"),
        ("build_sheet_face_graph", "face_graph_build"),
        ("_recursive_flattened_points", "recursive_flattening"),
        ("unfold_parallel_sheet", "unfold_parallel"),
        ("unfold_orthogonal_sheet", "unfold_orthogonal"),
        ("unfold_sheet", "unfold_dispatch"),
        ("propagate_openings_and_hole_to_bend", "flat_opening_propagation"),
    )
    wrapped: dict[str, object] = {}
    for attribute, phase in unfolder_targets:
        wrapped[attribute] = recorder.wrap(unfolder, attribute, phase)

    # cad_analyzer imported these functions by name; redirect only this worker's
    # module globals to the instrumented versions.
    analyzer.unfold_sheet = wrapped["unfold_sheet"]
    analyzer.propagate_openings_and_hole_to_bend = wrapped[
        "propagate_openings_and_hole_to_bend"
    ]
    analyzer.build_sheet_topology_context = wrapped["build_sheet_topology_context"]


def _final_analysis_details(result, shape_details: dict) -> dict:
    classification = result.part_classification
    flat = result.flat_pattern
    validation = flat.validation
    return {
        **shape_details,
        "classification": classification.category,
        "classification_confidence": classification.confidence,
        "classification_reason": classification.reason,
        "detected_thickness_mm": result.detected_thickness_mm,
        "thickness_confidence": result.thickness_confidence,
        "bend_count": result.bends.count,
        "opening_count": result.holes.physical_openings_total,
        "flat_pattern_status": flat.status,
        "flat_pattern_method": flat.method,
        "usable_for_costing": flat.usable_for_costing,
        "flat_validation_passed": validation.passed,
        "flat_graph_connected": validation.graph_connected,
        "flat_all_openings_propagated": validation.all_openings_propagated,
        "flat_warnings": list(flat.warnings),
        "analysis_warnings": list(result.warnings),
    }


def _profile_worker(step_path: Path, report_name: str) -> int:
    recorder = WorkerPhaseRecorder()
    shape_details: dict = {}
    try:
        import_call = recorder.start("freecad_import", "get_freecad_status + FreeCAD/Part")
        import app.cad_analyzer as analyzer
        import app.sheetmetal_unfolder as unfolder

        status = analyzer.get_freecad_status()
        if not status.available:
            raise RuntimeError(status.error or "FreeCAD unavailable")
        import FreeCAD  # noqa: F401
        import Part  # noqa: F401

        recorder.end(import_call, details={"freecad_available": True})
        _install_phase_wrappers(recorder, analyzer, unfolder)

        analyzer_file = Path(analyzer.__file__).resolve()
        source_lines = analyzer_file.read_text(encoding="utf-8").splitlines()
        create_line = next(
            index + 1 for index, line in enumerate(source_lines)
            if "shape = Part.Shape()" in line
        )
        read_line = next(
            index + 1 for index, line in enumerate(source_lines)
            if "shape.read(temp_path)" in line
        )
        thickness_line = next(
            index + 1 for index, line in enumerate(source_lines)
            if "detected_thickness, thickness_confidence = _detect_sheet_thickness(" in line
        )
        load_call: str | None = None
        inventory_call: str | None = None
        load_finished = False
        inventory_finished = False

        def analyze_tracer(frame, event, arg):
            nonlocal load_call, inventory_call, load_finished, inventory_finished, shape_details
            if event != "line":
                return analyze_tracer
            if load_call is None and frame.f_lineno >= create_line:
                load_call = recorder.start("step_shape_load", "Part.Shape.read")
            if not load_finished and load_call is not None and frame.f_lineno >= read_line + 2:
                shape = frame.f_locals.get("shape")
                shape_details = {
                    "shape_type": getattr(shape, "ShapeType", type(shape).__name__),
                    "solid_count": len(getattr(shape, "Solids", [])),
                    "shell_count": len(getattr(shape, "Shells", [])),
                    "face_count": len(getattr(shape, "Faces", [])),
                    "edge_count": len(getattr(shape, "Edges", [])),
                    "vertex_count": len(getattr(shape, "Vertexes", [])),
                }
                recorder.end(load_call, details=shape_details)
                load_finished = True
                inventory_call = recorder.start(
                    "shape_metadata_topology_inventory",
                    "inline bounding-box/mass/topology extraction",
                    shape_details,
                )
            if not inventory_finished and inventory_call is not None and frame.f_lineno >= thickness_line:
                recorder.end(inventory_call, details=shape_details)
                inventory_finished = True
                inventory_call = None
            return analyze_tracer

        def global_tracer(frame, event, arg):
            if event == "call" and frame.f_code is analyzer.analyze_step_file.__code__:
                return analyze_tracer
            return None

        file_bytes = step_path.read_bytes()
        total_call = recorder.start(
            "analyze_step_file_total",
            "app.cad_analyzer.analyze_step_file",
            {"file_size_bytes": len(file_bytes)},
        )
        sys.settrace(global_tracer)
        try:
            result = analyzer.analyze_step_file(
                file_bytes=file_bytes,
                source_file=report_name,
            )
        finally:
            sys.settrace(None)
        if inventory_call is not None and not inventory_finished:
            recorder.end(inventory_call, details=shape_details)
        recorder.end(total_call, result=result)

        serialization_call = recorder.start(
            "result_serialization", "CadAnalysisResponse.model_dump + json.dumps"
        )
        payload = result.model_dump() if hasattr(result, "model_dump") else result.dict()
        serialized = json.dumps(payload)
        recorder.end(
            serialization_call,
            details={"serialized_bytes": len(serialized.encode("utf-8"))},
        )
        _emit("ANALYSIS_RESULT", _final_analysis_details(result, shape_details))
        return 0
    except Exception as exc:
        _emit(
            "ANALYSIS_ERROR",
            {"type": type(exc).__name__, "message": str(exc)},
        )
        return 1


def _wait_for_health(process: subprocess.Popen, timeout_sec: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"API exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:18765/healthz", timeout=0.25
            ) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise TimeoutError("API did not become healthy")


def _file_fingerprint(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def _discover_profile_cases(
    input_dir: Path,
    reference_path: Path | None = None,
) -> list[tuple[str, Path, bool, bool]]:
    reference = (reference_path or REFERENCE_PATH).resolve()
    if not reference.is_file():
        raise FileNotFoundError(f"Missing reference case: {reference}")
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Missing input directory: {input_dir}")

    input_paths = sorted(
        (
            path.resolve()
            for path in input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in STEP_SUFFIXES
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )
    if not input_paths:
        raise ValueError(f"No STEP/STP files found in input directory: {input_dir}")

    reference_fingerprint = _file_fingerprint(reference)
    seen_paths: set[Path] = {reference}
    seen_fingerprints: set[tuple[int, str]] = {reference_fingerprint}
    cases: list[tuple[str, Path, bool, bool]] = []
    for path in input_paths:
        fingerprint = _file_fingerprint(path)
        if path in seen_paths or fingerprint in seen_fingerprints:
            continue
        seen_paths.add(path)
        seen_fingerprints.add(fingerprint)
        cases.append((path.name, path, path.name == NEGATIVE_CASE, False))

    cases.append((REFERENCE_NAME, reference, False, True))
    return cases


def _phase_row(
    report_name: str,
    negative_case: bool,
    reference_case: bool,
    call: dict,
) -> dict:
    before = call.get("rss_before_bytes")
    after = call.get("rss_after_bytes")
    started = call.get("started")
    finished = call.get("finished")
    return {
        "file_name": report_name,
        "negative_case": negative_case,
        "reference_case": reference_case,
        "phase": call["phase"],
        "function": call["function"],
        "call_index": call["call_index"],
        "completed": call.get("completed", False),
        "rss_before_mib": round(before / MIB, 2) if before is not None else None,
        "rss_after_mib": round(after / MIB, 2) if after is not None else None,
        "rss_delta_mib": (
            round((after - before) / MIB, 2)
            if before is not None and after is not None else None
        ),
        "peak_rss_mib": round(call.get("peak_bytes", 0) / MIB, 2),
        "duration_sec": (
            round(finished - started, 4)
            if started is not None and finished is not None else None
        ),
        "details_json": json.dumps(call.get("details", {}), ensure_ascii=False),
    }


def _diagnosis(phase_rows: list[dict], final: dict, outcome: str) -> str:
    if outcome != "completed":
        interrupted = next((row for row in reversed(phase_rows) if not row["completed"]), None)
        return (
            f"Analisi interrotta durante {interrupted['phase']}."
            if interrupted else "Analisi interrotta prima di identificare la fase interna."
        )
    deltas = [row for row in phase_rows if row["rss_delta_mib"] is not None]
    slow = [row for row in phase_rows if row["duration_sec"] is not None]
    largest = max(deltas, key=lambda row: row["rss_delta_mib"], default=None)
    slowest = max(slow, key=lambda row: row["duration_sec"], default=None)
    parts = []
    if largest:
        parts.append(
            f"Maggiore crescita RSS: {largest['phase']} ({largest['rss_delta_mib']:+.2f} MiB)."
        )
    if slowest:
        parts.append(
            f"Fase più lenta: {slowest['phase']} ({slowest['duration_sec']:.3f} s)."
        )
    if final.get("classification") == "sheet_metal":
        parts.append("Classificazione sheet_metal: " + str(final.get("classification_reason")))
    if final.get("flat_pattern_status") == "exact" or final.get("usable_for_costing") is True:
        parts.append(
            "Flat: " + str(final.get("flat_pattern_method"))
            + f"; validation_passed={final.get('flat_validation_passed')}."
        )
    return " ".join(parts)


def _run_case(
    api_pid: int,
    step_path: Path,
    report_name: str,
    negative_case: bool,
    reference_case: bool,
    timeout_sec: float,
    max_worker_ram_mib: float,
    sample_interval_sec: float,
) -> tuple[dict, list[dict]]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        str(step_path),
        "--report-name",
        report_name,
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
    calls: dict[str, dict] = {}
    active: list[str] = []
    final: dict = {}
    worker_error: dict = {}
    event_lock = threading.Lock()

    def read_output() -> None:
        assert worker.stdout is not None
        for line in worker.stdout:
            kind, _, raw = line.partition(" ")
            if kind not in {"PHASE_START", "PHASE_END", "ANALYSIS_RESULT", "ANALYSIS_ERROR"}:
                continue
            payload = json.loads(raw)
            with event_lock:
                if kind == "PHASE_START":
                    call_id = payload["call_id"]
                    same_phase_count = sum(
                        item["phase"] == payload["phase"] for item in calls.values()
                    )
                    calls[call_id] = {
                        "phase": payload["phase"],
                        "function": payload["function"],
                        "call_index": same_phase_count + 1,
                        "started": payload["monotonic"],
                        "rss_before_bytes": payload["rss_bytes"],
                        "peak_bytes": payload["rss_bytes"],
                        "details": payload.get("details", {}),
                        "completed": False,
                    }
                    active.append(call_id)
                elif kind == "PHASE_END":
                    call_id = payload["call_id"]
                    if call_id in calls:
                        calls[call_id]["finished"] = payload["monotonic"]
                        calls[call_id]["rss_after_bytes"] = payload["rss_bytes"]
                        calls[call_id]["peak_bytes"] = max(
                            calls[call_id]["peak_bytes"], payload["rss_bytes"]
                        )
                        calls[call_id]["details"].update(payload.get("details", {}))
                        calls[call_id]["completed"] = True
                    if call_id in active:
                        active.remove(call_id)
                elif kind == "ANALYSIS_RESULT":
                    final.update(payload)
                else:
                    worker_error.update(payload)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    peak_worker = 0
    peak_total = 0
    termination_reason: str | None = None
    try:
        while worker.poll() is None:
            now = time.monotonic()
            worker_rss = _tree_rss(worker.pid)
            api_rss = _tree_rss(api_pid)
            peak_worker = max(peak_worker, worker_rss)
            peak_total = max(peak_total, worker_rss + api_rss)
            with event_lock:
                for call_id in active:
                    calls[call_id]["peak_bytes"] = max(
                        calls[call_id]["peak_bytes"], worker_rss
                    )
            if timeout_sec > 0 and now - started >= timeout_sec:
                termination_reason = "timeout"
                break
            if max_worker_ram_mib > 0 and worker_rss / MIB >= max_worker_ram_mib:
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

    ended = time.monotonic()
    with event_lock:
        final_rss = peak_worker
        for call_id in active:
            calls[call_id]["finished"] = ended
            calls[call_id]["rss_after_bytes"] = final_rss
            calls[call_id]["completed"] = False
    residual = _process_group_members(pgid)
    if residual:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        time.sleep(0.1)
        residual = _process_group_members(pgid)

    ordered_calls = sorted(calls.values(), key=lambda item: item["started"])
    phase_rows = [
        _phase_row(report_name, negative_case, reference_case, call)
        for call in ordered_calls
    ]
    if termination_reason == "timeout":
        outcome = "timeout"
        error = f"Timeout after {timeout_sec:g} seconds"
    elif termination_reason == "ram_limit":
        outcome = "ram_limit"
        error = f"Worker exceeded {max_worker_ram_mib:g} MiB"
    elif worker.returncode != 0 or worker_error:
        outcome = "failed"
        error = worker_error.get("message") or f"Worker exit code {worker.returncode}"
    else:
        outcome = "completed"
        error = None
    diagnostic_rows = [
        row for row in phase_rows
        if row["phase"] not in {"analyze_step_file_total", "result_serialization"}
    ]
    completed_rows = [row for row in diagnostic_rows if row["completed"]]
    largest_delta = max(
        completed_rows,
        key=lambda row: row["rss_delta_mib"] if row["rss_delta_mib"] is not None else float("-inf"),
        default=None,
    )
    largest_peak = max(diagnostic_rows, key=lambda row: row["peak_rss_mib"], default=None)
    slowest = max(
        completed_rows,
        key=lambda row: row["duration_sec"] if row["duration_sec"] is not None else -1,
        default=None,
    )
    interrupted = next((row for row in reversed(phase_rows) if not row["completed"]), None)
    file_summary = {
        "file_name": report_name,
        "negative_case": negative_case,
        "reference_case": reference_case,
        "file_size_bytes": step_path.stat().st_size,
        "outcome": outcome,
        "error": error,
        "timeout": termination_reason == "timeout",
        "ram_limit_exceeded": termination_reason == "ram_limit",
        "largest_ram_increase_phase": largest_delta["phase"] if largest_delta else None,
        "largest_ram_increase_mib": largest_delta["rss_delta_mib"] if largest_delta else None,
        "maximum_peak_phase": largest_peak["phase"] if largest_peak else None,
        "maximum_phase_peak_mib": largest_peak["peak_rss_mib"] if largest_peak else None,
        "slowest_phase": slowest["phase"] if slowest else None,
        "slowest_phase_sec": slowest["duration_sec"] if slowest else None,
        "maximum_worker_ram_mib": round(peak_worker / MIB, 2),
        "maximum_api_plus_worker_ram_mib": round(peak_total / MIB, 2),
        "total_time_sec": round(ended - started, 3),
        "interrupted_phase": interrupted["phase"] if interrupted else None,
        "api_after_worker_exit_mib": round(_tree_rss(api_pid) / MIB, 2),
        "residual_process_pids": residual,
        "pair_planar_faces_call_count": sum(
            row["phase"] == "face_graph_planar_pairing" for row in phase_rows
        ),
        "face_graph_reconstruction_count": sum(
            row["phase"] == "topology_context_build" for row in phase_rows
        ),
        "face_graph_access_count": sum(
            row["phase"] == "face_graph_build" for row in phase_rows
        ),
        "analysis": final,
        "diagnosis": _diagnosis(phase_rows, final, outcome),
    }
    return file_summary, phase_rows


def _write_outputs(output_dir: Path, summaries: list[dict], phases: list[dict], settings: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "cad_phase_profile.json"
    csv_path = output_dir / "cad_phase_profile.csv"
    payload = {
        "profile_version": "1.1",
        "tested_total": len(summaries),
        "expected_total": settings["expected_total"],
        "settings": settings,
        "file_summaries": summaries,
        "phases": phases,
    }
    temporary = json_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(json_path)
    temporary_csv = csv_path.with_suffix(".csv.tmp")
    with temporary_csv.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=PHASE_CSV_FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in PHASE_CSV_FIELDS} for row in phases)
    temporary_csv.replace(csv_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase-by-phase CAD memory profiler")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--report-name")
    parser.add_argument("--input-dir", type=Path, default=Path("/input"))
    parser.add_argument("--output-dir", type=Path, default=Path("/output"))
    parser.add_argument("--timeout-sec", type=float, default=300.0)
    parser.add_argument("--max-worker-ram-mib", type=float, default=2300.0)
    parser.add_argument("--sample-interval-sec", type=float, default=0.02)
    args = parser.parse_args()
    os.chdir(PROJECT_ROOT)
    os.environ.update(_project_environment())
    if args.worker:
        return _profile_worker(args.worker.resolve(), args.report_name or args.worker.name)

    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    try:
        cases = _discover_profile_cases(input_dir)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    expected_total = len(cases)

    settings = {
        "sequential": True,
        "expected_total": expected_total,
        "unique_files_total": expected_total,
        "real_files": sum(not reference for _, _, _, reference in cases),
        "negative_cases": sum(negative for _, _, negative, _ in cases),
        "reference_files": sum(reference for _, _, _, reference in cases),
        "timeout_sec": args.timeout_sec,
        "max_worker_ram_mib": args.max_worker_ram_mib,
        "sample_interval_sec": args.sample_interval_sec,
        "source_sha256": {
            "app/cad_analyzer.py": _source_sha256("app/cad_analyzer.py"),
            "app/sheetmetal_unfolder.py": _source_sha256("app/sheetmetal_unfolder.py"),
        },
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
            "18765",
            "--workers",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(PROJECT_ROOT),
        env=_project_environment(),
    )
    summaries: list[dict] = []
    phases: list[dict] = []
    try:
        _wait_for_health(api)
        settings["api_idle_rss_mib"] = round(_tree_rss(api.pid) / MIB, 2)
        for index, (name, path, negative, reference) in enumerate(cases, start=1):
            print(f"[{index}/{expected_total}] Profiling {name}", file=sys.stderr, flush=True)
            summary, case_phases = _run_case(
                api.pid,
                path,
                name,
                negative,
                reference,
                args.timeout_sec,
                args.max_worker_ram_mib,
                args.sample_interval_sec,
            )
            summaries.append(summary)
            phases.extend(case_phases)
            _write_outputs(output_dir, summaries, phases, settings)
            print(
                f"  {summary['outcome']} | {summary['maximum_worker_ram_mib']} MiB | "
                f"{summary['total_time_sec']} s",
                file=sys.stderr,
                flush=True,
            )
    except KeyboardInterrupt:
        print("Profiling interrupted safely; completed data preserved.", file=sys.stderr)
        _write_outputs(output_dir, summaries, phases, settings)
        return 130
    finally:
        _terminate_group(api)
    _write_outputs(output_dir, summaries, phases, settings)
    print(json.dumps({"output_dir": str(output_dir), "tested_total": len(summaries)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
