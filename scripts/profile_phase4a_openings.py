from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import profile_real_steps_batch as runtime_profile


BASELINE_PATH = PROJECT_ROOT / "tests/dataset/phase4a_real_step_opening_baseline.json"
CANONICAL_CASES = tuple(json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["counts"])
CSV_FIELDS = (
    "file_name",
    "old_opening_count",
    "new_opening_count",
    "physical_identity_count",
    "raw_contour_count",
    "accepted_merge_count",
    "rejected_merge_count",
    "merge_reasons",
    "rejected_merge_reasons",
    "unrepresented_raw_contours",
    "peak_worker_mib",
    "peak_api_plus_worker_mib",
    "analyze_step_file_sec",
    "classification",
    "thickness_mm",
    "bend_count",
    "flat_status",
    "usable_for_costing",
    "outcome",
    "error",
    "residual_process_pids",
)


def _resolve_input(input_dir: Path, name: str) -> Path | None:
    exact = input_dir / name
    if exact.is_file():
        return exact
    uploaded = input_dir / name.replace(".STEP", "(1).STEP")
    return uploaded if uploaded.is_file() else None


def _feature_dimensions(feature) -> dict:
    return {
        key: getattr(feature, key)
        for key in (
            "diameter_mm",
            "through_diameter_mm",
            "countersink_major_diameter_mm",
            "overall_length_mm",
            "width_mm",
            "corner_radius_mm",
            "max_dimension_mm",
        )
        if getattr(feature, key) is not None
    }


def _component_contexts(shape, result, parameters):
    from app.cad_analyzer import (
        _build_physical_opening_context,
        _detect_sheet_thickness,
        _stable_solids,
    )
    from app.sheetmetal_unfolder import build_sheet_topology_context

    solids = _stable_solids(shape)
    if len(solids) != 1:
        contexts = []
        for index, solid in enumerate(solids, start=1):
            thickness, _ = _detect_sheet_thickness(solid)
            contexts.append(
                _build_physical_opening_context(
                    solid,
                    parameters,
                    thickness,
                    component_id=f"component_{index:03d}",
                )
            )
        return contexts

    topology = None
    thickness = result.detected_thickness_mm
    if result.part_classification.category == "sheet_metal" and thickness is not None:
        try:
            topology = build_sheet_topology_context(
                shape,
                thickness,
                parameters.flat_pattern_k_factor,
                parameters,
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            topology = None
    return [
        _build_physical_opening_context(
            shape,
            parameters,
            thickness,
            topology_context=topology,
        )
    ]


def _worker(step_path: Path) -> int:
    try:
        from app.cad_analyzer import (
            _classify_physical_opening,
            analyze_step_file,
            get_freecad_status,
            load_analysis_config,
        )

        status = get_freecad_status()
        if not status.available:
            raise RuntimeError(status.error or "FreeCAD unavailable")
        import Part

        file_bytes = step_path.read_bytes()
        print("PROFILE_PHASE analysis_started", flush=True)
        started = time.monotonic()
        result = analyze_step_file(file_bytes=file_bytes, source_file=step_path.name)
        elapsed = time.monotonic() - started
        print("PROFILE_PHASE analysis_completed", flush=True)

        shape = Part.Shape()
        shape.read(str(step_path))
        parameters = load_analysis_config()
        contexts = _component_contexts(shape, result, parameters)
        identities = [identity for context in contexts for identity in context.identities]
        mappings = []
        for context in contexts:
            for identity in context.identities:
                category, feature, _ = _classify_physical_opening(
                    identity,
                    parameters,
                    result.detected_thickness_mm,
                )
                mappings.append(
                    {
                        "physical_opening_id": identity.id,
                        "component_id": identity.component_id,
                        "source_contours": [
                            {
                                "face": contour.face_index,
                                "wire": contour.wire_index,
                            }
                            for contour in identity.contours
                        ],
                        "source_wall_face_count": len(identity.wall_faces),
                        "category": category,
                        "center": feature.center,
                        "axis": feature.axis,
                        "dimensions": _feature_dimensions(feature),
                        "panel": identity.panel_id,
                        "grouping_reason": identity.evidence,
                    }
                )
        accepted = [item for context in contexts for item in context.accepted_merges]
        rejected = [item for context in contexts for item in context.rejected_merges]
        raw_contours = sum(context.raw_contour_count for context in contexts)
        payload = {
            "analyze_step_file_sec": round(elapsed, 3),
            "new_opening_count": result.holes.physical_openings_total,
            "physical_identity_count": len(identities),
            "raw_contour_count": raw_contours,
            "accepted_merge_count": len(accepted),
            "rejected_merge_count": len(rejected),
            "merge_reasons": dict(Counter(item["reason"] for item in accepted)),
            "rejected_merge_reasons": dict(Counter(item["reason"] for item in rejected)),
            "unrepresented_raw_contours": max(
                0,
                raw_contours - sum(len(identity.contours) for identity in identities),
            ),
            "classification": result.part_classification.category,
            "thickness_mm": result.detected_thickness_mm,
            "bend_count": result.bends.count,
            "flat_status": result.flat_pattern.status,
            "usable_for_costing": result.flat_pattern.usable_for_costing,
            "physical_openings": mappings,
        }
        print("PROFILE_RESULT " + json.dumps(payload), flush=True)
        return 0
    except Exception as exc:
        print(
            "PROFILE_ERROR "
            + json.dumps({"type": type(exc).__name__, "message": str(exc)}),
            flush=True,
        )
        return 1


def _run_case(api_pid: int, step_path: Path, old_count: int) -> dict:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(step_path)]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=str(PROJECT_ROOT),
        env=runtime_profile._project_environment(),
    )
    result = {}
    error = {}
    state = {"analysis_active": False}

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            if line.startswith("PROFILE_PHASE "):
                state["analysis_active"] = line.strip().endswith("analysis_started")
            elif line.startswith("PROFILE_RESULT "):
                result.update(json.loads(line.removeprefix("PROFILE_RESULT ")))
            elif line.startswith("PROFILE_ERROR "):
                error.update(json.loads(line.removeprefix("PROFILE_ERROR ")))

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    peak_worker = 0
    peak_total = 0
    try:
        while process.poll() is None:
            if state["analysis_active"]:
                worker_rss = runtime_profile._tree_rss(process.pid)
                api_rss = runtime_profile._tree_rss(api_pid)
                peak_worker = max(peak_worker, worker_rss)
                peak_total = max(peak_total, worker_rss + api_rss)
            time.sleep(0.02)
        process.wait()
    except KeyboardInterrupt:
        runtime_profile._terminate_group(process)
        raise
    finally:
        reader.join(timeout=2)
    residual = runtime_profile._process_group_members(process.pid)
    row = {
        "file_name": step_path.name.replace("(1).STEP", ".STEP"),
        "old_opening_count": old_count,
        "peak_worker_mib": round(peak_worker / runtime_profile.MIB, 2),
        "peak_api_plus_worker_mib": round(peak_total / runtime_profile.MIB, 2),
        "residual_process_pids": residual,
        "outcome": "completed" if process.returncode == 0 and result else "failed",
        "error": error.get("message"),
        **result,
    }
    return row


