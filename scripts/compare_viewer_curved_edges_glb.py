#!/usr/bin/env python3
"""Compare GLB buffers before and after curved-edge sampling on real STEPs.

Run in FreeCAD Docker. The JSON reports precise changes rather than treating
different tessellated positions or normals as automatically equivalent.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import model_exporter as current  # noqa: E402
from scripts import _viewer_before_edge_refinement as previous  # noqa: E402
from scripts.compare_viewer_normals_glb import _buffers  # noqa: E402
from scripts.profile_viewer_v1 import DEFAULT_CASES  # noqa: E402

CASES = (("plate", ROOT / "tests/dataset/lamiera_piana_test_1/input.stp", "normal"),
         *DEFAULT_CASES)


def _export(module, file):
    progress = []
    start = time.perf_counter()
    payload = module.export_step_to_glb(str(file),
                                        progress=lambda phase, data: progress.append((phase, data)))
    if not payload.get("available"):
        raise RuntimeError("Fixed repository STEP failed to export")
    completed = next((data for phase, data in reversed(progress)
                      if phase == "export_completed"), {})
    edge_stats = next((data for phase, data in reversed(progress)
                       if phase == "glb_assembly"), {})
    mesh_stats = next((data for phase, data in reversed(progress)
                       if phase == "brep_edges"), {})
    return (base64.b64decode(payload["model_base64"]),
            round(time.perf_counter() - start, 6), completed,
            edge_stats.get("edge_segment_count"), mesh_stats.get("triangle_count"))


def compare(name, file, mode):
    keys = ("VIEWER_MODEL_MAX_TRIANGLES", "VIEWER_MODEL_TESSELLATION_RATIO",
            "VIEWER_MODEL_CURVED_FACE_REFINEMENT")
    saved = {key: os.environ.get(key) for key in keys}
    values = ("50000", "300", "0.8") if mode == "high" else ("120000", "650", "0.65")
    try:
        os.environ.update(zip(keys, values))
        old, old_sec, old_times, old_edges, old_triangles = _export(previous, file)
        new, new_sec, new_times, new_edges, new_triangles = _export(current, file)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    a, b = _buffers(old), _buffers(new)
    return {"case": name, "mode": mode,
            "whole_glb_byte_equal": old == new,
            "edge_segments_before": old_edges, "edge_segments_after": new_edges,
            "triangles_before": old_triangles, "triangles_after": new_triangles,
            "elapsed_before_sec": old_sec, "elapsed_after_sec": new_sec,
            "phase_sec_before": {k: round(old_times.get(k, 0), 6) for k in
                                 ("tessellation_sec", "occ_normals_sec", "brep_edges_sec")},
            "phase_sec_after": {k: round(new_times.get(k, 0), 6) for k in
                                ("tessellation_sec", "occ_normals_sec", "brep_edges_sec")},
            "boundary_refined_faces": new_times.get("boundary_refined_faces"),
            "boundary_unrefined_faces": new_times.get("boundary_unrefined_faces"),
            "buffers": {key: {"byte_equal": a[key] == b[key],
                               "size_before": len(a[key]), "size_after": len(b[key]),
                               "sha256_before": hashlib.sha256(a[key]).hexdigest(),
                               "sha256_after": hashlib.sha256(b[key]).hexdigest()}
                        for key in ("positions", "normals", "indices", "brep_edges")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=[case[0] for case in CASES], action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = [compare(name, file, mode) for name, file, mode in CASES
               if not args.case or name in args.case]
    output = json.dumps(results, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
