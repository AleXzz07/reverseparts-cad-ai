#!/usr/bin/env python3
"""Differentiate UV permutation, placement and incompatible triangulation.

Docker/FreeCAD 0.19 diagnostic only. Evaluates a fixed set of public test
faces and emits counts, distances and bounded transforms, never STEP content.
No approximate UV assignment is used by the exporter.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
STEP = ROOT / "tests/dataset/staffa_16_pieghe_stress_test/input.stp"
TARGET_FACES = (79, 97, 123, 125, 177, 209, 232)


def xyz(vector):
    return float(vector.x), float(vector.y), float(vector.z)


def bounds(points):
    return [[round(min(p[axis] for p in points), 6),
             round(max(p[axis] for p in points), 6)] for axis in range(3)]


def hash_tessellation(points, triangles):
    digest = hashlib.sha256()
    for p in points:
        digest.update(struct.pack("<3d", *xyz(p)))
    for triangle in triangles:
        digest.update(struct.pack("<3I", *triangle))
    return digest.hexdigest()


def cell_key(point, tolerance):
    return tuple(math.floor(x / tolerance) for x in point)


def match_counts(mesh, other, tolerance):
    """Count possible matches; never accept or generate a UV mapping."""
    bins = collections.defaultdict(list)
    for i, point in enumerate(mesh):
        bins[cell_key(point, tolerance)].append(i)
    matched, ambiguous, best_distances, best_indices = 0, 0, [], []
    for point in other:
        cx, cy, cz = cell_key(point, tolerance)
        candidates = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for i in bins.get((cx + dx, cy + dy, cz + dz), ()):
                        distance = math.dist(point, mesh[i])
                        if distance <= tolerance:
                            candidates.append((distance, i))
        if candidates:
            matched += 1
            ambiguous += len(candidates) != 1
            distance, index = min(candidates)
            best_distances.append(distance)
            best_indices.append(index)
    return {"nodes_with_candidate": matched, "ambiguous_uv_nodes": ambiguous,
            "mesh_nodes_reused": len(best_indices) - len(set(best_indices)),
            "largest_matched_distance_mm": round(max(best_distances, default=0), 7),
            "median_matched_distance_mm": round(sorted(best_distances)[len(best_distances)//2], 7)
            if best_distances else None}


def sampled_nearest_distances(mesh, other, count=16):
    if not other:
        return None
    samples = [other[i] for i in range(0, len(other), max(1, len(other)//count))][:count]
    minimums = [min(math.dist(point, source) for source in mesh) for point in samples]
    return {"sample_count": len(minimums),
            "min_mm": round(min(minimums), 7),
            "p50_mm": round(sorted(minimums)[len(minimums)//2], 7),
            "max_mm": round(max(minimums), 7)}


def variants(face, uv_nodes):
    native = [face.valueAt(float(u), float(v)) for u, v in uv_nodes]
    yield "face.valueAt", native
    placement = face.Placement
    try:
        yield "face.Placement.multVec(valueAt)", [placement.multVec(p) for p in native]
        yield "face.Placement.inverse().multVec(valueAt)", [
            placement.inverse().multVec(p) for p in native
        ]
    except Exception:
        pass
    try:
        surface = face.Surface
        yield "face.Surface.value", [surface.value(float(u), float(v)) for u, v in uv_nodes]
    except Exception:
        pass


def main():
    sys.path.insert(0, str(ROOT))
    from app import model_exporter
    model_exporter._configure_freecad_path()
    import FreeCAD  # noqa: F401 - FreeCAD must load before Part
    import Part
    shape = Part.Shape()
    shape.read(str(STEP))
    box = shape.BoundBox
    deflection = max(math.sqrt(box.XLength**2 + box.YLength**2 +
                               box.ZLength**2)/300., .04)*.8
    tolerance = min(.1, max(1e-5, deflection*.1))
    rows = []
    for face_index in TARGET_FACES:
        face = shape.Faces[face_index-1]
        if "bspline" not in type(face.Surface).__name__.lower():
            rows.append({"face": face_index, "surface": type(face.Surface).__name__,
                         "reason": "not_bspline"})
            continue
        vertices, triangles = face.tessellate(deflection)
        native_uv = face.getUVNodes()
        mesh = [xyz(p) for p in vertices]
        row = {"face": face_index, "vertices": len(vertices),
               "uv_nodes": len(native_uv), "triangles": len(triangles),
               "tessellation_sha256": hash_tessellation(vertices, triangles),
               "mesh_xyz_bounds_mm": bounds(mesh),
               "placement_translation_mm": tuple(round(x, 7) for x in xyz(face.Placement.Base)),
               "variants": {}}
        for kind, surface_vertices in variants(face, native_uv):
            coordinates = [xyz(point) for point in surface_vertices]
            distances = [math.dist(a, b) for a, b in zip(mesh, coordinates)]
            row["variants"][kind] = {
                "uv_xyz_bounds_mm": bounds(coordinates),
                "index_distance_median_mm": round(sorted(distances)[len(distances)//2], 7),
                "index_distance_max_mm": round(max(distances), 7),
                "within_tolerance": sum(d <= tolerance for d in distances),
                "set_matching": match_counts(mesh, coordinates, tolerance),
                "sample_nearest": sampled_nearest_distances(mesh, coordinates),
            }
        # If this changes, the act of fetching UVs may have replaced or
        # remeshed the triangulation: a 1:1 index claim would be unsafe.
        second_vertices, second_triangles = face.tessellate(deflection)
        row["tessellation_stable_after_uv_read"] = (
            row["tessellation_sha256"] == hash_tessellation(second_vertices, second_triangles)
        )
        rows.append(row)
    print(json.dumps({"case": "staffa_16_pieghe_stress_test",
                      "freecad_version": FreeCAD.Version()[:3],
                      "deflection_mm": deflection, "tolerance_mm": tolerance,
                      "faces": rows}, indent=2))


if __name__ == "__main__":
    main()
