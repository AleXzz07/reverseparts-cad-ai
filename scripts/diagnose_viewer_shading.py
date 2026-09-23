"""Inspect face planarity, normals and winding without touching CAD analysis."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import model_exporter as after  # noqa: E402
from scripts import _viewer_v1_1_before_shading as before  # noqa: E402


def inspect(step: Path, deflection: float) -> dict:
    after._configure_freecad_path()
    importlib.import_module("FreeCAD")
    Part = importlib.import_module("Part")
    shape = Part.Shape()
    shape.read(str(step))
    if shape.isNull():
        raise ValueError("Empty STEP shape")
    planar_rows = []
    for number, face in enumerate(shape.Faces, 1):
        if not after._is_planar_face(face):
            continue
        vertices, raw = face.tessellate(deflection)
        points = [after._vector(item) for item in vertices]
        facets = [tuple(map(int, item)) for item in raw if len(item) == 3]
        if not points or not facets:
            continue
        normal = after._normalize(after._vector(
            face.normalAt(*face.Surface.parameter(vertices[0]))
        ))
        original_normals = before._analytic_face_normals(
            face, list(vertices), points, facets,
        )
        repaired_normals = after._analytic_face_normals(
            face, list(vertices), points, facets,
        )
        winding = [after._dot(after._normal(*(points[i] for i in triangle)), normal)
                   for triangle in facets]
        origin = points[0]
        distances = [abs(after._dot(tuple(p[i] - origin[i] for i in range(3)), normal))
                     for p in points]
        changed = sum(any(abs(a - b) > 1e-5 for a, b in zip(old, new))
                      for old, new in zip(original_normals, repaired_normals))
        planar_rows.append({
            "face_number": number,
            "cad_area_mm2": float(face.Area),
            "wire_count": len(face.Wires),
            "vertices": len(points),
            "triangles": len(facets),
            "max_distance_from_cad_plane_mm": max(distances),
            "triangles_opposite_cad_normal": sum(value < -1e-6 for value in winding),
            "before_vertices_with_different_normal": changed,
            "after_unique_normals_6dp": len({tuple(round(x, 6) for x in n)
                                               for n in repaired_normals}),
        })
    old = before._tessellate_brep_faces(shape, deflection, curved_refinement=.65)
    new = after._tessellate_brep_faces(shape, deflection, curved_refinement=.65)
    return {
        "step": str(step),
        "face_count": len(shape.Faces),
        "positions_equal": old[0] == new[0],
        "undirected_triangles_equal":
            [sorted(t) for t in old[1]] == [sorted(t) for t in new[1]],
        "triangle_count": len(new[1]),
        "vertex_count": len(new[0]),
        "changed_normal_count": sum(any(abs(a - b) > 1e-5 for a, b in zip(x, y))
                                    for x, y in zip(old[2], new[2])),
        "planar_faces": sorted(planar_rows, key=lambda r: -r["cad_area_mm2"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--deflection", type=float, default=.3)
    args = parser.parse_args()
    result = inspect(args.step, args.deflection)
    content = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    print(content)
    return 0 if result["positions_equal"] and result["undirected_triangles_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
