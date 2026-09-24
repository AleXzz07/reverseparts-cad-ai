#!/usr/bin/env python3
"""Per-face FreeCAD OCC normal profile. Prints no STEP path or STEP contents.

Run in the application's FreeCAD Docker image. This is diagnostic code only:
it does not replace production normals or modify STEP/CAD analysis.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import model_exporter as exporter  # noqa: E402
from scripts.profile_viewer_v1 import DEFAULT_CASES, _settings  # noqa: E402


def _clock_call(fn, totals, key):
    start = time.perf_counter()
    try:
        return fn()
    finally:
        totals[key] = totals.get(key, 0.0) + time.perf_counter() - start


def profile_case(case, path, complexity):
    exporter._configure_freecad_path()
    import FreeCAD  # noqa: F401 - initialize before Part
    import Part

    shape = Part.Shape()
    shape.read(str(path))
    bbox = shape.BoundBox
    diagonal = math.sqrt(sum(v * v for v in
                             (bbox.XLength, bbox.YLength, bbox.ZLength)))
    _, ratio, refinement = _settings(complexity)
    deflection = max(diagonal / ratio, 0.04)
    rows = []
    for index, face in enumerate(shape.Faces, 1):
        totals = {}
        surface = _clock_call(lambda: face.Surface, totals, "surface_access_sec")
        kind = type(surface).__name__
        step = deflection if exporter._is_planar_face(face) else deflection * refinement
        vertices, facets = _clock_call(lambda: face.tessellate(step), totals, "tessellation_sec")
        if not vertices or not facets:
            continue
        points = [exporter._vector(v) for v in vertices]
        indices = [tuple(int(i) for i in f) for f in facets if len(f) == 3]
        # This is the previous exporter path: property lookup and projection
        # per mesh node, with OCC normalAt, plus eager mesh fallback.
        _clock_call(lambda: exporter._vertex_normals(points, indices),
                    totals, "eager_fallback_sec")
        failures = 0
        projection_failures = 0
        if exporter._is_planar_face(face):
            nodes = vertices[:1]
        else:
            nodes = vertices
        normals = []
        setup_surface_access = totals.get("surface_access_sec", 0.0)
        old_start = time.perf_counter()
        for node in nodes:
            try:
                wrapper = _clock_call(lambda: face.Surface, totals, "surface_access_sec")
                try:
                    uv = _clock_call(lambda: wrapper.parameter(node), totals,
                                     "projection_sec")
                except Exception:
                    projection_failures += 1
                    raise
                n = _clock_call(lambda: face.normalAt(*uv), totals, "normal_at_sec")
                normals.append(exporter._normalize(exporter._vector(n)))
            except Exception:
                failures += 1
                normals.append(None)
        totals["old_normal_loop_sec"] = time.perf_counter() - old_start

        # Candidate operations measured separately, without changing the
        # baseline cost accounting or classifying a B-spline as a plane.
        uv_source = None
        if kind.lower() in {"bsplinesurface", "geombsplinesurface"}:
            try:
                uv_source = _clock_call(face.getUVNodes, totals, "uv_fetch_sec")
                if len(uv_source) == len(points):
                    for uv in uv_source:
                        _clock_call(lambda uv=uv: face.valueAt(*uv), totals,
                                    "uv_position_check_sec")
                else:
                    uv_source = None
            except Exception:
                uv_source = None
        row = dict(face=index, surface=kind, vertices=len(vertices),
                   triangles=len(indices), failed_normals=failures,
                   projection_failures=projection_failures,
                   fallback_vertices=(len(vertices) if
                                      exporter._is_planar_face(face) and failures else failures),
                   uv_nodes=len(uv_source) if uv_source is not None else 0)
        row.update({key: round(value, 7) for key, value in totals.items()})
        row["other_normal_loop_sec"] = round(max(0.0, totals["old_normal_loop_sec"] -
            totals.get("projection_sec", 0) - totals.get("normal_at_sec", 0) -
            (totals.get("surface_access_sec", 0) - setup_surface_access)), 7)
        rows.append(row)
    by_surface = {}
    for row in rows:
        summary = by_surface.setdefault(row["surface"], {"faces": 0})
        summary["faces"] += 1
        for key, value in row.items():
            if key.endswith("_sec") or key in {"vertices", "triangles", "fallback_vertices",
                                               "failed_normals", "projection_failures", "uv_nodes"}:
                summary[key] = round(summary.get(key, 0) + value, 7)
    return {"case": case, "complexity": complexity, "faces": len(rows),
            "by_surface": by_surface, "per_face": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=[case for case, _, _ in DEFAULT_CASES])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = [item for item in DEFAULT_CASES if not args.case or item[0] in args.case]
    results = [profile_case(case, path, mode) for case, path, mode in cases]
    payload = json.dumps(results, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
