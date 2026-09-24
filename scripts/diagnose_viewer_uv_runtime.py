#!/usr/bin/env python3
"""Diagnose FreeCAD mesh-UV rejection without printing input paths or content.

Run inside the project's Docker image. No production code is modified. Output
contains only fixed case labels, API availability, numeric counts and errors.
"""

import collections
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import model_exporter as exporter  # noqa: E402


def diagnose():
    exporter._configure_freecad_path()
    import FreeCAD
    import Part

    shape = Part.Shape()
    shape.read(str(ROOT / "tests/dataset/staffa_16_pieghe_stress_test/input.stp"))
    box = shape.BoundBox
    diagonal = math.sqrt(sum(x * x for x in
                             (box.XLength, box.YLength, box.ZLength)))
    deflection = max(diagonal / 300., .04) * .8
    results = []
    surface_types = collections.Counter(type(face.Surface).__name__
                                        for face in shape.Faces)
    for index, face in enumerate(shape.Faces, 1):
        surface_type = type(face.Surface).__name__.lower()
        if surface_type not in {"bsplinesurface", "geombsplinesurface"}:
            continue
        points, facets = face.tessellate(deflection)
        if not points or not facets:
            continue
        row = {"face": index, "surface": surface_type,
               "vertices": len(points), "triangles": len(facets),
               "has_get_uv_nodes": callable(getattr(face, "getUVNodes", None)),
               "has_value_at": callable(getattr(face, "valueAt", None))}
        try:
            uvs = face.getUVNodes()
            row["uv_nodes"] = len(uvs)
        except Exception as exc:
            row["reason"] = "get_uv_exception"
            row["exception_type"] = type(exc).__name__
            results.append(row)
            continue
        if len(uvs) != len(points):
            row["reason"] = "node_count_mismatch"
            results.append(row)
            continue
        try:
            tolerance = min(.1, max(1e-5, deflection * .1))
            distances = []
            invalid_uv = 0
            for point, pair in zip(points, uvs):
                u, v = float(pair[0]), float(pair[1])
                if not math.isfinite(u) or not math.isfinite(v):
                    invalid_uv += 1
                    continue
                surface_point = face.valueAt(u, v)
                distances.append(math.dist(
                    (point.x, point.y, point.z),
                    (surface_point.x, surface_point.y, surface_point.z),
                ))
            row["invalid_uv"] = invalid_uv
            row["distance_max_mm"] = max(distances, default=None)
            row["distance_p50_mm"] = sorted(distances)[len(distances)//2] if distances else None
            row["distance_over_tolerance"] = sum(d > tolerance for d in distances)
            row["tolerance_mm"] = tolerance
            row["reason"] = ("invalid_uv" if invalid_uv else
                             "node_order_or_coordinates" if row["distance_over_tolerance"] else
                             "accepted")
        except Exception as exc:
            row["reason"] = "uv_validation_exception"
            row["exception_type"] = type(exc).__name__
        results.append(row)
    by_reason = collections.Counter(row["reason"] for row in results)
    by_reason_vertices = collections.Counter()
    for row in results:
        by_reason_vertices[row["reason"]] += row["vertices"]
    return {"freecad_version": str(FreeCAD.Version()[:3]),
            "case": "staffa_16_pieghe_stress_test", "faces": len(results),
            "deflection_mm": deflection, "reason_faces": dict(by_reason),
            "reason_vertices": dict(by_reason_vertices),
            "surface_types": dict(surface_types), "per_face": results}


if __name__ == "__main__":
    print(json.dumps(diagnose(), indent=2))
