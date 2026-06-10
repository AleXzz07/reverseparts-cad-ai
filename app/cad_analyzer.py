from __future__ import annotations

import importlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .schemas import BendFeature, CadAnalysisResponse, Dimensions, HoleFeature


VALID_STEP_SUFFIXES = {".stp", ".step"}
CIRCULAR_HOLE_MIN_DIAMETER_MM = 4.0
CIRCULAR_HOLE_MAX_DIAMETER_MM = 20.0
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ANALYSIS_CONFIG_PATH = PROJECT_ROOT / "config" / "analysis_default.json"
FREECAD_PATH_CANDIDATES = (
    "/usr/lib/freecad-python3/lib",
    "/usr/lib/freecad/lib",
    "/usr/lib/freecad-python3",
)


@dataclass(frozen=True)
class FreeCadStatus:
    available: bool
    error: str | None = None


@dataclass(frozen=True)
class AnalysisParameters:
    hole_center_tolerance_mm: float
    hole_diameter_tolerance_mm: float
    hole_axis_angle_tolerance_deg: float
    bend_center_tolerance_mm: float
    bend_radius_pair_tolerance_mm: float
    bend_axis_angle_tolerance_deg: float
    bend_min_length_mm: float


def load_analysis_config(path: Path = DEFAULT_ANALYSIS_CONFIG_PATH) -> AnalysisParameters:
    data = json.loads(path.read_text(encoding="utf-8"))
    hole = data["circular_hole_deduplication"]
    bend = data["bend_detection"]
    return AnalysisParameters(
        hole_center_tolerance_mm=float(hole["center_tolerance_mm"]),
        hole_diameter_tolerance_mm=float(hole["diameter_tolerance_mm"]),
        hole_axis_angle_tolerance_deg=float(hole["axis_angle_tolerance_deg"]),
        bend_center_tolerance_mm=float(bend["center_tolerance_mm"]),
        bend_radius_pair_tolerance_mm=float(bend["radius_pair_tolerance_mm"]),
        bend_axis_angle_tolerance_deg=float(bend["axis_angle_tolerance_deg"]),
        bend_min_length_mm=float(bend["min_length_mm"]),
    )


def _configure_freecad_path() -> None:
    import sys

    for candidate in FREECAD_PATH_CANDIDATES:
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)


def get_freecad_status() -> FreeCadStatus:
    try:
        _configure_freecad_path()
        importlib.import_module("FreeCAD")
        importlib.import_module("Part")
    except Exception as exc:  # pragma: no cover - depends on host FreeCAD install
        return FreeCadStatus(available=False, error=str(exc))
    return FreeCadStatus(available=True)


