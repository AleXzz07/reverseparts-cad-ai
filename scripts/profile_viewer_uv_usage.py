#!/usr/bin/env python3
"""Measure OCC UV association per face and the complete high-quality export.

Runs in the FreeCAD Docker image on a fixed, public repository fixture. The
output includes counts and timings, never file paths or source STEP content.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import resource
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import model_exporter as exporter  # noqa: E402

FIXTURE = ROOT / "tests/dataset/staffa_16_pieghe_stress_test/input.stp"


def main() -> None:
    exporter._configure_freecad_path()
    import FreeCAD  # noqa: F401 - initialize before Part
    import Part

    shape = Part.Shape()
    shape.read(str(FIXTURE))
    box = shape.BoundBox
    deflection = max(math.sqrt(sum(v * v for v in
                                   (box.XLength, box.YLength, box.ZLength))) / 300., .04)
    rows = []
    for index, face in enumerate(shape.Faces, 1):
        if type(face.Surface).__name__.lower() not in {"bsplinesurface", "geombsplinesurface"}:
            continue
        step = deflection * .8
        vertices, triangles = face.tessellate(step)
        if not vertices or not triangles:
            continue
        points = [exporter._vector(vertex) for vertex in vertices]
        start = time.perf_counter()
        matched = exporter._verified_tessellation_uv_nodes(face, points, step)
        association_sec = time.perf_counter() - start
        reused = sum(uv is not None for uv in matched) if matched is not None else 0
        start = time.perf_counter()
        exporter._analytic_face_normals(face, vertices, points, triangles, matched)
        normal_sec = time.perf_counter() - start
        rows.append({"face": index, "vertices": len(vertices),
                     "uv_reused_vertices": reused,
                     "uv_fallback_vertices": len(vertices) - reused,
                     "uv_association_sec": round(association_sec, 6),
                     "normal_evaluation_sec": round(normal_sec, 6)})

    import os
    settings = {"VIEWER_MODEL_MAX_TRIANGLES": "50000",
                "VIEWER_MODEL_TESSELLATION_RATIO": "300",
                "VIEWER_MODEL_CURVED_FACE_REFINEMENT": "0.8"}
    os.environ.update(settings)
    snapshots = []
    start = time.perf_counter()
    result = exporter.export_step_to_glb(
        str(FIXTURE), progress=lambda phase, timing: snapshots.append((phase, timing)),
    )
    export_sec = time.perf_counter() - start
    completed = next((values for phase, values in reversed(snapshots)
                      if phase == "export_completed"), {})
    fields = ("step_load_sec", "tessellation_sec", "occ_normals_sec",
              "uv_association_sec", "uv_association_last_attempt_sec",
              "uv_reused_face_count", "uv_reused_vertices", "uv_fallback_vertices",
              "brep_edges_sec", "glb_assembly_sec")
    print(json.dumps({
        "case": "staffa_16_pieghe_stress_test", "mode": "high",
        "bspline_faces": len(rows), "bspline_faces_optimized": sum(
            row["uv_reused_vertices"] > 0 for row in rows),
        "bspline_vertices": sum(row["vertices"] for row in rows),
        "uv_reused_vertices": sum(row["uv_reused_vertices"] for row in rows),
        "uv_fallback_vertices": sum(row["uv_fallback_vertices"] for row in rows),
        "per_face": rows,
        "complete_export": {"available": result.get("available"),
                            "elapsed_sec": round(export_sec, 6),
                            "phase": snapshots[-1][0] if snapshots else None,
                            **{key: completed.get(key) for key in fields},
                            "peak_rss_mib": round(resource.getrusage(
                                resource.RUSAGE_SELF).ru_maxrss / 1024., 2)},
    }, indent=2))
    if not result.get("available"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
