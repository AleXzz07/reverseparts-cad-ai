from __future__ import annotations

import json
import importlib
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cad_analyzer import (  # noqa: E402
    _configure_freecad_path,
    analyze_step_file,
    load_analysis_config,
)
from app.sheetmetal_unfolder import (  # noqa: E402
    _cylinder_length,
    _cylinder_span_deg,
    _face_points,
    _face_signature,
    _surface_type,
    build_sheet_face_graph,
    singleton_bend_adjacent_faces,
)


DATASET = ROOT / "tests" / "dataset" / "sheetmetal_benchmark_v2_canonical.json"
CASES = ROOT / "tests" / "dataset" / "sheetmetal_benchmark_v2"


def _error(actual: float | None, expected: float) -> float | None:
    if actual is None:
        return None
    return abs(float(actual) - expected)


def _shape(path: Path):
    _configure_freecad_path()
    part = importlib.import_module("Part")
    shape = part.Shape()
    shape.read(str(path))
    return shape


def _vector(value):
    return (float(value.x), float(value.y), float(value.z))


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _normalize(value):
    length = math.sqrt(_dot(value, value))
    return tuple(component / length for component in value)


def _local_unfold_reference(path: Path) -> dict:
    shape = _shape(path)
    planar = [face for face in shape.Faces if _surface_type(face) == "Part::GeomPlane"]
    face = max(planar, key=lambda item: float(item.Area))
    normal = _normalize(_vector(face.Surface.Axis))
    points = _face_points(face)
    directions = []
    for index in range(1, len(points)):
        delta = tuple(points[index][axis] - points[0][axis] for axis in range(3))
        length = math.sqrt(_dot(delta, delta))
        if length > 1e-6:
            directions.append(tuple(component / length for component in delta))
    u = directions[0]
    v = _normalize(_cross(normal, u))
    u_values = [_dot(point, u) for point in points]
    v_values = [_dot(point, v) for point in points]
    dimensions = sorted(
        (max(u_values) - min(u_values), max(v_values) - min(v_values)),
        reverse=True,
    )
    return {
        "local_blank_mm": {"x": round(dimensions[0], 4), "y": round(dimensions[1], 4)},
        "net_planar_face_area_mm2": round(float(face.Area), 4),
        "outer_perimeter_mm": round(float(face.OuterWire.Length), 4),
        "wire_count": len(face.Wires),
    }


def _graph_debug(path: Path, thickness_mm: float) -> dict:
    shape = _shape(path)
    parameters = load_analysis_config()
    graph = build_sheet_face_graph(shape, thickness_mm, 0.4, parameters)
    face_indices = {_face_signature(face): index for index, face in enumerate(shape.Faces, start=1)}
    panel_for_face = {
        _face_signature(face): panel.id
        for panel in graph.panels
        for face in panel.faces
    }
    planes = []
    for index, face in enumerate(shape.Faces, start=1):
        if _surface_type(face) != "Part::GeomPlane":
            continue
        normal = _normalize(_vector(face.Surface.Axis))
        position = _vector(face.Surface.Position)
        center = _vector(face.CenterOfMass)
        planes.append(
            {
                "face": index,
                "panel": panel_for_face.get(_face_signature(face)),
                "normal": [round(value, 4) for value in normal],
                "offset": round(_dot(normal, position), 4),
                "center": [round(value, 4) for value in center],
                "area_mm2": round(float(face.Area), 4),
            }
        )
    cylinders = []
    for index, face in enumerate(shape.Faces, start=1):
        if _surface_type(face) != "Part::GeomCylinder":
            continue
        axis = _normalize(_vector(face.Surface.Axis))
        adjacent = singleton_bend_adjacent_faces(face, shape, thickness_mm, parameters)
        cylinders.append(
            {
                "face": index,
                "radius_mm": round(float(face.Surface.Radius), 4),
                "span_deg": _cylinder_span_deg(face),
                "axis": [round(value, 4) for value in axis],
                "length_mm": round(_cylinder_length(face, axis), 4),
                "parameter_range": [round(float(value), 4) for value in face.ParameterRange],
                "singleton_adjacent_planar_faces": [
                    face_indices.get(_face_signature(item)) for item in adjacent
                ],
            }
        )
    return {
        "root": graph.root_panel_id,
        "connected": graph.connected,
        "direct": graph.direct,
        "panels": [
            {
                "id": panel.id,
                "faces": [face_indices.get(_face_signature(face)) for face in panel.faces],
                "normal": [round(value, 4) for value in panel.normal],
                "area_mm2": round(panel.area_mm2, 4),
            }
            for panel in graph.panels
        ],
        "planar_faces": planes,
        "bends": [
            {
                "id": bend.id,
                "radius_mm": round(bend.inner_radius_mm, 4),
                "angle_deg": round(bend.angle_deg, 4),
                "axis": [round(value, 4) for value in bend.axis],
                "panels": bend.panel_ids,
                "complete_pair": bend.outer_face is not None,
            }
            for bend in graph.bends
        ],
        "cylinders": cylinders,
        "warnings": graph.warnings,
    }


