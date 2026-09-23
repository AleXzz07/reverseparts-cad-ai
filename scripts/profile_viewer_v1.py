#!/usr/bin/env python3
"""Compare legacy, frozen V1, published V1.1 and shading correction.

This diagnostic never calls the CAD analysis, unfold, quote, or PDF paths.
Each measurement runs in a fresh subprocess so peak RSS is attributable to a
single STEP export and is released after every case.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import math
import os
import resource
import signal
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import model_exporter as optimized  # noqa: E402
from scripts import _viewer_v1_baseline as baseline  # noqa: E402
from scripts import _viewer_v1_1_before_shading as published  # noqa: E402

_configure_freecad_path = optimized._configure_freecad_path
_vector = optimized._vector


DEFAULT_CASES = (
    (
        "staffa_semplice",
        PROJECT_ROOT / "tests/dataset/staffa_1_piega_test_1/input.stp",
        "normal",
    ),
    (
        "staffa_superfici_curve",
        PROJECT_ROOT / "tests/dataset/staffa_u_test_1/input.stp",
        "normal",
    ),
    (
        "staffa_16_pieghe",
        PROJECT_ROOT / "tests/dataset/staffa_16_pieghe_stress_test/input.stp",
        "high",
    ),
)


def _peak_rss_mib() -> float:
    # Linux reports ru_maxrss in KiB; the Docker verification runs on Linux.
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _settings(complexity: str) -> tuple[int, float, float]:
    if complexity == "high":
        default_triangles, default_ratio, default_refinement = 50000, 300.0, 0.8
    else:
        default_triangles, default_ratio, default_refinement = 120000, 650.0, 0.65
    return (
        max(1000, int(os.getenv("VIEWER_MODEL_MAX_TRIANGLES", default_triangles))),
        max(1.0, _env_float("VIEWER_MODEL_TESSELLATION_RATIO", default_ratio)),
        min(
            1.0,
            max(
                0.4,
                _env_float(
                    "VIEWER_MODEL_CURVED_FACE_REFINEMENT",
                    default_refinement,
                ),
            ),
        ),
    )


def _cgroup_memory_limit_mib() -> float | None:
    candidates = (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    )
    for candidate in candidates:
        try:
            raw = candidate.read_text(encoding="ascii").strip()
            if raw == "max":
                return None
            value = int(raw)
            # Very large v1 values represent an unlimited cgroup.
            if value >= (1 << 60):
                return None
            return round(value / (1024 * 1024), 2)
        except (OSError, ValueError):
            continue
    return None


def _write_worker_snapshot(output_path: Path, result: dict[str, Any]) -> None:
    """Persist the last safe phase even if a later native call crashes."""

    temporary = output_path.with_suffix(output_path.suffix + ".partial")
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temporary, output_path)


def _load_shape(
    step_path: Path,
    mark_phase: Any,
) -> tuple[Any, float, float]:
    mark_phase("configure_freecad_path")
    _configure_freecad_path()
    # The production exporter initializes the FreeCAD runtime before Part.
    # Importing Part first can abort the native process without a Python
    # traceback on some Ubuntu/FreeCAD builds.
    mark_phase("import_freecad")
    importlib.import_module("FreeCAD")
    mark_phase("import_part")
    Part = importlib.import_module("Part")

    mark_phase("load_step")
    started = time.perf_counter()
    shape = Part.Shape()
    shape.read(str(step_path))
    elapsed = time.perf_counter() - started
    if shape.isNull():
        raise ValueError("FreeCAD imported an empty shape.")
    bbox = shape.BoundBox
    diagonal = math.sqrt(
        float(bbox.XLength) ** 2
        + float(bbox.YLength) ** 2
        + float(bbox.ZLength) ** 2
    )
    return shape, diagonal, elapsed


def _glb_buffer_hashes(glb: bytes) -> dict[str, str]:
    """Hash raw accessor buffers, excluding JSON padding and GLB headers."""
    json_size = struct.unpack_from("<I", glb, 12)[0]
    document = json.loads(glb[20:20 + json_size])
    binary_start = 20 + json_size + 8
    names = ("positions", "normals", "indices", "brep_edges")
    hashes = {}
    for name, view in zip(names, document["bufferViews"]):
        start = binary_start + view["byteOffset"]
        end = start + view["byteLength"]
        hashes[name] = hashlib.sha256(glb[start:end]).hexdigest()
    index_view = document["bufferViews"][2]
    index_start = binary_start + index_view["byteOffset"]
    index_count = document["accessors"][2]["count"]
    index_values = struct.unpack_from(f"<{index_count}I", glb, index_start)
    canonical = hashlib.sha256()
    for offset in range(0, index_count, 3):
        canonical.update(struct.pack("<3I", *sorted(index_values[offset:offset + 3])))
    hashes["undirected_triangles"] = canonical.hexdigest()
    hashes["whole_glb"] = hashlib.sha256(glb).hexdigest()
    return hashes


def _profiled_tessellation(
    module: Any,
    shape: Any,
    deflection: float,
    curved_refinement: float,
) -> tuple[list[Any], list[Any], list[Any], float, float]:
    """Time the original normal routine without changing its calculations."""
    normal_time = 0.0
    original = module._analytic_face_normals

    def timed_normals(*args: Any, **kwargs: Any) -> Any:
        nonlocal normal_time
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            normal_time += time.perf_counter() - started

    module._analytic_face_normals = timed_normals
    started = time.perf_counter()
    try:
        points, facets, normals = module._tessellate_brep_faces(
            shape, deflection, curved_refinement=curved_refinement,
        )
    finally:
        module._analytic_face_normals = original
    elapsed = time.perf_counter() - started
    return points, facets, normals, max(0.0, elapsed - normal_time), normal_time


def _legacy_export(
    shape: Any,
    diagonal: float,
    complexity: str,
    mark_phase: Any,
) -> dict[str, Any]:
    max_triangles, ratio, _ = _settings(complexity)
    deflection = max(diagonal / ratio, 0.04)
    vertices: list[Any] = []
    raw_facets: list[Any] = []
    mark_phase("legacy_tessellation")
    tessellation_started = time.perf_counter()
    for _ in range(5):
        vertices, raw_facets = shape.tessellate(deflection)
        if len(raw_facets) <= max_triangles:
            break
        deflection *= 1.7
    points = [_vector(vertex) for vertex in vertices]
    facets = [
        tuple(int(index) for index in facet)
        for facet in raw_facets
        if len(facet) == 3
    ]
    if len(facets) > max_triangles:
        raise ValueError("legacy mesh exceeds triangle cap")
    tessellation_sec = time.perf_counter() - tessellation_started
    mark_phase("legacy_glb_build")
    assembly_started = time.perf_counter()
    glb = baseline._build_glb(points, facets)
    return {
        "triangle_count": len(facets),
        "vertex_count": len(points),
        "brep_edge_segment_count": 0,
        "deflection_mm": deflection,
        "glb_size_bytes": len(glb),
        "tessellation_sec": round(tessellation_sec, 6),
        "occ_normals_sec": None,  # Legacy normals are computed during GLB assembly.
        "brep_edges_sec": None,
        "glb_assembly_sec": round(time.perf_counter() - assembly_started, 6),
        "buffer_sha256": _glb_buffer_hashes(glb),
    }


def _v1_export(
    shape: Any,
    diagonal: float,
    complexity: str,
    mark_phase: Any,
    *,
    module: Any = optimized,
) -> dict[str, Any]:
    max_triangles, ratio, curved_refinement = _settings(complexity)
    deflection = max(diagonal / ratio, 0.04)
    points = []
    facets = []
    normals = []
    prefix = ("v1" if module is baseline else
              "v1_1" if module is published else "shading_fix")
    mark_phase(f"{prefix}_face_tessellation_and_normals")
    tessellation_sec = 0.0
    normal_sec = 0.0
    for _ in range(5):
        points, facets, normals, tess_elapsed, norm_elapsed = _profiled_tessellation(
            module, shape, deflection, curved_refinement,
        )
        tessellation_sec += tess_elapsed
        normal_sec += norm_elapsed
        if len(facets) <= max_triangles:
            break
        deflection *= 1.7
    if len(facets) > max_triangles:
        raise ValueError("V1 mesh exceeds triangle cap")
    mark_phase(f"{prefix}_brep_edge_export")
    edges_started = time.perf_counter()
    edge_segments = module._extract_brep_edge_segments(
        shape,
        deflection=max(deflection * 0.5, 0.02),
        diagonal=diagonal,
    )
    edges_sec = time.perf_counter() - edges_started
    mark_phase(f"{prefix}_glb_build")
    assembly_started = time.perf_counter()
    glb = module._build_glb(
        points,
        facets,
        normals=normals,
        edge_segments=edge_segments,
    )
    assembly_sec = time.perf_counter() - assembly_started
    return {
        "triangle_count": len(facets),
        "vertex_count": len(points),
        "brep_edge_segment_count": len(edge_segments),
        "deflection_mm": deflection,
        "glb_size_bytes": len(glb),
        "tessellation_sec": round(tessellation_sec, 6),
        "occ_normals_sec": round(normal_sec, 6),
        "brep_edges_sec": round(edges_sec, 6),
        "glb_assembly_sec": round(assembly_sec, 6),
        "buffer_sha256": _glb_buffer_hashes(glb),
    }


def _worker(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    step_path = Path(args.step).resolve()
    output_path = Path(args.worker_output)
    max_triangles, ratio, curved_refinement = _settings(args.complexity)
    result: dict[str, Any] = {
        "case": args.case,
        "step_path": str(step_path),
        "mode": args.mode,
        "complexity": args.complexity,
        "status": "running",
        "phase": "worker_started",
        "error": None,
        "traceback": None,
        "worker_pid": os.getpid(),
        "step_exists": step_path.is_file(),
        "step_size_bytes": step_path.stat().st_size if step_path.is_file() else None,
        "cgroup_memory_limit_mib": _cgroup_memory_limit_mib(),
        "export_settings": {
            "max_triangles": max_triangles,
            "tessellation_ratio": ratio,
            "curved_face_refinement": curved_refinement,
            "minimum_deflection_mm": 0.04,
            "retry_count": 5,
            "retry_deflection_multiplier": 1.7,
        },
    }

    def mark_phase(phase: str) -> None:
        result["phase"] = phase
        result["elapsed_at_phase_sec"] = round(time.perf_counter() - started, 6)
        result["peak_rss_at_phase_mib"] = round(_peak_rss_mib(), 2)
        _write_worker_snapshot(output_path, result)

    mark_phase("validate_step_path")
    try:
        if not step_path.is_file():
            raise FileNotFoundError(f"STEP fixture does not exist: {step_path}")
        shape, diagonal, load_sec = _load_shape(step_path, mark_phase)
        generation_started = time.perf_counter()
        if args.mode == "legacy":
            metrics = _legacy_export(shape, diagonal, args.complexity, mark_phase)
        else:
            module = (baseline if args.mode == "v1" else
                      published if args.mode == "v1_1" else optimized)
            metrics = _v1_export(
                shape, diagonal, args.complexity, mark_phase, module=module,
            )
        result.update(metrics)
        result.update(
            {
                "status": "completed",
                "step_load_sec": round(load_sec, 6),
                "model_generation_sec": round(
                    time.perf_counter() - generation_started,
                    6,
                ),
            }
        )
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    result["total_sec"] = round(time.perf_counter() - started, 6)
    result["peak_worker_rss_mib"] = round(_peak_rss_mib(), 2)
    if result["status"] == "completed":
        result["phase"] = "completed"
    _write_worker_snapshot(output_path, result)
    return 0 if result["status"] == "completed" else 1


def _read_worker_payload(output_path: Path) -> dict[str, Any]:
    try:
        if output_path.stat().st_size:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _signal_details(returncode: int | None) -> tuple[int | None, str | None]:
    if returncode is None or returncode >= 0:
        return (None, None)
    signal_number = -returncode
    try:
        signal_name = signal.Signals(signal_number).name
    except ValueError:
        signal_name = f"SIGNAL_{signal_number}"
    return (signal_number, signal_name)


def _decode_timeout_stream(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _merge_process_diagnostics(
    payload: dict[str, Any],
    *,
    command: list[str],
    returncode: int | None,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    signal_number, signal_name = _signal_details(returncode)
    payload.update(
        {
            "worker_command": command,
            "worker_exit_code": returncode,
            "worker_signal": signal_number,
            "worker_signal_name": signal_name,
            "worker_stdout": stdout,
            "worker_stderr": stderr,
        }
    )
    if returncode not in (0, None):
        phase = payload.get("phase") or "before_first_snapshot"
        payload["status"] = "failed"
        if not payload.get("error"):
            if signal_name:
                payload["error"] = (
                    f"worker terminated by {signal_name} ({signal_number}) "
                    f"during {phase}"
                )
            else:
                payload["error"] = (
                    f"worker exited with code {returncode} during {phase}"
                )
    elif payload.get("status") != "completed":
        phase = payload.get("phase") or "before_first_snapshot"
        payload["status"] = "failed"
        if not payload.get("error"):
            payload["error"] = (
                "worker exited without a completed result "
                f"during {phase}"
            )
    return payload


def _run_one(
    case: str,
    step_path: Path,
    complexity: str,
    mode: str,
    timeout_sec: float,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        output_path = Path(handle.name)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--case",
        case,
        "--step",
        str(step_path),
        "--complexity",
        complexity,
        "--mode",
        mode,
        "--worker-output",
        str(output_path),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        payload = _read_worker_payload(output_path) or {
            "case": case,
            "step_path": str(step_path),
            "mode": mode,
            "complexity": complexity,
            "status": "failed",
            "phase": "before_first_snapshot",
            "error": None,
        }
        return _merge_process_diagnostics(
            payload,
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
    except subprocess.TimeoutExpired as exc:
        payload = _read_worker_payload(output_path) or {
            "case": case,
            "step_path": str(step_path),
            "mode": mode,
            "complexity": complexity,
            "phase": "before_first_snapshot",
        }
        payload.update(
            {
                "status": "timeout",
                "error": f"timeout after {timeout_sec:g} seconds",
                "worker_command": command,
                "worker_exit_code": None,
                "worker_signal": None,
                "worker_signal_name": None,
                "worker_stdout": _decode_timeout_stream(exc.stdout),
                "worker_stderr": _decode_timeout_stream(exc.stderr),
            }
        )
        return payload
    finally:
        output_path.unlink(missing_ok=True)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "case",
        "mode",
        "complexity",
        "status",
        "triangle_count",
        "vertex_count",
        "brep_edge_segment_count",
        "deflection_mm",
        "glb_size_bytes",
        "step_load_sec",
        "tessellation_sec",
        "occ_normals_sec",
        "brep_edges_sec",
        "glb_assembly_sec",
        "model_generation_sec",
        "total_sec",
        "peak_worker_rss_mib",
        "cgroup_memory_limit_mib",
        "phase",
        "worker_exit_code",
        "worker_signal_name",
        "worker_stderr",
        "traceback",
        "error",
        "step_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _equivalence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparisons = []
    for case, _, _ in DEFAULT_CASES:
        by_mode = {row["mode"]: row for row in rows if row["case"] == case}
        before, after = by_mode["v1_1"], by_mode["shading_fix"]
        if before["status"] != "completed" or after["status"] != "completed":
            comparisons.append({"case": case, "status": "unavailable"})
            continue
        keys = ("positions", "normals", "indices", "brep_edges",
                "undirected_triangles", "whole_glb")
        checks = {
            key: before["buffer_sha256"].get(key) == after["buffer_sha256"].get(key)
            for key in keys
        }
        counts_equal = all(
            before[key] == after[key]
            for key in ("triangle_count", "vertex_count", "brep_edge_segment_count")
        )
        geometry_equal = all(checks[key] for key in (
            "positions", "brep_edges", "undirected_triangles",
        ))
        comparisons.append({
            "case": case,
            "status": "pass" if geometry_equal and counts_equal else "mismatch",
            "byte_identical": checks,
            "geometry_equal": geometry_equal,
            "normal_and_winding_changes_intentional": True,
            "counts_equal": counts_equal,
        })
    return comparisons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="viewer-shading-profile")
    parser.add_argument("--timeout-sec", type=float, default=180.0)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--case")
    parser.add_argument("--step")
    parser.add_argument("--complexity", choices=("normal", "high"), default="normal")
    parser.add_argument("--mode", choices=("legacy", "v1", "v1_1", "shading_fix"))
    parser.add_argument("--worker-output")
    args = parser.parse_args()
    if args.worker:
        return _worker(args)

    missing = [str(path) for _, path, _ in DEFAULT_CASES if not path.is_file()]
    if missing:
        parser.error("Missing STEP fixtures: " + "; ".join(missing))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for case, step_path, complexity in DEFAULT_CASES:
        for mode in ("legacy", "v1", "v1_1", "shading_fix"):
            rows.append(
                _run_one(
                    case,
                    step_path,
                    complexity,
                    mode,
                    args.timeout_sec,
                )
            )
    report = {
        "profile": "viewer-shading-four-way",
        "cases": rows,
        "published_v1_1_vs_shading_fix": _equivalence(rows),
        "v1_baseline_sha256": hashlib.sha256(
            Path(baseline.__file__).read_bytes()
        ).hexdigest(),
        "published_v1_1_sha256": hashlib.sha256(
            Path(published.__file__).read_bytes()
        ).hexdigest(),
        "limits": {
            "normal": dict(
                zip(
                    (
                        "max_triangles",
                        "tessellation_ratio",
                        "curved_face_refinement",
                    ),
                    _settings("normal"),
                )
            ),
            "high": dict(
                zip(
                    (
                        "max_triangles",
                        "tessellation_ratio",
                        "curved_face_refinement",
                    ),
                    _settings("high"),
                )
            ),
            "target_peak_worker_rss_mib": 512,
            "container_memory_limit_mib": _cgroup_memory_limit_mib(),
        },
        "runtime": {
            "python_executable": sys.executable,
            "project_root": str(PROJECT_ROOT),
            "inputs": [
                {
                    "case": case,
                    "step_path": str(step_path),
                    "exists": step_path.is_file(),
                    "size_bytes": step_path.stat().st_size,
                }
                for case, step_path, _ in DEFAULT_CASES
            ],
        },
        "notes": [
            "step_load_sec measures FreeCAD STEP import.",
            "Tessellation and normal phase times are split by timing the face-normal routine; normal phase includes OCC, face-local fallback and alignment.",
            "Legacy computes mesh normals inside GLB assembly and has no OCC normals or B-Rep edges.",
            "model_generation_sec includes phase markers, counters, hashing and other Python overhead; phase times need not sum exactly to it.",
            "V1 and published V1.1 use frozen copies of their exporters. Published V1.1 vs shading_fix gate compares raw positions, CAD edges, and triangle indices ignoring winding; normals, winding, and complete GLB hashes may change intentionally.",
            "Browser network transfer and GLTFLoader parsing are not measured in Docker.",
        ],
    }
    json_path = output_dir / "viewer_shading_profile.json"
    csv_path = output_dir / "viewer_shading_profile.csv"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _write_csv(csv_path, rows)
    print(json.dumps(report, indent=2))
    return 0 if (
        all(row["status"] == "completed" for row in rows)
        and all(item["status"] == "pass" and item["counts_equal"]
                for item in report["published_v1_1_vs_shading_fix"])
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