def _write_outputs(output_dir: Path, rows: list[dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "profile": "Phase 4A physical opening identity",
        "results": rows,
    }
    (output_dir / "phase4a_opening_comparison.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with (output_dir / "phase4a_opening_comparison.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            csv_row = {key: row.get(key) for key in CSV_FIELDS}
            csv_row["merge_reasons"] = json.dumps(row.get("merge_reasons", {}), ensure_ascii=False)
            csv_row["rejected_merge_reasons"] = json.dumps(
                row.get("rejected_merge_reasons", {}), ensure_ascii=False
            )
            csv_row["residual_process_pids"] = json.dumps(
                row.get("residual_process_pids", [])
            )
            writer.writerow(csv_row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4A opening before/after profiler")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--input-dir", type=Path, default=Path("/input"))
    parser.add_argument("--output-dir", type=Path, default=Path("/output"))
    args = parser.parse_args()
    if args.worker:
        return _worker(args.worker.resolve())

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["counts"]
    resolved = {name: _resolve_input(args.input_dir, name) for name in CANONICAL_CASES}
    missing = [name for name, path in resolved.items() if path is None]
    if missing:
        parser.error("Missing Phase 4A inputs: " + "; ".join(missing))

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
        env=runtime_profile._project_environment(),
    )
    rows = []
    try:
        runtime_profile._wait_for_health(api)
        for index, name in enumerate(CANONICAL_CASES, start=1):
            print(f"[{index}/{len(CANONICAL_CASES)}] {name}", file=sys.stderr, flush=True)
            rows.append(_run_case(api.pid, resolved[name], baseline[name]))
            _write_outputs(args.output_dir, rows)
    except KeyboardInterrupt:
        _write_outputs(args.output_dir, rows)
        return 130
    finally:
        runtime_profile._terminate_group(api)
    _write_outputs(args.output_dir, rows)
    print(json.dumps({"tested_total": len(rows), "output_dir": str(args.output_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