def main() -> int:
    benchmark = json.loads(DATASET.read_text(encoding="utf-8"))
    pending_reference_cases = set(benchmark.get("pending_reference_cases", []))
    rows = []
    failed = False
    for piece in benchmark["pieces"]:
        case_id = piece["id"][:4]
        truth = piece["freecad_unfold_ground_truth"]
        expected_dimensions = truth.get(
            "blank_dimensions_local_2d_mm",
            truth["blank_dimensions_mm"],
        )
        step_path = CASES / case_id / "folded.step"
        actual = analyze_step_file(
            file_bytes=step_path.read_bytes(),
            source_file=step_path.name,
            material="acciaio",
            density_g_cm3=7.85,
        ).model_dump()
        flat = actual["flat_pattern"]
        graph_debug = _graph_debug(
            step_path,
            float(piece["design_truth"]["sheet_thickness_mm"]),
        )
        dimensions = flat.get("blank_dimensions_mm") or {}
        bend_ok = actual["bends"]["count"] == piece["design_truth"]["bend_count"]
        length_error = _error(dimensions.get("x"), expected_dimensions["length"])
        width_error = _error(dimensions.get("y"), expected_dimensions["width"])
        area_error = _error(flat.get("net_developed_area_mm2"), truth["material_area_from_unfold_volume_mm2"])
        perimeter_error = _error(flat.get("outer_perimeter_mm"), truth["largest_planar_outer_perimeter_mm"])
        core_passed = (
            bend_ok
            and flat.get("status") in {"exact", "validated_estimate"}
            and flat.get("usable_for_costing") is True
            and flat.get("validation", {}).get("passed") is True
        )
        reference_pending = case_id in pending_reference_cases
        passed = core_passed and perimeter_error is not None and perimeter_error <= (
            truth["largest_planar_outer_perimeter_mm"] * 0.01
        ) and (
            reference_pending
            or (
                length_error is not None
                and length_error <= max(0.5, expected_dimensions["length"] * 0.005)
                and width_error is not None
                and width_error <= max(0.5, expected_dimensions["width"] * 0.005)
                and area_error is not None
                and area_error <= truth["material_area_from_unfold_volume_mm2"] * 0.005
            )
        )
        failed = failed or not passed
        rows.append(
            {
                "case": case_id,
                "passed": passed,
                "bends": actual["bends"]["count"],
                "expected_bends": piece["design_truth"]["bend_count"],
                "radii_mm": [bend["radius_mm"] for bend in graph_debug["bends"]],
                "angles_deg": [bend["angle_deg"] for bend in graph_debug["bends"]],
                "axes": [bend["axis"] for bend in graph_debug["bends"]],
                "complete_cylinder_pairs": [
                    bend["complete_pair"] for bend in graph_debug["bends"]
                ],
                "panel_count": len(graph_debug["panels"]),
                "bend_zone_count": len(graph_debug["bends"]),
                "blank_mm": dimensions or None,
                "expected_blank_mm": expected_dimensions,
                "area_mm2": flat.get("net_developed_area_mm2"),
                "expected_area_mm2": truth["material_area_from_unfold_volume_mm2"],
                "perimeter_mm": flat.get("outer_perimeter_mm"),
                "expected_perimeter_mm": truth["largest_planar_outer_perimeter_mm"],
                "flat_status": flat.get("status"),
                "usable_for_costing": flat.get("usable_for_costing"),
                "comparison_scope": (
                    "geometry_regression_only_reference_pending"
                    if reference_pending
                    else "full"
                ),
                "reference_unfold_local": _local_unfold_reference(
                    CASES / case_id / "reference_unfold.step"
                ),
                "face_graph": graph_debug if not passed else None,
            }
        )
    print(json.dumps({"benchmark": benchmark["version"], "results": rows}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
