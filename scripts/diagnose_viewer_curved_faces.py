"""Inspect the non-planar face selected by the viewer regression in FreeCAD.

Run in the project Docker image: python3 scripts/diagnose_viewer_curved_faces.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import model_exporter as viewer


CASES = (
    "tests/dataset/lamiera_piana_test_1/input.stp",
    "tests/test_files/STAFFA TEST 1.stp",
    "tests/dataset/staffa_16_pieghe_stress_test/input.stp",
)


def _angle(left, right):
    return math.degrees(math.acos(max(-1.0, min(1.0, viewer._dot(left, right)))))


def inspect_face(face, index):
    source_vertices, raw_facets = face.tessellate(.3 * .65)
    source_vertices = list(source_vertices)
    points = [viewer._vector(vertex) for vertex in source_vertices]
    facets = [tuple(int(i) for i in facet) for facet in raw_facets if len(facet) == 3]
    fallback = viewer._vertex_normals(points, facets)
    cad_normals = []
    failures = []
    for vertex_index, vertex in enumerate(source_vertices):
        try:
            u, v = face.Surface.parameter(vertex)
            cad_normals.append(viewer._normalize(viewer._vector(face.normalAt(u, v))))
        except Exception as exc:
            cad_normals.append(None)
            failures.append({"vertex": vertex_index, "error": str(exc)})
    exported = viewer._analytic_face_normals(face, source_vertices, points, facets)
    valid = [n for n in cad_normals if n is not None]
    uv_range = [float(x) for x in face.ParameterRange]
    sample_indices = sorted(set((0, len(points)//4, len(points)//2,
                                 3*len(points)//4, len(points)-1))) if points else []
    # Compare to one reference normal: a sufficient lower bound on sweep,
    # without quadratic work on densely tessellated real faces.
    return {
        "face_index_0_based": index,
        "face_index_1_based": index + 1,
        "surface_type": type(face.Surface).__name__,
        "area": float(face.Area),
        "uv_range": uv_range,
        "tessellated_vertices": len(points),
        "triangles": len(facets),
        "cad_sampled_normals": [{"vertex": i, "normal": cad_normals[i]}
                                for i in sample_indices],
        "cad_angle_from_first_max_deg": max((_angle(valid[0], n)
                                              for n in valid[1:]), default=0.0),
        "exported_angle_from_first_max_deg": max((_angle(exported[0], n)
                                                   for n in exported[1:]), default=0.0),
        "fallback_vertices": len(failures),
        "fallback_examples": failures[:5],
        "fallback_normals_sample": [fallback[i] for i in range(min(3, len(fallback)))],
    }


def main():
    import FreeCAD  # noqa: F401; FreeCAD must load before Part
    import Part

    root = Path(__file__).resolve().parents[1]
    for relative in CASES:
        shape = Part.Shape()
        shape.read(str(root / relative))
        curved = [(index, face) for index, face in enumerate(shape.Faces)
                  if not viewer._is_planar_face(face)]
        cylinders = sorted(((index, face) for index, face in curved
                            if type(face.Surface).__name__.lower() in
                            {"cylinder", "geomcylinder"}),
                           key=lambda item: float(item[1].Area), reverse=True)
        selected_cylinder = None
        for index, face in cylinders:
            detail = inspect_face(face, index)
            if (detail["tessellated_vertices"] >= 3 and
                    detail["cad_angle_from_first_max_deg"] > 5):
                selected_cylinder = detail
                break
        largest_index, largest_face = max(
            curved, key=lambda item: float(item[1].Area),
        ) if curved else (None, None)
        print(json.dumps({"step": relative, "face_count": len(shape.Faces),
                          "curved_face_count": len(curved),
                          "largest_nonplanar": inspect_face(largest_face, largest_index)
                          if curved else None,
                          "verified_curved_cylinder": selected_cylinder}, indent=2))


if __name__ == "__main__":
    main()