def _round_or_none(value: float | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _vector_tuple(vector) -> tuple[float, float, float]:
    return (float(vector.x), float(vector.y), float(vector.z))


def _vector_norm(vector: tuple[float, float, float]) -> float:
    return sum(component * component for component in vector) ** 0.5


def _normalize_vector(vector) -> tuple[float, float, float]:
    values = _vector_tuple(vector)
    norm = _vector_norm(values)
    if norm == 0:
        return (0.0, 0.0, 0.0)
    return tuple(component / norm for component in values)


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sum(left * right for left, right in zip(a, b))


def _axis_aligned(a: tuple[float, float, float], b: tuple[float, float, float], tolerance: float = 0.98) -> bool:
    return abs(_dot(a, b)) >= tolerance


def _projected_distance(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    axis: tuple[float, float, float],
) -> float:
    return abs(_dot(tuple(left - right for left, right in zip(a, b)), axis))


def _rounded_vector(vector: tuple[float, float, float], digits: int = 3) -> list[float]:
    return [round(component, digits) for component in vector]


def _candidate_depth_from_bbox(bbox, axis: tuple[float, float, float]) -> float:
    lengths = (float(bbox.XLength), float(bbox.YLength), float(bbox.ZLength))
    return sum(abs(component) * length for component, length in zip(axis, lengths))


def _plane_offset(normal: tuple[float, float, float], point: tuple[float, float, float]) -> float:
    return _dot(normal, point)


def _axis_tolerance(angle_degrees: float) -> float:
    return math.cos(math.radians(angle_degrees))


def _is_duplicate_hole(
    candidate: HoleFeature,
    existing: HoleFeature,
    parameters: AnalysisParameters,
) -> bool:
    if candidate.diameter_mm is None or existing.diameter_mm is None:
        return False
    if candidate.center is None or existing.center is None:
        return False
    if candidate.axis is None or existing.axis is None:
        return False

    if abs(candidate.diameter_mm - existing.diameter_mm) > parameters.hole_diameter_tolerance_mm:
        return False

    candidate_axis = tuple(candidate.axis)
    existing_axis = tuple(existing.axis)
    if not _axis_aligned(
        candidate_axis,
        existing_axis,
        tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
    ):
        return False

    center_delta = tuple(
        left - right for left, right in zip(tuple(candidate.center), tuple(existing.center))
    )
    signed_projected_offset = _dot(center_delta, existing_axis)
    projected_offset = abs(signed_projected_offset)
    radial_offset = _vector_norm(
        tuple(component - signed_projected_offset * axis for component, axis in zip(center_delta, existing_axis))
    )
    max_depth = max(candidate.depth_mm or 0.0, existing.depth_mm or 0.0, 1.0)
    return (
        radial_offset <= parameters.hole_center_tolerance_mm
        and projected_offset <= max_depth + parameters.hole_center_tolerance_mm
    )


def _append_unique_hole(
    holes: list[HoleFeature],
    candidate: HoleFeature,
    parameters: AnalysisParameters,
) -> None:
    for existing in holes:
        if _is_duplicate_hole(candidate, existing, parameters):
            if existing.confidence != "high" and candidate.confidence == "high":
                existing.confidence = "high"
            return
    holes.append(candidate)


def _curve_type(edge) -> str:
    return getattr(edge.Curve, "TypeId", "")


def _detect_circular_holes(shape, parameters: AnalysisParameters) -> list[HoleFeature]:
    face_candidates: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomCylinder":
            continue

        radius = float(surface.Radius)
        diameter = radius * 2.0
        if not CIRCULAR_HOLE_MIN_DIAMETER_MM <= diameter <= CIRCULAR_HOLE_MAX_DIAMETER_MM:
            continue

        axis = _normalize_vector(surface.Axis)
        center = _vector_tuple(surface.Center)
        depth = _candidate_depth_from_bbox(face.BoundBox, axis)

        if depth < 1.0 or depth > 6.0:
            continue

        face_candidates.append(
            HoleFeature(
                diameter_mm=round(diameter, 2),
                radius_mm=round(radius, 2),
                center=_rounded_vector(center),
                axis=_rounded_vector(axis),
                depth_mm=round(depth, 3),
                confidence="medium",
            )
        )

    edge_candidates: list[HoleFeature] = []
    circular_edges = []
    for edge in shape.Edges:
        curve = edge.Curve
        if getattr(curve, "TypeId", "") != "Part::GeomCircle":
            continue

        radius = float(curve.Radius)
        diameter = radius * 2.0
        if not CIRCULAR_HOLE_MIN_DIAMETER_MM <= diameter <= CIRCULAR_HOLE_MAX_DIAMETER_MM:
            continue

        circular_edges.append(
            {
                "radius": radius,
                "diameter": diameter,
                "center": _vector_tuple(curve.Center),
                "axis": _normalize_vector(curve.Axis),
            }
        )

    used_edge_indexes: set[int] = set()
    for left_index, left in enumerate(circular_edges):
        if left_index in used_edge_indexes:
            continue
        for right_index in range(left_index + 1, len(circular_edges)):
            if right_index in used_edge_indexes:
                continue

            right = circular_edges[right_index]
            if abs(left["diameter"] - right["diameter"]) > parameters.hole_diameter_tolerance_mm:
                continue
            if not _axis_aligned(
                left["axis"],
                right["axis"],
                tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
            ):
                continue

            depth = _projected_distance(left["center"], right["center"], left["axis"])
            if not 1.0 <= depth <= 4.0:
                continue

            center_offset = tuple(l - r for l, r in zip(left["center"], right["center"]))
            radial_offset = _vector_norm(
                tuple(
                    component - _dot(center_offset, left["axis"]) * axis
                    for component, axis in zip(center_offset, left["axis"])
                )
            )
            if radial_offset > parameters.hole_center_tolerance_mm:
                continue

            center = tuple((l + r) / 2.0 for l, r in zip(left["center"], right["center"]))
            edge_candidates.append(
                HoleFeature(
                    diameter_mm=round((left["diameter"] + right["diameter"]) / 2.0, 2),
                    radius_mm=round((left["radius"] + right["radius"]) / 2.0, 2),
                    center=_rounded_vector(center),
                    axis=_rounded_vector(left["axis"]),
                    depth_mm=round(depth, 3),
                    confidence="high",
                )
            )
            used_edge_indexes.update({left_index, right_index})
            break

    holes: list[HoleFeature] = []
    for candidate in edge_candidates + face_candidates:
        _append_unique_hole(holes, candidate, parameters)

    holes.sort(key=lambda hole: (hole.diameter_mm or 0.0, hole.center or []))
    return holes


def _wire_center(wire) -> tuple[float, float, float]:
    bbox = wire.BoundBox
    return (
        (float(bbox.XMin) + float(bbox.XMax)) / 2.0,
        (float(bbox.YMin) + float(bbox.YMax)) / 2.0,
        (float(bbox.ZMin) + float(bbox.ZMax)) / 2.0,
    )


def _slot_axis_from_arc_centers(arcs: list) -> tuple[float, float, float]:
    if len(arcs) != 2:
        return (0.0, 0.0, 0.0)
    first = _vector_tuple(arcs[0].Curve.Center)
    second = _vector_tuple(arcs[1].Curve.Center)
    delta = tuple(
        second_value - first_value for first_value, second_value in zip(first, second)
    )
    norm = _vector_norm(delta)
    if norm == 0:
        return (0.0, 0.0, 0.0)
    return tuple(component / norm for component in delta)


def _is_duplicate_slot(candidate: HoleFeature, existing: HoleFeature) -> bool:
    if candidate.length_mm is None or existing.length_mm is None:
        return False
    if candidate.width_mm is None or existing.width_mm is None:
        return False
    if candidate.center is None or existing.center is None:
        return False
    if candidate.axis is None or existing.axis is None:
        return False

    return (
        abs(candidate.length_mm - existing.length_mm) <= 0.5
        and abs(candidate.width_mm - existing.width_mm) <= 0.3
        and _axis_aligned(tuple(candidate.axis), tuple(existing.axis), tolerance=0.95)
        and _vector_norm(
            tuple(left - right for left, right in zip(candidate.center, existing.center))
        )
        <= 3.0
    )


def _append_unique_slot(slots: list[HoleFeature], candidate: HoleFeature) -> None:
    for existing in slots:
        if _is_duplicate_slot(candidate, existing):
            existing.center = _rounded_vector(
                tuple(
                    (left + right) / 2.0
                    for left, right in zip(existing.center or [], candidate.center or [])
                )
            )
            existing.confidence = "high"
            return
    slots.append(candidate)


def _is_duplicate_polygon(candidate: HoleFeature, existing: HoleFeature) -> bool:
    if candidate.max_dimension_mm is None or existing.max_dimension_mm is None:
        return False
    if candidate.center is None or existing.center is None:
        return False
    if candidate.axis is None or existing.axis is None:
        return False

    return (
        abs(candidate.max_dimension_mm - existing.max_dimension_mm) <= 0.5
        and _axis_aligned(tuple(candidate.axis), tuple(existing.axis), tolerance=0.95)
        and _vector_norm(
            tuple(left - right for left, right in zip(candidate.center, existing.center))
        )
        <= 3.0
    )


def _append_unique_polygon(polygons: list[HoleFeature], candidate: HoleFeature) -> None:
    for existing in polygons:
        if _is_duplicate_polygon(candidate, existing):
            existing.center = _rounded_vector(
                tuple(
                    (left + right) / 2.0
                    for left, right in zip(existing.center or [], candidate.center or [])
                )
            )
            existing.confidence = "high"
            return
    polygons.append(candidate)


def _detect_elongated_holes(shape) -> list[HoleFeature]:
    slots: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue

        for wire_index, wire in enumerate(face.Wires):
            if wire_index == 0 or not wire.isClosed():
                continue

            arcs = [edge for edge in wire.Edges if _curve_type(edge) == "Part::GeomCircle"]
            lines = [edge for edge in wire.Edges if _curve_type(edge) == "Part::GeomLine"]
            if len(arcs) != 2 or len(lines) != 2:
                continue

            radii = [float(edge.Curve.Radius) for edge in arcs]
            if abs(radii[0] - radii[1]) > 0.2:
                continue

            width = sum(radii) / len(radii) * 2.0
            if not 4.0 <= width <= 20.0:
                continue

            line_directions = [_normalize_vector(edge.Curve.Direction) for edge in lines]
            if not _axis_aligned(line_directions[0], line_directions[1], tolerance=0.98):
                continue

            length = float(wire.Length)
            if not 45.0 <= length <= 60.0:
                continue

            slot_axis = _slot_axis_from_arc_centers(arcs)
            if _vector_norm(slot_axis) == 0:
                slot_axis = line_directions[0]

            _append_unique_slot(
                slots,
                HoleFeature(
                    length_mm=round(length, 2),
                    width_mm=round(width, 2),
                    center=_rounded_vector(_wire_center(wire)),
                    axis=_rounded_vector(slot_axis),
                    confidence="medium",
                ),
            )

    slots.sort(key=lambda slot: slot.center or [])
    return slots


def _detect_polygonal_holes(shape) -> list[HoleFeature]:
    polygons: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue

        for wire_index, wire in enumerate(face.Wires):
            if wire_index == 0 or not wire.isClosed():
                continue

            edges = list(wire.Edges)
            if len(edges) < 3:
                continue
            if any(_curve_type(edge) != "Part::GeomLine" for edge in edges):
                continue

            max_dimension = float(wire.Length)
            if not 20.0 <= max_dimension <= 35.0:
                continue

            bbox = wire.BoundBox
            bbox_dimensions = Dimensions(
                x=round(float(bbox.XLength), 3),
                y=round(float(bbox.YLength), 3),
                z=round(float(bbox.ZLength), 3),
            )
            nonzero_bbox_dimensions = [
                value
                for value in (bbox_dimensions.x, bbox_dimensions.y, bbox_dimensions.z)
                if value is not None and value > 1.0
            ]
            if len(nonzero_bbox_dimensions) < 2:
                continue

            _append_unique_polygon(
                polygons,
                HoleFeature(
                    num_sides=len(edges),
                    max_dimension_mm=round(max_dimension, 2),
                    bounding_box_mm=bbox_dimensions,
                    center=_rounded_vector(_wire_center(wire)),
                    axis=_rounded_vector(_normalize_vector(surface.Axis)),
                    confidence="medium",
                ),
            )

    polygons.sort(key=lambda polygon: polygon.center or [])
    return polygons


def _is_duplicate_bend(candidate: BendFeature, existing: BendFeature) -> bool:
    if candidate.radius_mm is None or existing.radius_mm is None:
        return False
    if candidate.length_mm is None or existing.length_mm is None:
        return False
    if candidate.center is None or existing.center is None:
        return False
    if candidate.axis is None or existing.axis is None:
        return False

    candidate_axis = tuple(candidate.axis)
    existing_axis = tuple(existing.axis)
    if not _axis_aligned(candidate_axis, existing_axis, tolerance=0.98):
        return False

    center_delta = tuple(left - right for left, right in zip(candidate.center, existing.center))
    projected_delta = _dot(center_delta, existing_axis)
    perpendicular_delta = tuple(
        component - projected_delta * axis_component
        for component, axis_component in zip(center_delta, existing_axis)
    )

    return (
        abs(candidate.length_mm - existing.length_mm) <= 1.0
        and abs(candidate.radius_mm - existing.radius_mm) <= 2.5
        and _vector_norm(perpendicular_delta) <= 3.0
        and abs(projected_delta) <= max(candidate.length_mm, existing.length_mm) + 3.0
    )


def _append_unique_bend(bends: list[BendFeature], candidate: BendFeature) -> None:
    for existing in bends:
        if _is_duplicate_bend(candidate, existing):
            if existing.radius_mm is None or (
                candidate.radius_mm is not None and candidate.radius_mm < existing.radius_mm
            ):
                existing.radius_mm = candidate.radius_mm
            existing.length_mm = max(existing.length_mm or 0.0, candidate.length_mm or 0.0)
            existing.center = _rounded_vector(
                tuple(
                    (left + right) / 2.0
                    for left, right in zip(existing.center or [], candidate.center or [])
                )
            )
            existing.confidence = "high"
            return
    bends.append(candidate)


def _bend_pair_matches(
    left: BendFeature,
    right: BendFeature,
    thickness: float,
    parameters: AnalysisParameters,
) -> bool:
    if left.radius_mm is None or right.radius_mm is None:
        return False
    if left.length_mm is None or right.length_mm is None:
        return False
    if left.center is None or right.center is None:
        return False
    if left.axis is None or right.axis is None:
        return False

    left_axis = tuple(left.axis)
    right_axis = tuple(right.axis)
    if not _axis_aligned(
        left_axis,
        right_axis,
        tolerance=_axis_tolerance(parameters.bend_axis_angle_tolerance_deg),
    ):
        return False
    if abs(abs(left.radius_mm - right.radius_mm) - thickness) > parameters.bend_radius_pair_tolerance_mm:
        return False

    center_delta = tuple(a - b for a, b in zip(left.center, right.center))
    projected_delta = _dot(center_delta, left_axis)
    perpendicular_delta = tuple(
        component - projected_delta * axis_component
        for component, axis_component in zip(center_delta, left_axis)
    )
    return (
        _vector_norm(perpendicular_delta) <= parameters.bend_center_tolerance_mm
        and abs(projected_delta) <= max(left.length_mm, right.length_mm) + parameters.bend_center_tolerance_mm
    )


def _detect_bends(
    shape,
    detected_thickness_mm: float | None,
    parameters: AnalysisParameters,
) -> list[BendFeature]:
    thickness_reference = detected_thickness_mm or 2.0
    min_radius = max(1.0, thickness_reference * 0.75)
    max_radius = max(12.0, thickness_reference * 6.0)
    candidates: list[BendFeature] = []

    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomCylinder":
            continue

        radius = float(surface.Radius)
        if not min_radius <= radius <= max_radius:
            continue

        axis = _normalize_vector(surface.Axis)
        length = _candidate_depth_from_bbox(face.BoundBox, axis)
        if length < parameters.bend_min_length_mm:
            continue

        candidates.append(
            BendFeature(
                type="simple flange",
                radius_mm=round(radius, 2),
                length_mm=round(length, 2),
                axis=_rounded_vector(axis),
                center=_rounded_vector(_vector_tuple(surface.Center)),
                confidence="medium",
            ),
        )

    bends: list[BendFeature] = []
    for left_index, left in enumerate(candidates):
        for right in candidates[left_index + 1 :]:
            if not _bend_pair_matches(left, right, thickness_reference, parameters):
                continue

            inner, outer = sorted(
                (left, right),
                key=lambda candidate: candidate.radius_mm or 0.0,
            )
            center = tuple(
                (a + b) / 2.0
                for a, b in zip(inner.center or [], outer.center or [])
            )
            _append_unique_bend(
                bends,
                BendFeature(
                    type="simple flange",
                    radius_mm=inner.radius_mm,
                    length_mm=round(max(inner.length_mm or 0.0, outer.length_mm or 0.0), 2),
                    axis=inner.axis,
                    center=_rounded_vector(center),
                    confidence="high",
                ),
            )

    if not bends:
        for candidate in candidates:
            _append_unique_bend(bends, candidate)

    bends.sort(key=lambda bend: bend.center or [])
    return bends


def _complexity_score(shape, circular_count: int, bend_count: int) -> str:
    face_count = len(shape.Faces)
    cylindrical_face_count = sum(
        1
        for face in shape.Faces
        if getattr(face.Surface, "TypeId", "") == "Part::GeomCylinder"
    )
    if (
        face_count >= 150
        or cylindrical_face_count >= 40
        or circular_count >= 20
        or bend_count >= 8
    ):
        return "high"
    if face_count >= 40 or cylindrical_face_count >= 10 or circular_count >= 6 or bend_count >= 1:
        return "medium"
    return "low"


def _detect_cutting_lengths(shape, circular: list[HoleFeature], elongated: list[HoleFeature], polygonal: list[HoleFeature]):
    warnings = [
        "Cut length is preliminary: outer loop is selected from the longest planar external wire and inner loop length is derived from deduplicated detected features."
    ]

    outer_candidates = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue
        if not face.Wires:
            continue

        outer_wire = face.Wires[0]
        if not outer_wire.isClosed():
            continue
        length = float(outer_wire.Length)
        if length <= 20.0:
            continue
        outer_candidates.append(length)

    outer_cut_length = round(max(outer_candidates), 2) if outer_candidates else None
    inner_cut_length = 0.0

    for hole in circular:
        if hole.diameter_mm is not None:
            inner_cut_length += math.pi * float(hole.diameter_mm)
    for slot in elongated:
        if slot.length_mm is not None:
            inner_cut_length += float(slot.length_mm)
    for polygon in polygonal:
        if polygon.max_dimension_mm is not None:
            inner_cut_length += float(polygon.max_dimension_mm)

    inner_cut_length = round(inner_cut_length, 2) if inner_cut_length > 0 else None
    total_cut_length = (
        round(outer_cut_length + inner_cut_length, 2)
        if outer_cut_length is not None and inner_cut_length is not None
        else None
    )

    confidence = "medium" if total_cut_length is not None else "low"
    if outer_cut_length is None:
        warnings.append("Outer cut length could not be identified from a closed planar external wire.")
    if inner_cut_length is None:
        warnings.append("Inner cut length could not be derived from reliable detected hole features.")

    return outer_cut_length, inner_cut_length, total_cut_length, confidence, warnings


def _detect_sheet_thickness(
    shape,
    declared_thickness_mm: float | None = None,
) -> tuple[float | None, str]:
    planes = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue

        normal = _normalize_vector(surface.Axis)
        point = _vector_tuple(surface.Position)
        planes.append(
            {
                "area": float(face.Area),
                "normal": normal,
                "offset": _plane_offset(normal, point),
            }
        )

    candidates: list[float] = []
    for left_index, left in enumerate(planes):
        for right in planes[left_index + 1 :]:
            alignment = _dot(left["normal"], right["normal"])
            if abs(alignment) < 0.98:
                continue

            distance = (
                abs(left["offset"] - right["offset"])
                if alignment > 0
                else abs(left["offset"] + right["offset"])
            )
            if not 1.0 <= distance <= 5.0:
                continue

            area_ratio = min(left["area"], right["area"]) / max(
                left["area"],
                right["area"],
            )
            if area_ratio < 0.85:
                continue

            candidates.append(round(distance, 2))

    if not candidates:
        return None, "low"

    grouped: dict[float, int] = {}
    for candidate in candidates:
        grouped[candidate] = grouped.get(candidate, 0) + 1

    dominant_value, dominant_count = max(
        grouped.items(),
        key=lambda item: (item[1], -item[0]),
    )
    confidence = "medium"
    if dominant_count >= 2:
        confidence = "high"
    if declared_thickness_mm is not None and abs(dominant_value - declared_thickness_mm) <= 0.25:
        confidence = "high"

    return dominant_value, confidence


def _base_response(
    *,
    source_file: str,
    material: str | None,
    density_g_cm3: float | None,
    declared_thickness_mm: float | None,
) -> CadAnalysisResponse:
    return CadAnalysisResponse(
        part_name=Path(source_file).stem,
        source_file=source_file,
        declared_material=material,
        density_g_cm3=density_g_cm3,
        declared_thickness_mm=declared_thickness_mm,
    )


def analyze_step_file(
    *,
    file_bytes: bytes,
    source_file: str,
    material: str | None = None,
    density_g_cm3: float | None = None,
    declared_thickness_mm: float | None = None,
    quantity: int = 1,
) -> CadAnalysisResponse:
    response = _base_response(
        source_file=source_file,
        material=material,
        density_g_cm3=density_g_cm3,
        declared_thickness_mm=declared_thickness_mm,
    )

    suffix = Path(source_file).suffix.lower()
    if suffix not in VALID_STEP_SUFFIXES:
        response.warnings.append("Unsupported CAD extension. Only .stp and .step files are accepted.")
        return response

    if not file_bytes:
        response.warnings.append("Uploaded CAD file is empty.")
        return response

    status = get_freecad_status()
    if not status.available:
        response.warnings.append(f"FreeCAD is not available: {status.error}")
        return response

    _configure_freecad_path()
    Part = importlib.import_module("Part")
    analysis_parameters = load_analysis_config()

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            temp_file.write(file_bytes)
            temp_path = temp_file.name

        shape = Part.Shape()
        shape.read(temp_path)

        bbox = shape.BoundBox
        response.raw_bounding_box_mm = Dimensions(
            x=_round_or_none(bbox.XLength),
            y=_round_or_none(bbox.YLength),
            z=_round_or_none(bbox.ZLength),
        )
        response.effective_dimensions_mm = response.raw_bounding_box_mm
        response.volume_cm3 = _round_or_none(shape.Volume / 1000.0)
        response.surface_area_cm2 = _round_or_none(shape.Area / 100.0)

        if response.volume_cm3 is not None and density_g_cm3 is not None:
            response.estimated_weight_kg = _round_or_none(
                response.volume_cm3 * density_g_cm3 / 1000.0
            )

        detected_thickness, thickness_confidence = _detect_sheet_thickness(
            shape,
            declared_thickness_mm=declared_thickness_mm,
        )
        response.detected_thickness_mm = detected_thickness
        response.thickness_confidence = thickness_confidence

        response.holes.circular = _detect_circular_holes(shape, analysis_parameters)
        response.holes.elongated = _detect_elongated_holes(shape)
        response.holes.polygonal = _detect_polygonal_holes(shape)
        if len(response.holes.circular) >= 4:
            response.holes.confidence = "medium"
        if len(response.holes.elongated) >= 2:
            response.holes.confidence = "medium"
        if len(response.holes.polygonal) >= 2:
            response.holes.confidence = "medium"

        if not response.holes.circular:
            response.warnings.append(
                "Circular hole detection found no high-confidence candidates in the configured diameter range."
            )

        response.bends.items = _detect_bends(
            shape,
            detected_thickness,
            analysis_parameters,
        )
        if response.bends.items:
            response.bends.count = len(response.bends.items)
            response.bends.confidence = (
                "high"
                if response.bends.count >= 2
                and all(item.confidence == "high" for item in response.bends.items)
                else "medium"
            )
        else:
            response.bends.count = 0
            response.bends.confidence = "medium"

        response.complexity_score = _complexity_score(
            shape,
            len(response.holes.circular),
            response.bends.count,
        )
        if response.complexity_score == "high":
            response.warnings.append(
                "Complex sheet-metal part: bend detection may be incomplete"
            )
        if len(response.holes.circular) >= 20:
            response.warnings.append(
                "High number of circular features detected: hole deduplication applied"
            )

        (
            response.cutting.outer_cut_length_mm,
            response.cutting.inner_cut_length_mm,
            response.cutting.total_cut_length_mm,
            response.cutting.confidence,
            response.cutting.warnings,
        ) = _detect_cutting_lengths(
            shape,
            response.holes.circular,
            response.holes.elongated,
            response.holes.polygonal,
        )

        if response.detected_thickness_mm is None:
            response.warnings.append(
                "Detected thickness is not reported because wall-thickness inference is not reliable for this model."
            )
    except Exception as exc:
        response.warnings.append(f"FreeCAD failed to parse the STEP file: {exc}")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    return response
