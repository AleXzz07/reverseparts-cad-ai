#!/usr/bin/env python3
"""Inspect cached vs cleaned FreeCAD 0.19 triangulations on fixed fixtures.

This read-only diagnostic prints numeric counts and geometry hashes, never
uploaded files, file paths, or STEP contents. Run inside the FreeCAD image.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import model_exporter as viewer  # noqa: E402

CASES = (
    ("plate_hole", ROOT / "tests/dataset/lamiera_piana_test_1/input.stp", .3),
    ("complex_slot", ROOT / "tests/dataset/staffa_16_pieghe_stress_test/input.stp", .9344),
)


def _slot(face):
    return any(sum(type(edge.Curve).__name__.lower() in {"circle", "geomcircle"}
                   for edge in wire.Edges) == 2 and
               sum(type(edge.Curve).__name__.lower() in {"line", "geomline"}
                   for edge in wire.Edges) == 2 for wire in face.Wires)


def _pick(shape, case):
    if case == "plate_hole":
        return max((face for face in shape.Faces if viewer._is_planar_face(face)
                    and len(face.Wires) == 5), key=lambda face: face.Area)
    return next(face for face in shape.Faces if _slot(face))


def _sag(points, facets, face):
    counts = {}
    for a, b, c in facets:
        for first, second in ((a, b), (b, c), (c, a)):
            key = tuple(sorted((first, second)))
            counts[key] = counts.get(key, 0) + 1
    chords = [edge for edge, count in counts.items() if count == 1]
    results = []
    for edge in face.Edges:
        circle = edge.Curve
        if type(circle).__name__.lower() not in {"circle", "geomcircle"}:
            continue
        center = viewer._vector(circle.Center)
        axis = viewer._normalize(viewer._vector(circle.Axis))
        radius = float(circle.Radius)
        values = []
        for a, b in chords:
            if all(abs(math.dist(points[index], center) - radius) < .002
                   and abs(viewer._dot(tuple(points[index][j] - center[j]
                                             for j in range(3)), axis)) < .002
                   for index in (a, b)):
                midpoint = tuple((points[a][axis] + points[b][axis]) / 2
                                 for axis in range(3))
                values.append(radius - math.dist(midpoint, center))
        if values:
            results.append((len(values), max(values)))
    return {"matched_circle_count": len(results),
            "boundary_chords": sum(n for n, _ in results),
            "maximum_circle_sag_mm": round(max((sag for _, sag in results), default=0.), 7)}


def _mesh(face, deflection):
    start = time.perf_counter()
    raw, facets = face.tessellate(deflection)
    points = [viewer._vector(node) for node in raw]
    facets = [tuple(map(int, triangle)) for triangle in facets]
    digest = hashlib.sha256()
    for point in points:
        digest.update(struct.pack("<3d", *point))
    position_sha = digest.hexdigest()
    for triangle in facets:
        digest.update(struct.pack("<3I", *triangle))
    return {"vertices": len(points), "triangles": len(facets),
            "positions_sha256": position_sha, "mesh_sha256": digest.hexdigest(),
            "elapsed_sec": round(time.perf_counter() - start, 6),
            **_sag(points, facets, face)}


def main():
    viewer._configure_freecad_path()
    import FreeCAD  # noqa: F401
    import Part
    rows = []
    for label, path, coarse_deflection in CASES:
        shape = Part.Shape()
        shape.read(str(path))
        face = _pick(shape, label)
        baseline = _mesh(face, coarse_deflection)
        repeated = _mesh(face, .02)
        try:
            fresh_face = face.cleaned()
            cleaned = _mesh(fresh_face, .02)
            clean_status = "supported"
        except (AttributeError, TypeError):
            cleaned = None
            clean_status = "unavailable"
        shape_fresh = Part.Shape()
        shape_fresh.read(str(path))
        fine_first = _mesh(_pick(shape_fresh, label), .02)
        rows.append({"case": label, "surface": type(face.Surface).__name__,
                     "face_area_mm2": round(float(face.Area), 6),
                     "coarse_deflection_mm": coarse_deflection,
                     "fine_deflection_mm": .02,
                     "coarse_then_fine": {"baseline": baseline, "fine": repeated},
                     "cleaned_api": clean_status, "fine_on_cleaned_copy": cleaned,
                     "fine_first_on_new_import": fine_first})
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
