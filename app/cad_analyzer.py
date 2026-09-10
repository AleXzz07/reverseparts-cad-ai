from __future__ import annotations

import importlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .schemas import (
    AssemblyAnalysis,
    AssemblyComponent,
    AssemblyPassage,
    BendFeature,
    CadAnalysisResponse,
    Dimensions,
    FlatPattern,
    HoleFeature,
    Holes,
    PartClassification,
    WeldEvidence,
)
from .sheetmetal_unfolder import (
    propagate_openings_and_hole_to_bend,
    singleton_bend_adjacent_faces,
    unfold_sheet,
)


VALID_STEP_SUFFIXES = {".stp", ".step"}
FALLBACK_CYLINDER_MIN_DIAMETER_MM = 4.0
FALLBACK_CYLINDER_MAX_DIAMETER_MM = 20.0
UNKNOWN_HOLE_WARNING = (
    "Some openings were detected but their shape could not be classified "
    "with confidence."
)
UNKNOWN_HOLE_REASON = (
    "Hole/opening detected but shape classification is uncertain"
)
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
    opening_min_dimension_mm: float
    opening_max_dimension_mm: float
    opening_min_perimeter_mm: float
    opening_max_perimeter_mm: float
    bend_center_tolerance_mm: float
    bend_radius_pair_tolerance_mm: float
    bend_axis_angle_tolerance_deg: float
    bend_min_length_mm: float
    assembly_contact_tolerance_mm: float
    assembly_passage_center_tolerance_mm: float
    assembly_passage_diameter_tolerance_mm: float
    assembly_passage_axial_gap_tolerance_mm: float
    flat_pattern_k_factor: float
    flat_pattern_max_simple_parallel_bends: int
    flat_pattern_face_pair_distance_tolerance_mm: float
    flat_pattern_edge_match_tolerance_mm: float
    flat_pattern_width_consistency_tolerance_mm: float
    flat_pattern_continuity_tolerance_mm: float
    flat_pattern_max_overlap_area_mm2: float
    flat_pattern_max_area_coherence_error_pct: float
    flat_pattern_max_perimeter_coherence_error_pct: float


def load_analysis_config(path: Path = DEFAULT_ANALYSIS_CONFIG_PATH) -> AnalysisParameters:
    data = json.loads(path.read_text(encoding="utf-8"))
    hole = data["circular_hole_deduplication"]
    opening = data.get("planar_opening_detection", {})
    bend = data["bend_detection"]
    flat_pattern = data.get("flat_pattern", {})
    assembly = data.get("assembly_detection", {})
    parameters = AnalysisParameters(
        hole_center_tolerance_mm=float(hole["center_tolerance_mm"]),
        hole_diameter_tolerance_mm=float(hole["diameter_tolerance_mm"]),
        hole_axis_angle_tolerance_deg=float(hole["axis_angle_tolerance_deg"]),
        opening_min_dimension_mm=float(opening.get("min_dimension_mm", 0.5)),
        opening_max_dimension_mm=float(opening.get("max_dimension_mm", 1000.0)),
        opening_min_perimeter_mm=float(opening.get("min_perimeter_mm", 2.0)),
        opening_max_perimeter_mm=float(opening.get("max_perimeter_mm", 5000.0)),
        bend_center_tolerance_mm=float(bend["center_tolerance_mm"]),
        bend_radius_pair_tolerance_mm=float(bend["radius_pair_tolerance_mm"]),
        bend_axis_angle_tolerance_deg=float(bend["axis_angle_tolerance_deg"]),
        bend_min_length_mm=float(bend["min_length_mm"]),
        assembly_contact_tolerance_mm=float(assembly.get("contact_tolerance_mm", 0.05)),
        assembly_passage_center_tolerance_mm=float(
            assembly.get("passage_center_tolerance_mm", 0.25)
        ),
        assembly_passage_diameter_tolerance_mm=float(
            assembly.get("passage_diameter_tolerance_mm", 0.2)
        ),
        assembly_passage_axial_gap_tolerance_mm=float(
            assembly.get("passage_axial_gap_tolerance_mm", 0.1)
        ),
        flat_pattern_k_factor=float(flat_pattern.get("k_factor", 0.4)),
        flat_pattern_max_simple_parallel_bends=int(
            flat_pattern.get("max_simple_parallel_bends", 4)
        ),
        flat_pattern_face_pair_distance_tolerance_mm=float(
            flat_pattern.get("face_pair_distance_tolerance_mm", 0.08)
        ),
        flat_pattern_edge_match_tolerance_mm=float(
            flat_pattern.get("edge_match_tolerance_mm", 0.05)
        ),
        flat_pattern_width_consistency_tolerance_mm=float(
            flat_pattern.get("width_consistency_tolerance_mm", 0.25)
        ),
        flat_pattern_continuity_tolerance_mm=float(
            flat_pattern.get("continuity_tolerance_mm", 0.05)
        ),
        flat_pattern_max_overlap_area_mm2=float(
            flat_pattern.get("max_overlap_area_mm2", 0.05)
        ),
        flat_pattern_max_area_coherence_error_pct=float(
            flat_pattern.get("max_area_coherence_error_pct", 0.5)
        ),
        flat_pattern_max_perimeter_coherence_error_pct=float(
            flat_pattern.get("max_perimeter_coherence_error_pct", 1.0)
        ),
    )
    if not 0 < parameters.opening_min_dimension_mm <= parameters.opening_max_dimension_mm:
        raise ValueError("Invalid planar opening dimension limits in analysis config.")
    if not 0 < parameters.opening_min_perimeter_mm <= parameters.opening_max_perimeter_mm:
        raise ValueError("Invalid planar opening perimeter limits in analysis config.")
    if not 0 <= parameters.flat_pattern_k_factor <= 1:
        raise ValueError("Invalid flat-pattern K-factor in analysis config.")
    if parameters.flat_pattern_max_simple_parallel_bends < 0:
        raise ValueError("Invalid flat-pattern bend limit in analysis config.")
    if parameters.flat_pattern_face_pair_distance_tolerance_mm <= 0:
        raise ValueError("Invalid flat-pattern face-pair tolerance in analysis config.")
    if parameters.flat_pattern_edge_match_tolerance_mm <= 0:
        raise ValueError("Invalid flat-pattern edge tolerance in analysis config.")
    if parameters.flat_pattern_width_consistency_tolerance_mm <= 0:
        raise ValueError("Invalid flat-pattern width tolerance in analysis config.")
    if parameters.flat_pattern_continuity_tolerance_mm <= 0:
        raise ValueError("Invalid flat-pattern continuity tolerance in analysis config.")
    if parameters.assembly_contact_tolerance_mm < 0:
        raise ValueError("Invalid assembly contact tolerance in analysis config.")
    return parameters


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


def _mass_center_components(shape) -> tuple[float, float, float] | None:
    def components(value) -> tuple[float, float, float] | None:
        try:
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                result = tuple(float(item) for item in value[:3])
            else:
                result = tuple(
                    float(
                        getattr(value, lower)
                        if hasattr(value, lower)
                        else getattr(value, upper)
                    )
                    for lower, upper in (("x", "X"), ("y", "Y"), ("z", "Z"))
                )
        except (AttributeError, TypeError, ValueError):
            return None
        return result if all(math.isfinite(item) for item in result) else None

    def direct_center(item) -> tuple[float, float, float] | None:
        for attribute in ("CenterOfMass", "CenterOfGravity"):
            try:
                value = getattr(item, attribute)
                value = value() if callable(value) else value
            except (AttributeError, TypeError, ValueError):
                continue
            result = components(value)
            if result is not None:
                return result
        return None

    direct_result = direct_center(shape)
    if direct_result is not None:
        return direct_result

    weighted_centers: list[tuple[float, tuple[float, float, float]]] = []
    for solid in getattr(shape, "Solids", []) or []:
        center = direct_center(solid)
        try:
            volume = float(solid.Volume)
        except (AttributeError, TypeError, ValueError):
            continue
        if center is not None and math.isfinite(volume) and volume > 0:
            weighted_centers.append((volume, center))
    total_volume = sum(volume for volume, _ in weighted_centers)
    if total_volume <= 0:
        return None
    return tuple(
        sum(volume * center[index] for volume, center in weighted_centers) / total_volume
        for index in range(3)
    )


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
        and projected_offset
        <= max_depth
        + parameters.hole_center_tolerance_mm
        + parameters.hole_diameter_tolerance_mm
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


def _is_planar_opening_size_valid(
    *,
    dimension_mm: float,
    perimeter_mm: float,
    parameters: AnalysisParameters,
) -> bool:
    return (
        parameters.opening_min_dimension_mm
        <= dimension_mm
        <= parameters.opening_max_dimension_mm
        and parameters.opening_min_perimeter_mm
        <= perimeter_mm
        <= parameters.opening_max_perimeter_mm
    )


def _cylindrical_surface_span_deg(face) -> float | None:
    try:
        parameter_range = tuple(float(value) for value in face.ParameterRange)
        if len(parameter_range) < 2:
            return None
        angle = math.degrees(abs(parameter_range[1] - parameter_range[0]))
    except (AttributeError, TypeError, ValueError):
        return None
    return angle if math.isfinite(angle) else None


def _wire_shares_edge_with_face(wire, face) -> bool:
    """Use B-Rep adjacency when the STEP importer preserves edge identity."""
    for wire_edge in getattr(wire, "Edges", []) or []:
        for face_edge in getattr(face, "Edges", []) or []:
            try:
                if wire_edge.isSame(face_edge):
                    return True
            except (AttributeError, TypeError, RuntimeError):
                continue
    return False


def _circular_feature_matches_cylinder(
    feature: HoleFeature,
    cylinder: dict,
    parameters: AnalysisParameters,
    *,
    check_axial_bounds: bool = True,
) -> bool:
    if feature.diameter_mm is None or feature.center is None or feature.axis is None:
        return False
    if abs(feature.diameter_mm - cylinder["diameter"]) > parameters.hole_diameter_tolerance_mm:
        return False
    feature_axis = tuple(feature.axis)
    cylinder_axis = cylinder["axis"]
    if not _axis_aligned(
        feature_axis,
        cylinder_axis,
        tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
    ):
        return False
    delta = tuple(left - right for left, right in zip(tuple(feature.center), cylinder["center"]))
    projected = _dot(delta, cylinder_axis)
    radial = _vector_norm(
        tuple(component - projected * axis for component, axis in zip(delta, cylinder_axis))
    )
    if radial > parameters.hole_center_tolerance_mm:
        return False
    if not check_axial_bounds:
        return True
    return abs(projected) <= (
        cylinder["depth"]
        + parameters.hole_center_tolerance_mm
        + parameters.hole_diameter_tolerance_mm
    )


def _opposite_circular_contours_match(
    left: dict,
    right: dict,
    thickness_mm: float,
    parameters: AnalysisParameters,
) -> bool:
    """Fallback for STEP files whose cylindrical hole wall is split or absent.

    Coaxiality alone is deliberately insufficient: the contours must lie on
    opposite parallel skins separated by the detected sheet thickness.
    """
    left_feature = left["feature"]
    right_feature = right["feature"]
    if (
        left_feature.diameter_mm is None
        or right_feature.diameter_mm is None
        or left_feature.center is None
        or right_feature.center is None
        or left_feature.axis is None
        or right_feature.axis is None
    ):
        return False
    if abs(left_feature.diameter_mm - right_feature.diameter_mm) > parameters.hole_diameter_tolerance_mm:
        return False
    left_axis = tuple(left_feature.axis)
    right_axis = tuple(right_feature.axis)
    if not _axis_aligned(
        left_axis,
        right_axis,
        tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
    ):
        return False
    center_delta = tuple(
        left_value - right_value
        for left_value, right_value in zip(left_feature.center, right_feature.center)
    )
    projected = _dot(center_delta, left_axis)
    radial_offset = _vector_norm(
        tuple(component - projected * axis for component, axis in zip(center_delta, left_axis))
    )
    if radial_offset > parameters.hole_center_tolerance_mm:
        return False
    left_reference = _planar_face_reference(left["face"])
    right_reference = _planar_face_reference(right["face"])
    if left_reference is None or right_reference is None:
        return False
    left_normal, left_offset = left_reference
    right_normal, right_offset = right_reference
    if not _axis_aligned(
        left_normal,
        right_normal,
        tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
    ):
        return False
    separation = _parallel_plane_distance(
        left_normal,
        left_offset,
        right_normal,
        right_offset,
    )
    return abs(separation - thickness_mm) <= max(
        0.25,
        parameters.hole_diameter_tolerance_mm,
    )


def _detect_circular_holes(
    shape,
    parameters: AnalysisParameters,
    detected_thickness_mm: float | None = None,
) -> tuple[list[HoleFeature], int]:
    cylinder_candidates: list[dict] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomCylinder":
            continue

        radius = float(surface.Radius)
        diameter = radius * 2.0
        if not FALLBACK_CYLINDER_MIN_DIAMETER_MM <= diameter <= FALLBACK_CYLINDER_MAX_DIAMETER_MM:
            continue

        axis = _normalize_vector(surface.Axis)
        center = _vector_tuple(surface.Center)
        depth = _candidate_depth_from_bbox(face.BoundBox, axis)
        angular_span = _cylindrical_surface_span_deg(face)
        if depth <= 0.1 or angular_span is None or angular_span < 350.0:
            continue
        cylinder_candidates.append(
            {
                "face": face,
                "radius": radius,
                "diameter": diameter,
                "center": center,
                "axis": axis,
                "depth": depth,
            }
        )

    planar_wire_candidates: list[dict] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue
        for wire_index, wire in enumerate(face.Wires):
            if wire_index == 0 or not wire.isClosed():
                continue
            edges = list(wire.Edges)
            if not edges or any(_curve_type(edge) != "Part::GeomCircle" for edge in edges):
                continue

            radii = [float(edge.Curve.Radius) for edge in edges]
            if max(radii) - min(radii) > parameters.hole_diameter_tolerance_mm / 2.0:
                continue
            diameter = sum(radii) / len(radii) * 2.0
            if not _is_planar_opening_size_valid(
                dimension_mm=diameter,
                perimeter_mm=float(wire.Length),
                parameters=parameters,
            ):
                continue

            planar_wire_candidates.append(
                {
                    "face": face,
                    "wire": wire,
                    "feature": HoleFeature(
                    diameter_mm=round(diameter, 2),
                    radius_mm=round(diameter / 2.0, 2),
                    perimeter_mm=round(math.pi * diameter, 2),
                    circumference_mm=round(math.pi * diameter, 2),
                    area_mm2=round(math.pi * (diameter / 2.0) ** 2, 2),
                    center=_rounded_vector(_wire_center(wire)),
                    axis=_rounded_vector(_normalize_vector(surface.Axis)),
                    confidence="high",
                    ),
                }
            )

    holes: list[HoleFeature] = []
    used_planar_indexes: set[int] = set()

    # Primary path: a full cylindrical wall connected to one or both planar
    # opening contours is the strongest evidence of one physical through-hole.
    for cylinder in cylinder_candidates:
        bounded_geometric_matches = [
            index
            for index, record in enumerate(planar_wire_candidates)
            if _circular_feature_matches_cylinder(record["feature"], cylinder, parameters)
        ]
        topological_matches = [
            index
            for index, record in enumerate(planar_wire_candidates)
            if _circular_feature_matches_cylinder(
                record["feature"],
                cylinder,
                parameters,
                check_axial_bounds=False,
            )
            if _wire_shares_edge_with_face(
                planar_wire_candidates[index]["wire"],
                cylinder["face"],
            )
        ]
        if topological_matches:
            # OCC's infinite-cylinder origin is not guaranteed to lie at an
            # end of the bounded face. Anchor the axial check to a real shared
            # rim, then absorb only the opposite rim within this wall's depth.
            anchored_geometric_matches = []
            for index, record in enumerate(planar_wire_candidates):
                if not _circular_feature_matches_cylinder(
                    record["feature"],
                    cylinder,
                    parameters,
                    check_axial_bounds=False,
                ):
                    continue
                record_center = tuple(record["feature"].center or [])
                if any(
                    _projected_distance(
                        record_center,
                        tuple(planar_wire_candidates[anchor]["feature"].center or []),
                        cylinder["axis"],
                    )
                    <= cylinder["depth"]
                    + parameters.hole_center_tolerance_mm
                    + parameters.hole_diameter_tolerance_mm
                    for anchor in topological_matches
                ):
                    anchored_geometric_matches.append(index)
            matches = topological_matches + [
                index
                for index in anchored_geometric_matches
                if index not in topological_matches
            ]
        else:
            matches = bounded_geometric_matches
        if not matches:
            continue
        available_matches = [index for index in matches if index not in used_planar_indexes]
        if not available_matches:
            continue
        centers = [
            tuple(planar_wire_candidates[index]["feature"].center or cylinder["center"])
            for index in available_matches
        ]
        center = tuple(sum(values) / len(values) for values in zip(*centers))
        diameter = cylinder["diameter"]
        radius = cylinder["radius"]
        holes.append(
            HoleFeature(
                diameter_mm=round(diameter, 2),
                radius_mm=round(radius, 2),
                perimeter_mm=round(math.pi * diameter, 2),
                circumference_mm=round(math.pi * diameter, 2),
                area_mm2=round(math.pi * radius * radius, 2),
                center=_rounded_vector(center),
                axis=_rounded_vector(cylinder["axis"]),
                depth_mm=round(cylinder["depth"], 3),
                confidence="high",
            )
        )
        used_planar_indexes.update(available_matches)

    # Fallback path: pair only opposite planar contours separated by the
    # detected sheet thickness. This supports split cylindrical walls while
    # refusing to merge unrelated coaxial holes.
    if detected_thickness_mm is not None:
        for left_index, left in enumerate(planar_wire_candidates):
            if left_index in used_planar_indexes:
                continue
            for right_index in range(left_index + 1, len(planar_wire_candidates)):
                if right_index in used_planar_indexes:
                    continue
                right = planar_wire_candidates[right_index]
                if not _opposite_circular_contours_match(
                    left,
                    right,
                    detected_thickness_mm,
                    parameters,
                ):
                    continue
                left_feature = left["feature"]
                right_feature = right["feature"]
                center = tuple(
                    (a + b) / 2.0
                    for a, b in zip(left_feature.center or [], right_feature.center or [])
                )
                merged = left_feature.model_copy(deep=True)
                merged.center = _rounded_vector(center)
                merged.depth_mm = round(detected_thickness_mm, 3)
                merged.confidence = "high"
                holes.append(merged)
                used_planar_indexes.update({left_index, right_index})
                break

    for index, record in enumerate(planar_wire_candidates):
        if index not in used_planar_indexes:
            holes.append(record["feature"])

    holes.sort(key=lambda hole: (hole.diameter_mm or 0.0, hole.center or []))
    return holes, len(cylinder_candidates)


def _circular_edge_geometry(edge) -> tuple[float, tuple[float, float, float]] | None:
    curve = getattr(edge, "Curve", None)
    if getattr(curve, "TypeId", "") != "Part::GeomCircle":
        return None
    try:
        radius = float(curve.Radius)
        center = _vector_tuple(curve.Center)
    except (AttributeError, TypeError, ValueError):
        return None
    if radius <= 0 or not math.isfinite(radius) or not all(
        math.isfinite(value) for value in center
    ):
        return None
    return radius, center


def _countersink_candidates(shape) -> list[dict]:
    candidates: list[dict] = []
    cylinder_faces = [
        face
        for face in shape.Faces
        if getattr(getattr(face, "Surface", None), "TypeId", "")
        == "Part::GeomCylinder"
    ]
    for face in shape.Faces:
        surface = getattr(face, "Surface", None)
        if getattr(surface, "TypeId", "") != "Part::GeomCone":
            continue
        angular_span = _cylindrical_surface_span_deg(face)
        if angular_span is None or angular_span < 350.0:
            continue
        try:
            axis = _normalize_vector(surface.Axis)
        except (AttributeError, TypeError, ValueError):
            continue

        circles = [
            geometry
            for edge in getattr(face, "Edges", []) or []
            if (geometry := _circular_edge_geometry(edge)) is not None
        ]
        distinct: list[tuple[float, tuple[float, float, float]]] = []
        for radius, center in sorted(circles, key=lambda item: item[0]):
            if not any(abs(radius - known_radius) <= 0.01 for known_radius, _ in distinct):
                distinct.append((radius, center))
        if len(distinct) < 2:
            continue
        minor_radius, minor_center = distinct[0]
        major_radius, major_center = distinct[-1]
        depth = _projected_distance(minor_center, major_center, axis)
        if major_radius <= minor_radius or depth <= 0.01:
            continue

        topology_supported = any(
            _wire_shares_edge_with_face(
                SimpleWireProxy(getattr(face, "Edges", []) or []),
                cylinder_face,
            )
            and abs(float(cylinder_face.Surface.Radius) - minor_radius) <= 0.1
            and _axis_aligned(
                axis,
                _normalize_vector(cylinder_face.Surface.Axis),
                tolerance=0.98,
            )
            for cylinder_face in cylinder_faces
        )
        candidates.append(
            {
                "face": face,
                "axis": axis,
                "minor_radius": minor_radius,
                "major_radius": major_radius,
                "minor_center": minor_center,
                "major_center": major_center,
                "depth": depth,
                "topology_supported": topology_supported,
            }
        )
    return candidates


class SimpleWireProxy:
    """Minimal edge container used by the B-Rep adjacency helper."""

    def __init__(self, edges) -> None:
        self.Edges = edges


def _annotate_countersunk_holes(
    shape,
    holes: list[HoleFeature],
    parameters: AnalysisParameters,
    thickness_mm: float | None,
) -> int:
    """Attach conical countersink geometry to an existing through-hole.

    A countersink is metadata on one physical circular opening. The legacy
    diameter/area/perimeter fields deliberately remain the through profile so
    downstream 2D cutting calculations do not count the conical removal.
    """
    annotated = 0
    used_hole_ids: set[int] = set()
    for candidate in _countersink_candidates(shape):
        if (
            not candidate["topology_supported"]
            and (
                thickness_mm is None
                or candidate["depth"]
                > thickness_mm + parameters.hole_diameter_tolerance_mm
            )
        ):
            continue
        matching: list[HoleFeature] = []
        for hole in holes:
            if id(hole) in used_hole_ids:
                continue
            if hole.diameter_mm is None or hole.center is None or hole.axis is None:
                continue
            if abs(hole.diameter_mm / 2.0 - candidate["minor_radius"]) > (
                parameters.hole_diameter_tolerance_mm / 2.0
            ):
                continue
            hole_axis = tuple(hole.axis)
            if not _axis_aligned(
                hole_axis,
                candidate["axis"],
                tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
            ):
                continue
            delta = tuple(
                left - right
                for left, right in zip(candidate["minor_center"], tuple(hole.center))
            )
            projected = _dot(delta, hole_axis)
            radial_offset = _vector_norm(
                tuple(
                    component - projected * axis_component
                    for component, axis_component in zip(delta, hole_axis)
                )
            )
            if radial_offset <= parameters.hole_center_tolerance_mm:
                matching.append(hole)
        if len(matching) != 1:
            continue
        hole = matching[0]
        through_diameter = float(hole.diameter_mm or 0.0)
        hole.type = "countersunk"
        hole.through_diameter_mm = round(through_diameter, 2)
        hole.countersink_major_diameter_mm = round(
            2.0 * candidate["major_radius"], 2
        )
        hole.countersink_depth_mm = round(candidate["depth"], 3)
        hole.confidence = "high" if candidate["topology_supported"] else "medium"
        used_hole_ids.add(id(hole))
        annotated += 1
    return annotated


def _wire_center(wire) -> tuple[float, float, float]:
    bbox = wire.BoundBox
    return (
        (float(bbox.XMin) + float(bbox.XMax)) / 2.0,
        (float(bbox.YMin) + float(bbox.YMax)) / 2.0,
        (float(bbox.ZMin) + float(bbox.ZMax)) / 2.0,
    )


def _wires_are_same(left, right) -> bool:
    if left is right:
        return True
    try:
        return bool(left.isSame(right))
    except (AttributeError, TypeError, RuntimeError):
        return False


def _face_outer_wire(face):
    try:
        outer_wire = face.OuterWire
        if outer_wire is not None:
            return outer_wire
    except (AttributeError, RuntimeError):
        pass
    wires = list(getattr(face, "Wires", []) or [])
    return wires[0] if wires else None


def _face_inner_wires(face) -> list:
    wires = list(getattr(face, "Wires", []) or [])
    outer_wire = _face_outer_wire(face)
    if outer_wire is None:
        return []
    return [wire for wire in wires if not _wires_are_same(wire, outer_wire)]


def _planar_face_reference(face) -> tuple[tuple[float, float, float], float] | None:
    surface = getattr(face, "Surface", None)
    if getattr(surface, "TypeId", "") != "Part::GeomPlane":
        return None
    try:
        normal = _normalize_vector(surface.Axis)
        offset = _plane_offset(normal, _vector_tuple(surface.Position))
    except (AttributeError, TypeError, ValueError):
        return None
    return normal, offset


def _parallel_plane_distance(
    left_normal: tuple[float, float, float],
    left_offset: float,
    right_normal: tuple[float, float, float],
    right_offset: float,
) -> float:
    alignment = _dot(left_normal, right_normal)
    return (
        abs(left_offset - right_offset)
        if alignment >= 0
        else abs(left_offset + right_offset)
    )


def _wire_bbox_dimensions(wire) -> tuple[float, float, float]:
    bbox = wire.BoundBox
    return (
        float(bbox.XLength),
        float(bbox.YLength),
        float(bbox.ZLength),
    )


def _matching_opposite_wire(
    shape,
    source_face,
    source_wire,
    parameters: AnalysisParameters,
    thickness_mm: float | None,
) -> bool:
    """Check that an inner contour is repeated on the opposite sheet skin.

    Bend-transition boundaries can be inner wires on one planar face even though
    they are not manufacturing openings. Geometric measurements are compared
    instead of raw edge counts because STEP exporters may split equal curves.
    """
    source_reference = _planar_face_reference(source_face)
    if source_reference is None:
        return False
    source_normal, source_offset = source_reference
    source_center = _wire_center(source_wire)
    source_perimeter = float(source_wire.Length)
    source_dimensions = sorted(
        value for value in _wire_bbox_dimensions(source_wire) if value > 1e-6
    )

    for other_face in shape.Faces:
        if other_face is source_face:
            continue
        other_reference = _planar_face_reference(other_face)
        if other_reference is None:
            continue
        other_normal, other_offset = other_reference
        if not _axis_aligned(
            source_normal,
            other_normal,
            tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
        ):
            continue

        plane_distance = _parallel_plane_distance(
            source_normal,
            source_offset,
            other_normal,
            other_offset,
        )
        if thickness_mm is not None:
            thickness_tolerance = max(0.25, thickness_mm * 0.15)
            if abs(plane_distance - thickness_mm) > thickness_tolerance:
                continue
        elif not 0.5 <= plane_distance <= 6.0:
            continue

        for other_wire in _face_inner_wires(other_face):
            if not other_wire.isClosed():
                continue
            other_perimeter = float(other_wire.Length)
            perimeter_tolerance = max(0.5, source_perimeter * 0.015)
            if abs(source_perimeter - other_perimeter) > perimeter_tolerance:
                continue

            other_dimensions = sorted(
                value for value in _wire_bbox_dimensions(other_wire) if value > 1e-6
            )
            if len(source_dimensions) != len(other_dimensions):
                continue
            if any(
                abs(left - right) > max(0.5, max(left, right) * 0.02)
                for left, right in zip(source_dimensions, other_dimensions)
            ):
                continue

            center_delta = tuple(
                left - right
                for left, right in zip(source_center, _wire_center(other_wire))
            )
            signed_axial_offset = _dot(center_delta, source_normal)
            radial_offset = _vector_norm(
                tuple(
                    component - signed_axial_offset * axis
                    for component, axis in zip(center_delta, source_normal)
                )
            )
            if radial_offset <= parameters.hole_center_tolerance_mm:
                return True
    return False


def _planar_wire_area(wire) -> float | None:
    try:
        Part = importlib.import_module("Part")
        return round(float(Part.Face(wire).Area), 3)
    except Exception:
        # FreeCAD raises Part.OCCError for wires that cannot form a valid face.
        # Area is optional metadata, so a failure must not stop CAD analysis.
        return None


def _slot_orientation_axis_from_arc_centers(
    arc_centers: list[tuple[float, float, float]],
) -> tuple[float, float, float]:
    if len(arc_centers) != 2:
        return (0.0, 0.0, 0.0)
    first, second = arc_centers
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

    orientation_matches = True
    if candidate.orientation_axis is not None and existing.orientation_axis is not None:
        # Slot orientation is axial: [x, y, z] and [-x, -y, -z] describe the
        # same longitudinal direction and must deduplicate as one opening.
        orientation_matches = _axis_aligned(
            tuple(candidate.orientation_axis),
            tuple(existing.orientation_axis),
            tolerance=0.95,
        )
    elif candidate.orientation_axis is not None or existing.orientation_axis is not None:
        orientation_matches = False

    return (
        abs(candidate.length_mm - existing.length_mm) <= 0.5
        and abs(candidate.width_mm - existing.width_mm) <= 0.3
        and _axis_aligned(tuple(candidate.axis), tuple(existing.axis), tolerance=0.95)
        and orientation_matches
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


def _is_duplicate_rounded_rectangle(
    candidate: HoleFeature,
    existing: HoleFeature,
) -> bool:
    if any(
        value is None
        for value in (
            candidate.overall_length_mm,
            existing.overall_length_mm,
            candidate.width_mm,
            existing.width_mm,
            candidate.corner_radius_mm,
            existing.corner_radius_mm,
            candidate.center,
            existing.center,
            candidate.axis,
            existing.axis,
        )
    ):
        return False
    return (
        abs(float(candidate.overall_length_mm) - float(existing.overall_length_mm)) <= 0.5
        and abs(float(candidate.width_mm) - float(existing.width_mm)) <= 0.5
        and abs(float(candidate.corner_radius_mm) - float(existing.corner_radius_mm)) <= 0.3
        and _axis_aligned(tuple(candidate.axis or []), tuple(existing.axis or []), tolerance=0.95)
        and _vector_norm(
            tuple(
                left - right
                for left, right in zip(candidate.center or [], existing.center or [])
            )
        )
        <= 3.0
    )


def _append_unique_rounded_rectangle(
    openings: list[HoleFeature],
    candidate: HoleFeature,
) -> None:
    for existing in openings:
        if _is_duplicate_rounded_rectangle(candidate, existing):
            existing.center = _rounded_vector(
                tuple(
                    (left + right) / 2.0
                    for left, right in zip(existing.center or [], candidate.center or [])
                )
            )
            existing.confidence = "high"
            return
    openings.append(candidate)


def _detect_rounded_rectangular_holes(
    shape,
    parameters: AnalysisParameters,
    thickness_mm: float | None = None,
) -> list[HoleFeature]:
    openings: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue

        for wire in _face_inner_wires(face):
            if not wire.isClosed():
                continue
            arcs = [edge for edge in wire.Edges if _curve_type(edge) == "Part::GeomCircle"]
            lines = [edge for edge in wire.Edges if _curve_type(edge) == "Part::GeomLine"]
            if len(arcs) < 4 or len(lines) != 4 or len(arcs) + len(lines) != len(wire.Edges):
                continue

            radii = [float(edge.Curve.Radius) for edge in arcs]
            if max(radii) - min(radii) > parameters.hole_diameter_tolerance_mm / 2.0:
                continue
            corner_radius = sum(radii) / len(radii)

            arc_centers: list[tuple[float, float, float]] = []
            for edge in arcs:
                center = _vector_tuple(edge.Curve.Center)
                if not any(
                    _vector_norm(tuple(a - b for a, b in zip(center, existing)))
                    <= parameters.hole_center_tolerance_mm
                    for existing in arc_centers
                ):
                    arc_centers.append(center)
            # Four distinct tangent corner arcs distinguish this feature from a slot.
            if len(arc_centers) != 4:
                continue

            directions = [_normalize_vector(edge.Curve.Direction) for edge in lines]
            first_family = [
                direction
                for direction in directions
                if _axis_aligned(direction, directions[0], tolerance=0.98)
            ]
            second_family = [direction for direction in directions if direction not in first_family]
            if len(first_family) != 2 or len(second_family) != 2:
                continue
            if not _axis_aligned(second_family[0], second_family[1], tolerance=0.98):
                continue
            if abs(_dot(first_family[0], second_family[0])) > 0.05:
                continue

            line_lengths = sorted(float(edge.Length) for edge in lines)
            length_tolerance = max(0.5, line_lengths[-1] * 0.02)
            if (
                abs(line_lengths[0] - line_lengths[1]) > length_tolerance
                or abs(line_lengths[2] - line_lengths[3]) > length_tolerance
            ):
                continue
            short_straight = (line_lengths[0] + line_lengths[1]) / 2.0
            long_straight = (line_lengths[2] + line_lengths[3]) / 2.0
            width = short_straight + 2.0 * corner_radius
            overall_length = long_straight + 2.0 * corner_radius
            perimeter = float(wire.Length)
            if not _is_planar_opening_size_valid(
                dimension_mm=overall_length,
                perimeter_mm=perimeter,
                parameters=parameters,
            ):
                continue
            if not _matching_opposite_wire(shape, face, wire, parameters, thickness_mm):
                continue

            area = _planar_wire_area(wire)
            if area is None:
                area = (
                    overall_length * width
                    - (4.0 - math.pi) * corner_radius**2
                )
            bbox = wire.BoundBox
            _append_unique_rounded_rectangle(
                openings,
                HoleFeature(
                    type="rounded rectangular opening",
                    max_dimension_mm=round(overall_length, 3),
                    bounding_box_mm=Dimensions(
                        x=round(float(bbox.XLength), 3),
                        y=round(float(bbox.YLength), 3),
                        z=round(float(bbox.ZLength), 3),
                    ),
                    perimeter_mm=round(perimeter, 2),
                    area_mm2=round(float(area), 2),
                    overall_length_mm=round(overall_length, 2),
                    width_mm=round(width, 2),
                    corner_radius_mm=round(corner_radius, 2),
                    center=_rounded_vector(_wire_center(wire)),
                    axis=_rounded_vector(_normalize_vector(surface.Axis)),
                    confidence="medium",
                ),
            )

    openings.sort(key=lambda opening: opening.center or [])
    return openings


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


def _detect_elongated_holes(
    shape,
    parameters: AnalysisParameters,
    thickness_mm: float | None = None,
) -> list[HoleFeature]:
    slots: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue

        for wire in _face_inner_wires(face):
            if not wire.isClosed():
                continue

            arcs = [edge for edge in wire.Edges if _curve_type(edge) == "Part::GeomCircle"]
            lines = [edge for edge in wire.Edges if _curve_type(edge) == "Part::GeomLine"]
            if len(arcs) < 2 or len(lines) != 2 or len(arcs) + len(lines) != len(wire.Edges):
                continue

            radii = [float(edge.Curve.Radius) for edge in arcs]
            if max(radii) - min(radii) > parameters.hole_diameter_tolerance_mm / 2.0:
                continue

            arc_centers: list[tuple[float, float, float]] = []
            for edge in arcs:
                center = _vector_tuple(edge.Curve.Center)
                if not any(
                    _vector_norm(tuple(a - b for a, b in zip(center, existing)))
                    <= parameters.hole_center_tolerance_mm
                    for existing in arc_centers
                ):
                    arc_centers.append(center)
            if len(arc_centers) != 2:
                continue

            width = sum(radii) / len(radii) * 2.0
            line_directions = [_normalize_vector(edge.Curve.Direction) for edge in lines]
            if not _axis_aligned(line_directions[0], line_directions[1], tolerance=0.98):
                continue
            if not _matching_opposite_wire(
                shape,
                face,
                wire,
                parameters,
                thickness_mm,
            ):
                continue

            length = float(wire.Length)
            if not _is_planar_opening_size_valid(
                dimension_mm=width,
                perimeter_mm=length,
                parameters=parameters,
            ):
                continue

            orientation_axis = _slot_orientation_axis_from_arc_centers(arc_centers)
            straight_length = _vector_norm(
                tuple(left - right for left, right in zip(arc_centers[0], arc_centers[1]))
            )
            overall_length = straight_length + width
            slot_area = straight_length * width + math.pi * (width / 2.0) ** 2
            if _vector_norm(orientation_axis) == 0:
                orientation_axis = line_directions[0]

            _append_unique_slot(
                slots,
                HoleFeature(
                    # Kept for compatibility with validated historical ground truth.
                    length_mm=round(length, 2),
                    overall_length_mm=round(overall_length, 2),
                    straight_length_mm=round(straight_length, 2),
                    end_radius_mm=round(width / 2.0, 2),
                    width_mm=round(width, 2),
                    perimeter_mm=round(length, 2),
                    area_mm2=round(slot_area, 2),
                    center=_rounded_vector(_wire_center(wire)),
                    axis=_rounded_vector(_normalize_vector(surface.Axis)),
                    orientation_axis=_rounded_vector(orientation_axis),
                    confidence="medium",
                ),
            )

    slots.sort(key=lambda slot: slot.center or [])
    return slots


def _detect_polygonal_holes(
    shape,
    parameters: AnalysisParameters,
    thickness_mm: float | None = None,
) -> list[HoleFeature]:
    polygons: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue

        for wire in _face_inner_wires(face):
            if not wire.isClosed():
                continue

            edges = list(wire.Edges)
            if len(edges) < 3:
                continue
            if any(_curve_type(edge) != "Part::GeomLine" for edge in edges):
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

            perimeter = float(wire.Length)
            if not _is_planar_opening_size_valid(
                dimension_mm=max(nonzero_bbox_dimensions),
                perimeter_mm=perimeter,
                parameters=parameters,
            ):
                continue
            if not _matching_opposite_wire(
                shape,
                face,
                wire,
                parameters,
                thickness_mm,
            ):
                continue

            _append_unique_polygon(
                polygons,
                HoleFeature(
                    num_sides=len(edges),
                    max_dimension_mm=round(max(nonzero_bbox_dimensions), 3),
                    bounding_box_mm=bbox_dimensions,
                    perimeter_mm=round(perimeter, 2),
                    area_mm2=_planar_wire_area(wire),
                    center=_rounded_vector(_wire_center(wire)),
                    axis=_rounded_vector(_normalize_vector(surface.Axis)),
                    confidence="medium",
                ),
            )

        outer_wire = _face_outer_wire(face)
        if outer_wire is None or not outer_wire.isClosed():
            continue
        outer_edges = list(outer_wire.Edges)
        if len(outer_edges) != 6:
            continue
        if any(_curve_type(edge) != "Part::GeomLine" for edge in outer_edges):
            continue
        if not 20.0 <= float(outer_wire.Length) <= 40.0 or float(face.Area) > 100.0:
            continue

        bbox = outer_wire.BoundBox
        bbox_dimensions = Dimensions(
            x=round(float(bbox.XLength), 3),
            y=round(float(bbox.YLength), 3),
            z=round(float(bbox.ZLength), 3),
        )
        _append_unique_polygon(
            polygons,
            HoleFeature(
                num_sides=6,
                max_dimension_mm=round(
                    max(
                        float(bbox.XLength),
                        float(bbox.YLength),
                        float(bbox.ZLength),
                    ),
                    3,
                ),
                bounding_box_mm=bbox_dimensions,
                perimeter_mm=round(float(outer_wire.Length), 2),
                area_mm2=_planar_wire_area(outer_wire),
                center=_rounded_vector(_wire_center(outer_wire)),
                axis=_rounded_vector(_normalize_vector(surface.Axis)),
                confidence="medium",
            ),
        )

    polygons.sort(key=lambda polygon: polygon.center or [])
    return polygons


def _detect_formed_holes(
    shape,
    parameters: AnalysisParameters,
) -> list[HoleFeature]:
    formed: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue
        for wire_index, wire in enumerate(face.Wires):
            if wire_index == 0 or not wire.isClosed():
                continue
            edge_types = {_curve_type(edge) for edge in wire.Edges}
            if "Part::GeomBSplineCurve" not in edge_types:
                continue

            candidate = HoleFeature(
                type="raised collar opening",
                length_mm=round(float(wire.Length), 2),
                perimeter_mm=round(float(wire.Length), 2),
                area_mm2=_planar_wire_area(wire),
                center=_rounded_vector(_wire_center(wire)),
                axis=_rounded_vector(_normalize_vector(surface.Axis)),
                confidence="medium",
            )
            duplicate = False
            for existing in formed:
                if (
                    existing.center is not None
                    and existing.axis is not None
                    and candidate.center is not None
                    and candidate.axis is not None
                    and _axis_aligned(
                        tuple(existing.axis),
                        tuple(candidate.axis),
                        tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
                    )
                    and _vector_norm(
                        tuple(a - b for a, b in zip(existing.center, candidate.center))
                    )
                    <= 3.0
                ):
                    existing.confidence = "high"
                    duplicate = True
                    break
            if not duplicate:
                formed.append(candidate)

    formed.sort(key=lambda hole: hole.center or [])
    return formed


def _center_matches_feature(
    center: tuple[float, float, float],
    feature: HoleFeature,
    tolerance_mm: float = 3.0,
) -> bool:
    if feature.center is None:
        return False
    return (
        _vector_norm(
            tuple(
                left - right
                for left, right in zip(center, tuple(feature.center))
            )
        )
        <= tolerance_mm
    )


def _append_unique_unknown(
    unknown: list[HoleFeature],
    candidate: HoleFeature,
) -> None:
    for existing in unknown:
        if (
            existing.center is not None
            and candidate.center is not None
            and existing.axis is not None
            and candidate.axis is not None
            and existing.max_dimension_mm is not None
            and candidate.max_dimension_mm is not None
            and abs(
                existing.max_dimension_mm - candidate.max_dimension_mm
            )
            <= 1.0
            and _axis_aligned(
                tuple(existing.axis),
                tuple(candidate.axis),
                tolerance=0.95,
            )
            and _vector_norm(
                tuple(
                    left - right
                    for left, right in zip(
                        existing.center,
                        candidate.center,
                    )
                )
            )
            <= 3.0
        ):
            existing.confidence = "medium"
            return
    unknown.append(candidate)


def _wire_circular_diameter(wire) -> float | None:
    edges = list(getattr(wire, "Edges", []) or [])
    if not edges:
        return None
    radii = []
    for edge in edges:
        geometry = _circular_edge_geometry(edge)
        if geometry is None:
            return None
        radii.append(geometry[0])
    if max(radii) - min(radii) > 0.1:
        return None
    return 2.0 * sum(radii) / len(radii)


def _feature_profile_diameters(feature: HoleFeature) -> list[float]:
    values = [feature.diameter_mm]
    if feature.type == "countersunk":
        values.extend(
            [
                feature.through_diameter_mm,
                feature.countersink_major_diameter_mm,
            ]
        )
    return [float(value) for value in values if value is not None]


def _wire_matches_feature_profile(
    wire,
    feature: HoleFeature,
    face_axis: tuple[float, float, float],
) -> bool:
    if feature.center is None:
        return False
    if feature.axis is not None and not _axis_aligned(
        tuple(feature.axis), face_axis, tolerance=0.95
    ):
        return False
    center = _wire_center(wire)
    axis = tuple(feature.axis) if feature.axis is not None else face_axis
    delta = tuple(left - right for left, right in zip(center, tuple(feature.center)))
    projected = _dot(delta, axis)
    radial_offset = _vector_norm(
        tuple(
            component - projected * axis_component
            for component, axis_component in zip(delta, axis)
        )
    )
    if radial_offset > 3.0:
        return False
    wire_diameter = _wire_circular_diameter(wire)
    feature_diameters = _feature_profile_diameters(feature)
    if wire_diameter is not None and feature_diameters:
        return any(abs(wire_diameter - value) <= 0.25 for value in feature_diameters)
    return _center_matches_feature(center, feature)


def _is_countersink_transition_face(face, features: list[HoleFeature]) -> bool:
    """Reject the annular shoulder between the cone and through cylinder.

    That face is internal machining geometry. Treating its two concentric rims
    as a normal sheet skin produces the countersink radial width as a false
    hole-to-edge distance.
    """
    outer_wire = _face_outer_wire(face)
    if outer_wire is None:
        return False
    inner_wires = _face_inner_wires(face)
    if not inner_wires:
        return False
    outer_diameter = _wire_circular_diameter(outer_wire)
    if outer_diameter is None:
        return False
    outer_center = _wire_center(outer_wire)
    for feature in features:
        if (
            feature.type != "countersunk"
            or feature.through_diameter_mm is None
            or feature.countersink_major_diameter_mm is None
            or not _center_matches_feature(outer_center, feature)
        ):
            continue
        for inner_wire in inner_wires:
            inner_diameter = _wire_circular_diameter(inner_wire)
            if inner_diameter is None:
                continue
            diameters = sorted((outer_diameter, inner_diameter))
            expected = sorted(
                (
                    float(feature.through_diameter_mm),
                    float(feature.countersink_major_diameter_mm),
                )
            )
            if all(abs(left - right) <= 0.25 for left, right in zip(diameters, expected)):
                return True
    return False


def _countersink_profile_adjustment(wire, feature: HoleFeature) -> float:
    """Project a through rim to the conservative countersink envelope."""
    if feature.type != "countersunk" or feature.countersink_major_diameter_mm is None:
        return 0.0
    wire_diameter = _wire_circular_diameter(wire)
    if wire_diameter is None:
        return 0.0
    return max(
        0.0,
        (float(feature.countersink_major_diameter_mm) - wire_diameter) / 2.0,
    )


def _detect_unknown_holes(
    shape,
    known_features: list[HoleFeature],
    parameters: AnalysisParameters,
    thickness_mm: float | None = None,
) -> list[HoleFeature]:
    unknown: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue

        for wire in _face_inner_wires(face):
            if not wire.isClosed():
                continue

            perimeter = float(wire.Length)
            bbox = wire.BoundBox
            max_dimension = max(
                float(bbox.XLength),
                float(bbox.YLength),
                float(bbox.ZLength),
            )
            if not _is_planar_opening_size_valid(
                dimension_mm=max_dimension,
                perimeter_mm=perimeter,
                parameters=parameters,
            ):
                continue
            if not _matching_opposite_wire(
                shape,
                face,
                wire,
                parameters,
                thickness_mm,
            ):
                continue

            center = _wire_center(wire)
            if any(
                _center_matches_feature(center, feature)
                for feature in known_features
            ):
                continue

            _append_unique_unknown(
                unknown,
                HoleFeature(
                    type="unknown opening",
                    reason=UNKNOWN_HOLE_REASON,
                    max_dimension_mm=round(max_dimension, 2),
                    bounding_box_mm=Dimensions(
                        x=round(float(bbox.XLength), 3),
                        y=round(float(bbox.YLength), 3),
                        z=round(float(bbox.ZLength), 3),
                    ),
                    perimeter_mm=round(perimeter, 2),
                    area_mm2=_planar_wire_area(wire),
                    center=_rounded_vector(center),
                    axis=_rounded_vector(_normalize_vector(surface.Axis)),
                    confidence="low",
                ),
            )

    unknown.sort(key=lambda hole: hole.center or [])
    return unknown


def _annotate_hole_edge_distances(
    shape,
    features: list[HoleFeature],
) -> tuple[float | None, str, int]:
    """Measure opening-to-external-edge distances on the same planar face.

    This is deliberately kept separate from hole classification: an unavailable
    distance must never change a validated hole count or category.
    """
    if not features:
        return None, "low", 0

    measured_feature_ids: set[int] = set()
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue
        wires = list(face.Wires)
        if len(wires) < 2:
            continue
        if _is_countersink_transition_face(face, features):
            continue
        outer_wire = _face_outer_wire(face)
        if outer_wire is None:
            continue
        face_axis = _normalize_vector(surface.Axis)

        for inner_wire in _face_inner_wires(face):
            if not inner_wire.isClosed():
                continue
            center = _wire_center(inner_wire)
            matching_features = [
                feature
                for feature in features
                if _wire_matches_feature_profile(inner_wire, feature, face_axis)
            ]
            if not matching_features:
                continue
            feature = min(
                matching_features,
                key=lambda item: _vector_norm(
                    tuple(left - right for left, right in zip(center, item.center or center))
                ),
            )
            try:
                distance = float(inner_wire.distToShape(outer_wire)[0])
            except (AttributeError, TypeError, ValueError):
                continue
            if not math.isfinite(distance) or distance < 0:
                continue
            distance = max(
                0.0,
                distance - _countersink_profile_adjustment(inner_wire, feature),
            )
            rounded_distance = round(distance, 3)
            if feature.edge_distance_mm is None or rounded_distance < feature.edge_distance_mm:
                feature.edge_distance_mm = rounded_distance
            measured_feature_ids.add(id(feature))

    measured_distances = [
        feature.edge_distance_mm
        for feature in features
        if feature.edge_distance_mm is not None
    ]
    measured_count = len(measured_feature_ids)
    if not measured_distances:
        return None, "low", 0
    coverage = measured_count / len(features)
    confidence = "high" if coverage >= 0.99 else "medium" if coverage >= 0.5 else "low"
    return min(measured_distances), confidence, measured_count


def _annotate_hole_to_hole_distances(
    shape,
    features: list[HoleFeature],
) -> tuple[float | None, str, int]:
    if len(features) < 2:
        return None, "low", 0

    measured_pairs: set[tuple[int, int]] = set()
    mapped_features: set[int] = set()
    all_distances: list[float] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue
        if _is_countersink_transition_face(face, features):
            continue
        face_axis = _normalize_vector(surface.Axis)
        matched: list[tuple[object, HoleFeature]] = []
        for wire in _face_inner_wires(face):
            if not wire.isClosed():
                continue
            center = _wire_center(wire)
            candidates = [
                feature
                for feature in features
                if _wire_matches_feature_profile(wire, feature, face_axis)
            ]
            if not candidates:
                continue
            feature = min(
                candidates,
                key=lambda item: _vector_norm(
                    tuple(left - right for left, right in zip(center, item.center or center))
                ),
            )
            matched.append((wire, feature))
            mapped_features.add(id(feature))

        for left_index, (left_wire, left_feature) in enumerate(matched):
            for right_wire, right_feature in matched[left_index + 1 :]:
                if left_feature is right_feature:
                    continue
                pair_key = tuple(sorted((id(left_feature), id(right_feature))))
                try:
                    distance = float(left_wire.distToShape(right_wire)[0])
                except (AttributeError, TypeError, ValueError):
                    continue
                if not math.isfinite(distance) or distance < 0:
                    continue
                distance = max(
                    0.0,
                    distance
                    - _countersink_profile_adjustment(left_wire, left_feature)
                    - _countersink_profile_adjustment(right_wire, right_feature),
                )
                rounded_distance = round(distance, 3)
                all_distances.append(rounded_distance)
                measured_pairs.add(pair_key)
                for feature in (left_feature, right_feature):
                    if (
                        feature.nearest_hole_distance_mm is None
                        or rounded_distance < feature.nearest_hole_distance_mm
                    ):
                        feature.nearest_hole_distance_mm = rounded_distance

    if not all_distances:
        return None, "low", 0
    coverage = len(mapped_features) / len(features)
    confidence = "high" if coverage >= 0.99 else "medium" if coverage >= 0.5 else "low"
    return min(all_distances), confidence, len(measured_pairs)


def _cylindrical_face_angle_deg(face) -> float | None:
    """Return the cylindrical angular span when FreeCAD exposes a stable range."""
    angle = _cylindrical_surface_span_deg(face)
    if angle is None:
        return None
    if not math.isfinite(angle) or not 1.0 <= angle <= 180.0:
        return None
    return round(angle, 2)


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
            existing.confidence = (
                "high"
                if existing.confidence == "high" or candidate.confidence == "high"
                else "medium"
            )
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
    part_category: str = "sheet_metal",
) -> list[BendFeature]:
    if detected_thickness_mm is None or part_category != "sheet_metal":
        return []
    thickness_reference = detected_thickness_mm
    min_radius = max(1.0, thickness_reference * 0.75)
    max_radius = max(12.0, thickness_reference * 6.0)
    candidates: list[tuple[object, BendFeature]] = []

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
        angular_span = _cylindrical_surface_span_deg(face)
        if angular_span is not None and angular_span >= 350.0:
            continue
        angle_deg = _cylindrical_face_angle_deg(face)

        candidates.append(
            (face, BendFeature(
                type="simple flange",
                radius_mm=round(radius, 2),
                length_mm=round(length, 2),
                angle_deg=angle_deg,
                axis=_rounded_vector(axis),
                center=_rounded_vector(_vector_tuple(surface.Center)),
                confidence="medium",
            )),
        )

    bends: list[BendFeature] = []
    paired_faces: set[int] = set()
    for left_index, (left_face, left) in enumerate(candidates):
        for right_face, right in candidates[left_index + 1 :]:
            if not _bend_pair_matches(left, right, thickness_reference, parameters):
                continue
            paired_faces.update((id(left_face), id(right_face)))

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
                    angle_deg=inner.angle_deg or outer.angle_deg,
                    axis=inner.axis,
                    center=_rounded_vector(center),
                    confidence="high",
                ),
            )

    # Lower-confidence fallback for STEP exports where AutoMiter/trimming has
    # removed or segmented one cylindrical skin.  Radius and angle alone are
    # never sufficient: the helper requires two distinct sheet panels, shared
    # axial tangent edges and rejects complete cylinders/holes explicitly.
    for face, candidate in candidates:
        if id(face) in paired_faces:
            continue
        adjacent_faces = singleton_bend_adjacent_faces(
            face,
            shape,
            thickness_reference,
            parameters,
        )
        if len(adjacent_faces) != 2:
            continue
        candidate.confidence = "medium"
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


def _detect_cutting_lengths(
    shape,
    circular: list[HoleFeature],
    elongated: list[HoleFeature],
    rounded_rectangular: list[HoleFeature],
    polygonal: list[HoleFeature],
    formed: list[HoleFeature],
    unknown: list[HoleFeature],
):
    warnings = [
        "Cut length is preliminary: outer loop is selected from the longest planar external wire and inner loop length is derived from deduplicated detected features."
    ]

    outer_candidates = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue
        outer_wire = _face_outer_wire(face)
        if outer_wire is None:
            continue
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
        slot_perimeter = slot.perimeter_mm or slot.length_mm
        if slot_perimeter is not None:
            inner_cut_length += float(slot_perimeter)
    for opening in rounded_rectangular:
        if opening.perimeter_mm is not None:
            inner_cut_length += float(opening.perimeter_mm)
    for polygon in polygonal:
        # max_dimension_mm historically contained the wire perimeter. New
        # analyses use the semantic perimeter_mm field while the fallback keeps
        # previously stored API payloads readable.
        polygon_perimeter = polygon.perimeter_mm or polygon.max_dimension_mm
        if polygon_perimeter is not None:
            inner_cut_length += float(polygon_perimeter)
    for formed_hole in formed:
        if formed_hole.length_mm is not None:
            inner_cut_length += float(formed_hole.length_mm)
    for unknown_hole in unknown:
        if unknown_hole.perimeter_mm is not None:
            inner_cut_length += float(unknown_hole.perimeter_mm)

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


def _estimate_flat_pattern(
    *,
    shape,
    thickness_mm: float | None,
    thickness_confidence: str,
    bends: list[BendFeature],
    holes: list[HoleFeature],
    cutting_outer_perimeter_mm: float | None,
    density_g_cm3: float | None,
    parameters: AnalysisParameters,
    part_category: str = "sheet_metal",
) -> FlatPattern:
    result = FlatPattern(
        thickness_mm=thickness_mm,
        k_factor=parameters.flat_pattern_k_factor,
    )
    if part_category == "multi_solid":
        result.warnings.append(
            "Sviluppo piano globale non disponibile: lo STEP contiene piu solidi/componenti."
        )
        return result
    if thickness_mm is None or thickness_mm <= 0:
        result.warnings.append(
            "Sviluppo piano non determinabile: spessore lamiera non disponibile."
        )
        return result

    try:
        volume_mm3 = float(shape.Volume)
    except (AttributeError, TypeError, ValueError):
        volume_mm3 = 0.0
    if not math.isfinite(volume_mm3) or volume_mm3 <= 0:
        result.warnings.append(
            "Sviluppo piano non determinabile: volume CAD non disponibile."
        )
        return result

    if bends:
        geometric_result = unfold_sheet(
            shape=shape,
            thickness_mm=thickness_mm,
            thickness_confidence=thickness_confidence,
            holes=holes,
            density_g_cm3=density_g_cm3,
            k_factor=parameters.flat_pattern_k_factor,
            parameters=parameters,
        )
        if geometric_result is not None:
            if holes:
                geometric_result, _, _ = propagate_openings_and_hole_to_bend(
                    result=geometric_result,
                    shape=shape,
                    thickness_mm=thickness_mm,
                    holes=holes,
                    k_factor=parameters.flat_pattern_k_factor,
                    parameters=parameters,
                )
            return geometric_result

    countersink_extra_removed_volume_mm3 = 0.0
    for hole in holes:
        if (
            hole.type != "countersunk"
            or hole.through_diameter_mm is None
            or hole.countersink_major_diameter_mm is None
            or hole.countersink_depth_mm is None
        ):
            continue
        minor_radius = float(hole.through_diameter_mm) / 2.0
        major_radius = float(hole.countersink_major_diameter_mm) / 2.0
        countersink_depth = float(hole.countersink_depth_mm)
        if major_radius <= minor_radius or countersink_depth <= 0:
            continue
        countersink_extra_removed_volume_mm3 += (
            math.pi
            * countersink_depth
            / 3.0
            * (
                major_radius * major_radius
                + major_radius * minor_radius
                - 2.0 * minor_radius * minor_radius
            )
        )

    result.available = True
    result.status = "partial"
    result.method = "unsupported topology; diagnostic material-volume comparison only"
    result.diagnostic_volume_area_mm2 = round(
        (volume_mm3 + countersink_extra_removed_volume_mm3) / thickness_mm,
        4,
    )

    bend_lengths = [bend.length_mm for bend in bends if bend.length_mm is not None]
    if bend_lengths:
        result.total_bend_length_mm = round(sum(bend_lengths), 2)

    bend_allowances = []
    for bend in bends:
        if bend.radius_mm is None or bend.angle_deg is None:
            bend_allowances = []
            break
        bend_allowances.append(
            math.radians(float(bend.angle_deg))
            * (float(bend.radius_mm) + parameters.flat_pattern_k_factor * thickness_mm)
        )
    if bends and bend_allowances:
        result.total_bend_allowance_mm = round(sum(bend_allowances), 2)

    bbox = shape.BoundBox
    bbox_dimensions = [
        float(bbox.XLength),
        float(bbox.YLength),
        float(bbox.ZLength),
    ]
    if not bends:
        opening_area = (
            sum(float(hole.area_mm2 or 0.0) for hole in holes)
            if all(hole.area_mm2 is not None for hole in holes)
            else None
        )
        outer_areas = []
        for face in getattr(shape, "Faces", []):
            if getattr(face.Surface, "TypeId", "") != "Part::GeomPlane":
                continue
            outer_wire = _face_outer_wire(face)
            if outer_wire is None or not outer_wire.isClosed():
                continue
            area = _planar_wire_area(outer_wire)
            if area is not None and area > 0:
                outer_areas.append(area)
        gross_area = max(outer_areas) if outer_areas else None
        thickness_axis = min(
            range(3),
            key=lambda index: abs(bbox_dimensions[index] - thickness_mm),
        )
        planar_dimensions = [
            dimension
            for index, dimension in enumerate(bbox_dimensions)
            if index != thickness_axis
        ]
        planar_fallback = False
        if gross_area is None and all(dimension > 0 for dimension in planar_dimensions):
            fallback_perimeter = 2.0 * sum(planar_dimensions)
            if (
                cutting_outer_perimeter_mm is not None
                and abs(float(cutting_outer_perimeter_mm) - fallback_perimeter)
                <= parameters.flat_pattern_max_perimeter_coherence_error_pct / 100.0 * fallback_perimeter
            ):
                gross_area = planar_dimensions[0] * planar_dimensions[1]
                planar_fallback = True
        if all(dimension > 0 for dimension in planar_dimensions) and gross_area is not None and opening_area is not None:
            length, width = sorted(planar_dimensions, reverse=True)
            net_area = gross_area - opening_area
            result.blank_dimensions_mm = Dimensions(
                x=round(length, 4),
                y=round(width, 4),
            )
            result.net_developed_area_mm2 = round(net_area, 4)
            result.opening_area_mm2 = round(opening_area, 4)
            result.gross_blank_area_mm2 = round(gross_area, 4)
            result.outer_perimeter_mm = (
                round(float(cutting_outer_perimeter_mm), 4)
                if cutting_outer_perimeter_mm is not None
                else None
            )
            inner_perimeter = sum(
                float(hole.perimeter_mm or hole.circumference_mm or (
                    math.pi * hole.diameter_mm if hole.diameter_mm is not None else 0.0
                ))
                for hole in holes
            )
            result.inner_perimeter_mm = round(inner_perimeter, 4)
            result.total_cut_length_mm = (
                round(result.outer_perimeter_mm + inner_perimeter, 4)
                if result.outer_perimeter_mm is not None
                else None
            )
            result.propagated_opening_count = len(holes)
            result.status = "validated_estimate" if planar_fallback else "exact"
            result.usable_for_costing = result.total_cut_length_mm is not None
            result.is_estimate = planar_fallback
            result.method = (
                "validated planar bounding rectangle fallback"
                if planar_fallback
                else "validated planar STEP contours"
            )
            result.confidence = "high" if thickness_confidence == "high" else "medium"
            diagnostic_error = abs(result.diagnostic_volume_area_mm2 - net_area) / max(net_area, 1e-9) * 100.0
            result.diagnostic_volume_area_error_pct = round(diagnostic_error, 6)
            result.validation.graph_connected = True
            result.validation.topology_continuous = True
            result.validation.self_intersections = 0
            result.validation.overlap_area_mm2 = 0.0
            result.validation.area_coherence_error_pct = 0.0
            result.validation.perimeter_coherence_error_pct = 0.0
            result.validation.all_openings_propagated = True
            result.validation.passed = result.usable_for_costing
            if planar_fallback:
                result.warnings.append(
                    "Contorno planare ricostruito da bounding box e perimetro coerente; stato conservativo validated_estimate."
                )
            for hole in holes:
                hole.flat_contour_propagated = True
            if result.usable_for_costing and density_g_cm3 is not None:
                result.blank_weight_kg = round(
                    gross_area * thickness_mm * density_g_cm3 / 1_000_000,
                    3,
                )
            return result

    result.confidence = "low"
    result.usable_for_costing = False
    result.blank_weight_kg = None
    result.warnings.append(
        "Sviluppo piano completo non determinabile con sicurezza per questa geometria; disponibili solo i dati parziali verificabili."
    )
    return result


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

    candidates: list[tuple[float, float]] = []
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

            candidates.append((round(distance, 2), min(left["area"], right["area"])))

    if not candidates:
        return None, "low"

    grouped: dict[float, dict[str, float]] = {}
    for candidate, support_area in candidates:
        group = grouped.setdefault(candidate, {"count": 0.0, "support_area": 0.0})
        group["count"] += 1.0
        group["support_area"] += support_area

    ranked = sorted(
        grouped.items(),
        key=lambda item: (item[1]["support_area"], item[1]["count"]),
        reverse=True,
    )
    dominant_value, dominant_evidence = ranked[0]
    dominant_count = int(dominant_evidence["count"])

    if len(ranked) > 1:
        second_value, second_evidence = ranked[1]
        support_ratio = second_evidence["support_area"] / max(
            dominant_evidence["support_area"],
            1e-9,
        )
        if support_ratio >= 0.95 and abs(dominant_value - second_value) > 0.1:
            references: list[float] = []
            if declared_thickness_mm is not None:
                references.append(float(declared_thickness_mm))
            try:
                thin_solid_estimate = 2.0 * float(shape.Volume) / float(shape.Area)
            except (AttributeError, TypeError, ValueError, ZeroDivisionError):
                thin_solid_estimate = 0.0
            if 0.5 <= thin_solid_estimate <= 6.0:
                references.append(thin_solid_estimate)
            if not references:
                return None, "low"
            reference = references[0]
            dominant_error = abs(dominant_value - reference)
            second_error = abs(second_value - reference)
            if abs(dominant_error - second_error) <= 0.1:
                return None, "low"
            if second_error < dominant_error:
                dominant_value, dominant_evidence = second_value, second_evidence
                dominant_count = int(dominant_evidence["count"])
    confidence = "medium"
    if dominant_count >= 2:
        confidence = "high"
    if declared_thickness_mm is not None and abs(dominant_value - declared_thickness_mm) <= 0.25:
        confidence = "high"

    return dominant_value, confidence


def _classify_part_geometry(
    shape,
    detected_thickness_mm: float | None,
    thickness_confidence: str,
) -> PartClassification:
    """Conservatively distinguish sheet metal from compact massive solids."""
    solid_count = len(getattr(shape, "Solids", []) or [])
    if solid_count > 1:
        return PartClassification(
            category="multi_solid",
            confidence="high",
            reason=(
                f"Lo STEP contiene {solid_count} solidi/componenti distinti; "
                "non e un singolo pezzo lamiera preventivabile come unita."
            ),
        )
    if detected_thickness_mm is not None:
        return PartClassification(
            category="sheet_metal",
            confidence=thickness_confidence,
            reason=(
                "Coppie di superfici planari parallele confermano uno spessore "
                f"lamiera costante di {detected_thickness_mm:.2f} mm."
            ),
        )

    try:
        volume = float(shape.Volume)
        area = float(shape.Area)
        bbox = shape.BoundBox
        dimensions = [float(bbox.XLength), float(bbox.YLength), float(bbox.ZLength)]
        positive_dimensions = [value for value in dimensions if value > 1e-6]
        thin_solid_estimate = 2.0 * volume / area
        minimum_dimension = min(positive_dimensions)
        maximum_dimension = max(positive_dimensions)
        compactness = minimum_dimension / maximum_dimension
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return PartClassification(
            category="unknown",
            confidence="low",
            reason="Dati geometrici insufficienti per distinguere lamiera e pezzo massivo.",
        )

    if thin_solid_estimate > 6.0 and minimum_dimension > 6.0 and compactness >= 0.15:
        confidence = "high" if thin_solid_estimate >= 8.0 and minimum_dimension >= 10.0 else "medium"
        return PartClassification(
            category="non_sheet_metal",
            confidence=confidence,
            reason=(
                "Nessuno spessore lamiera affidabile; volume/superficie e proporzioni "
                "del bounding box indicano un solido massivo."
            ),
        )

    return PartClassification(
        category="unknown",
        confidence="low",
        reason=(
            "Spessore lamiera non confermato e geometria non abbastanza compatta "
            "per una classificazione massiva sicura."
        ),
    )


def _stable_solids(shape) -> list:
    """Return solids in a deterministic geometric order.

    STEP topology order is not a stable public identifier.  Sorting by location,
    extents and volume keeps component IDs repeatable for UI configuration and
    dataset comparisons.
    """
    solids = list(getattr(shape, "Solids", []) or [])

    def key(solid) -> tuple[float, ...]:
        bbox = solid.BoundBox
        return tuple(
            round(float(value), 6)
            for value in (
                bbox.XMin,
                bbox.YMin,
                bbox.ZMin,
                bbox.XLength,
                bbox.YLength,
                bbox.ZLength,
                getattr(solid, "Volume", 0.0),
            )
        )

    return sorted(solids, key=key)


def _assign_component_feature_ids(holes: Holes, component_id: str) -> None:
    groups = (
        ("circular", holes.circular),
        ("elongated", holes.elongated),
        ("rounded_rectangular", holes.rounded_rectangular),
        ("polygonal", holes.polygonal),
        ("formed", holes.formed),
        ("unknown", holes.unknown),
    )
    for group_name, features in groups:
        for index, feature in enumerate(features, start=1):
            feature.component_id = component_id
            feature.feature_id = f"{component_id}_{group_name}_{index:03d}"


def _detect_component_holes(
    solid,
    component_id: str,
    parameters: AnalysisParameters,
    thickness_mm: float | None,
) -> Holes:
    holes = Holes()
    holes.circular, _ = _detect_circular_holes(solid, parameters, thickness_mm)
    holes.countersunk_holes = _annotate_countersunk_holes(
        solid,
        holes.circular,
        parameters,
        thickness_mm,
    )
    holes.elongated = _detect_elongated_holes(solid, parameters, thickness_mm)
    holes.rounded_rectangular = _detect_rounded_rectangular_holes(
        solid,
        parameters,
        thickness_mm,
    )
    holes.polygonal = _detect_polygonal_holes(solid, parameters, thickness_mm)
    holes.formed = _detect_formed_holes(solid, parameters)
    holes.unknown = _detect_unknown_holes(
        solid,
        [
            *holes.circular,
            *holes.elongated,
            *holes.rounded_rectangular,
            *holes.polygonal,
            *holes.formed,
        ],
        parameters,
        thickness_mm,
    )
    holes.circular_holes = len(holes.circular)
    holes.elongated_holes = len(holes.elongated)
    holes.rounded_rectangular_holes = len(holes.rounded_rectangular)
    holes.polygonal_holes = len(holes.polygonal)
    holes.formed_holes = len(holes.formed)
    holes.unknown_holes = len(holes.unknown)
    holes.total_holes = sum(
        (
            holes.circular_holes,
            holes.elongated_holes,
            holes.rounded_rectangular_holes,
            holes.polygonal_holes,
            holes.formed_holes,
            holes.unknown_holes,
        )
    )
    holes.physical_openings_total = holes.total_holes
    diameters = [
        feature.diameter_mm
        for feature in holes.circular
        if feature.diameter_mm is not None
    ]
    if diameters:
        holes.min_circular_diameter_mm = min(diameters)
        holes.max_circular_diameter_mm = max(diameters)
    holes.confidence = "high" if holes.total_holes and not holes.unknown else (
        "medium" if holes.total_holes else "low"
    )
    _assign_component_feature_ids(holes, component_id)
    return holes


def _aggregate_component_holes(components: list[AssemblyComponent]) -> Holes:
    result = Holes()
    for component in components:
        result.circular.extend(component.holes.circular)
        result.elongated.extend(component.holes.elongated)
        result.rounded_rectangular.extend(component.holes.rounded_rectangular)
        result.polygonal.extend(component.holes.polygonal)
        result.formed.extend(component.holes.formed)
        result.unknown.extend(component.holes.unknown)
        result.countersunk_holes += component.holes.countersunk_holes
    result.circular_holes = len(result.circular)
    result.elongated_holes = len(result.elongated)
    result.rounded_rectangular_holes = len(result.rounded_rectangular)
    result.polygonal_holes = len(result.polygonal)
    result.formed_holes = len(result.formed)
    result.unknown_holes = len(result.unknown)
    result.total_holes = sum(
        (
            result.circular_holes,
            result.elongated_holes,
            result.rounded_rectangular_holes,
            result.polygonal_holes,
            result.formed_holes,
            result.unknown_holes,
        )
    )
    result.physical_openings_total = result.total_holes
    diameters = [
        feature.diameter_mm
        for feature in result.circular
        if feature.diameter_mm is not None
    ]
    if diameters:
        result.min_circular_diameter_mm = min(diameters)
        result.max_circular_diameter_mm = max(diameters)
    result.confidence = "high" if result.total_holes and not result.unknown else (
        "medium" if result.total_holes else "low"
    )
    return result


def _hole_axial_interval(feature: HoleFeature, axis: tuple[float, float, float]) -> tuple[float, float] | None:
    if feature.center is None or feature.depth_mm is None:
        return None
    center_projection = _dot(tuple(feature.center), axis)
    half_depth = float(feature.depth_mm) / 2.0
    return center_projection - half_depth, center_projection + half_depth


def _assembly_circular_features_match(
    left: HoleFeature,
    right: HoleFeature,
    parameters: AnalysisParameters,
) -> bool:
    if (
        left.component_id == right.component_id
        or left.diameter_mm is None
        or right.diameter_mm is None
        or left.center is None
        or right.center is None
        or left.axis is None
        or right.axis is None
    ):
        return False
    if abs(float(left.diameter_mm) - float(right.diameter_mm)) > parameters.assembly_passage_diameter_tolerance_mm:
        return False
    left_axis = tuple(float(value) for value in left.axis)
    right_axis = tuple(float(value) for value in right.axis)
    left_norm = _vector_norm(left_axis)
    right_norm = _vector_norm(right_axis)
    if left_norm == 0 or right_norm == 0:
        return False
    left_axis = tuple(value / left_norm for value in left_axis)
    right_axis = tuple(value / right_norm for value in right_axis)
    if not _axis_aligned(
        left_axis,
        right_axis,
        tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
    ):
        return False
    center_delta = tuple(a - b for a, b in zip(left.center, right.center))
    axial_delta = _dot(center_delta, left_axis)
    radial_delta = _vector_norm(
        tuple(
            component - axial_delta * axis_component
            for component, axis_component in zip(center_delta, left_axis)
        )
    )
    if radial_delta > parameters.assembly_passage_center_tolerance_mm:
        return False
    left_interval = _hole_axial_interval(left, left_axis)
    right_interval = _hole_axial_interval(right, left_axis)
    if left_interval is None or right_interval is None:
        return False
    gap = max(
        0.0,
        max(left_interval[0], right_interval[0])
        - min(left_interval[1], right_interval[1]),
    )
    return gap <= parameters.assembly_passage_axial_gap_tolerance_mm


def _build_assembly_passages(
    components: list[AssemblyComponent],
    parameters: AnalysisParameters,
) -> list[AssemblyPassage]:
    features = [feature for component in components for feature in component.holes.circular]
    parents = list(range(len(features)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_index, left in enumerate(features):
        for right_index in range(left_index + 1, len(features)):
            if _assembly_circular_features_match(left, features[right_index], parameters):
                union(left_index, right_index)

    groups: dict[int, list[HoleFeature]] = {}
    for index, feature in enumerate(features):
        groups.setdefault(find(index), []).append(feature)

    passages: list[AssemblyPassage] = []
    for group in groups.values():
        merged = len(group) > 1
        centers = [feature.center for feature in group if feature.center is not None]
        center = (
            _rounded_vector(tuple(sum(values) / len(values) for values in zip(*centers)))
            if centers
            else None
        )
        passages.append(
            AssemblyPassage(
                id=f"passage_{len(passages) + 1:03d}",
                geometry="circular",
                component_ids=sorted({str(feature.component_id) for feature in group if feature.component_id}),
                feature_ids=[str(feature.feature_id) for feature in group if feature.feature_id],
                diameter_mm=round(sum(float(feature.diameter_mm or 0.0) for feature in group) / len(group), 2),
                center=center,
                axis=group[0].axis,
                confidence="high" if merged else group[0].confidence,
                reason=(
                    "Aperture coassiali di componenti a contatto formano un passaggio continuo nell'assemblato."
                    if merged
                    else "Apertura appartenente a un solo componente dell'assemblato."
                ),
            )
        )
    for component in components:
        other_features = [
            *component.holes.elongated,
            *component.holes.rounded_rectangular,
            *component.holes.polygonal,
            *component.holes.formed,
            *component.holes.unknown,
        ]
        for feature in other_features:
            passages.append(
                AssemblyPassage(
                    id=f"passage_{len(passages) + 1:03d}",
                    geometry="unknown",
                    component_ids=[component.id],
                    feature_ids=[feature.feature_id] if feature.feature_id else [],
                    center=feature.center,
                    axis=feature.axis,
                    confidence=feature.confidence,
                    reason="Apertura non circolare appartenente a un solo componente dell'assemblato.",
                )
            )
    return passages


def _edge_is_in_wire(edge, wire) -> bool:
    for wire_edge in getattr(wire, "Edges", []) or []:
        try:
            if edge.isSame(wire_edge):
                return True
        except (AttributeError, TypeError):
            continue
    return False


def _edge_lies_on_face(edge, face, tolerance_mm: float) -> bool:
    """Require the complete edge, rather than one coincident point, on a face."""
    try:
        common = edge.common(face)
        common_length = float(getattr(common, "Length", 0.0))
        edge_length = float(edge.Length)
        if edge_length > 0 and common_length >= edge_length - max(tolerance_mm, edge_length * 0.001):
            return True
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        Part = importlib.import_module("Part")
        points = edge.discretize(Number=24)
        return bool(points) and all(
            float(Part.Vertex(point).distToShape(face)[0]) <= tolerance_mm
            for point in points
        )
    except Exception:
        return False


def _detect_weld_candidates(
    solids: list,
    component_ids: list[str],
    parameters: AnalysisParameters,
) -> list[WeldEvidence]:
    candidates: list[WeldEvidence] = []
    seen: set[tuple[str, str, float, tuple[float, float, float]]] = set()
    for source_index, solid in enumerate(solids):
        for face in solid.Faces:
            surface = face.Surface
            if getattr(surface, "TypeId", "") != "Part::GeomCylinder":
                continue
            span = _cylindrical_surface_span_deg(face)
            if span is None or span < 350.0:
                continue
            radius = float(surface.Radius)
            axis = _normalize_vector(surface.Axis)
            for edge in getattr(face, "Edges", []) or []:
                geometry = _circular_edge_geometry(edge)
                if geometry is None or abs(geometry[0] - radius) > parameters.hole_diameter_tolerance_mm:
                    continue
                own_outer_boundary = any(
                    getattr(planar_face.Surface, "TypeId", "") == "Part::GeomPlane"
                    and (outer_wire := _face_outer_wire(planar_face)) is not None
                    and _edge_is_in_wire(edge, outer_wire)
                    for planar_face in solid.Faces
                )
                if not own_outer_boundary:
                    continue
                edge_center = _vector_tuple(edge.Curve.Center)
                for target_index, target in enumerate(solids):
                    if target_index == source_index:
                        continue
                    for target_face in target.Faces:
                        target_surface = target_face.Surface
                        if getattr(target_surface, "TypeId", "") != "Part::GeomPlane":
                            continue
                        if not _axis_aligned(
                            axis,
                            _normalize_vector(target_surface.Axis),
                            tolerance=_axis_tolerance(parameters.hole_axis_angle_tolerance_deg),
                        ):
                            continue
                        if not _edge_lies_on_face(
                            edge,
                            target_face,
                            parameters.assembly_contact_tolerance_mm,
                        ):
                            continue
                        pair = tuple(sorted((component_ids[source_index], component_ids[target_index])))
                        rounded_center = tuple(round(value, 3) for value in edge_center)
                        key = (pair[0], pair[1], round(radius, 3), rounded_center)
                        if key in seen:
                            continue
                        seen.add(key)
                        candidates.append(
                            WeldEvidence(
                                id=f"weld_{len(candidates) + 1:03d}",
                                state="weld_candidate",
                                review_status="pending",
                                component_ids=list(pair),
                                geometry="circular",
                                nominal_length_mm=round(float(edge.Length), 2),
                                reference_diameter_mm=round(radius * 2.0, 2),
                                center=_rounded_vector(edge_center),
                                axis=_rounded_vector(axis),
                                contact_evidence="full_outer_circular_boundary_on_other_component_face",
                                confidence="medium",
                                reason=(
                                    "Contorno circolare esterno a contatto con una faccia di un altro componente; "
                                    "la saldatura richiede conferma utente."
                                ),
                            )
                        )
    return candidates


def _analyze_assembly(shape, parameters: AnalysisParameters) -> AssemblyAnalysis:
    solids = _stable_solids(shape)
    if len(solids) <= 1:
        return AssemblyAnalysis(component_count=len(solids))
    components: list[AssemblyComponent] = []
    component_ids = [f"component_{index:03d}" for index in range(1, len(solids) + 1)]
    for component_id, solid in zip(component_ids, solids):
        thickness, thickness_confidence = _detect_sheet_thickness(solid)
        bbox = solid.BoundBox
        components.append(
            AssemblyComponent(
                id=component_id,
                name=component_id.replace("_", " ").title(),
                bounding_box_mm=Dimensions(
                    x=_round_or_none(bbox.XLength),
                    y=_round_or_none(bbox.YLength),
                    z=_round_or_none(bbox.ZLength),
                ),
                volume_cm3=_round_or_none(float(solid.Volume) / 1000.0),
                surface_area_cm2=_round_or_none(float(solid.Area) / 100.0),
                classification=_classify_part_geometry(
                    solid,
                    thickness,
                    thickness_confidence,
                ),
                holes=_detect_component_holes(
                    solid,
                    component_id,
                    parameters,
                    thickness,
                ),
            )
        )
    passages = _build_assembly_passages(components, parameters)
    candidates = _detect_weld_candidates(solids, component_ids, parameters)
    return AssemblyAnalysis(
        component_count=len(components),
        components=components,
        component_opening_features_total=sum(component.holes.total_holes for component in components),
        physical_passages_total=len(passages),
        physical_passages=passages,
        weld_candidates=candidates,
        confidence="medium" if candidates else "low",
        warnings=[
            "Le giunzioni CAD sono candidate geometriche: processo e parametri di saldatura richiedono conferma utente."
        ] if candidates else [
            "Nessuna giunzione saldata deducibile con affidabilita dal solo contatto geometrico."
        ],
    )


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
    k_factor: float | None = None,
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
    if k_factor is not None:
        if not 0.0 <= float(k_factor) <= 1.0:
            raise ValueError("K-factor must be between 0 and 1.")
        analysis_parameters = replace(
            analysis_parameters,
            flat_pattern_k_factor=float(k_factor),
        )

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

        response.geometry.solid_count = len(getattr(shape, "Solids", []))
        response.geometry.shell_count = len(getattr(shape, "Shells", []))
        response.geometry.face_count = len(getattr(shape, "Faces", []))
        response.geometry.edge_count = len(getattr(shape, "Edges", []))
        response.geometry.vertex_count = len(getattr(shape, "Vertexes", []))
        response.geometry.bounding_box_center_mm = Dimensions(
            x=_round_or_none((float(bbox.XMin) + float(bbox.XMax)) / 2.0),
            y=_round_or_none((float(bbox.YMin) + float(bbox.YMax)) / 2.0),
            z=_round_or_none((float(bbox.ZMin) + float(bbox.ZMax)) / 2.0),
        )
        center_of_mass = _mass_center_components(shape)
        if center_of_mass is not None:
            response.geometry.center_of_mass_mm = Dimensions(
                x=_round_or_none(center_of_mass[0]),
                y=_round_or_none(center_of_mass[1]),
                z=_round_or_none(center_of_mass[2]),
            )

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
        response.part_classification = _classify_part_geometry(
            shape,
            detected_thickness,
            thickness_confidence,
        )

        response.holes.circular, raw_circular_candidate_count = _detect_circular_holes(
            shape,
            analysis_parameters,
            detected_thickness,
        )
        response.holes.countersunk_holes = _annotate_countersunk_holes(
            shape,
            response.holes.circular,
            analysis_parameters,
            detected_thickness,
        )
        response.holes.elongated = _detect_elongated_holes(
            shape,
            analysis_parameters,
            detected_thickness,
        )
        response.holes.rounded_rectangular = _detect_rounded_rectangular_holes(
            shape,
            analysis_parameters,
            detected_thickness,
        )
        response.holes.polygonal = _detect_polygonal_holes(
            shape,
            analysis_parameters,
            detected_thickness,
        )
        response.holes.formed = _detect_formed_holes(shape, analysis_parameters)
        response.holes.unknown = _detect_unknown_holes(
            shape,
            [
                *response.holes.circular,
                *response.holes.elongated,
                *response.holes.rounded_rectangular,
                *response.holes.polygonal,
                *response.holes.formed,
            ],
            analysis_parameters,
            detected_thickness,
        )
        if response.part_classification.category == "multi_solid":
            response.assembly = _analyze_assembly(shape, analysis_parameters)
            response.holes = _aggregate_component_holes(response.assembly.components)
        response.holes.circular_holes = len(response.holes.circular)
        response.holes.elongated_holes = len(response.holes.elongated)
        response.holes.rounded_rectangular_holes = len(
            response.holes.rounded_rectangular
        )
        response.holes.polygonal_holes = len(response.holes.polygonal)
        response.holes.formed_holes = len(response.holes.formed)
        response.holes.unknown_holes = len(response.holes.unknown)
        response.holes.total_holes = (
            response.holes.circular_holes
            + response.holes.elongated_holes
            + response.holes.rounded_rectangular_holes
            + response.holes.polygonal_holes
            + response.holes.formed_holes
            + response.holes.unknown_holes
        )
        response.holes.physical_openings_total = response.holes.total_holes
        if response.assembly.component_count > 1:
            response.holes.physical_openings_total = (
                response.assembly.physical_passages_total
            )
        circular_diameters = [
            hole.diameter_mm
            for hole in response.holes.circular
            if hole.diameter_mm is not None
        ]
        if circular_diameters:
            response.holes.min_circular_diameter_mm = min(circular_diameters)
            response.holes.max_circular_diameter_mm = max(circular_diameters)

        all_hole_features = [
            *response.holes.circular,
            *response.holes.elongated,
            *response.holes.rounded_rectangular,
            *response.holes.polygonal,
            *response.holes.formed,
            *response.holes.unknown,
        ]
        (
            response.manufacturability.min_hole_to_edge_mm,
            response.manufacturability.hole_to_edge_confidence,
            response.manufacturability.measured_holes,
        ) = _annotate_hole_edge_distances(shape, all_hole_features)
        (
            response.manufacturability.min_hole_to_hole_mm,
            response.manufacturability.hole_to_hole_confidence,
            response.manufacturability.measured_hole_pairs,
        ) = _annotate_hole_to_hole_distances(shape, all_hole_features)
        if all_hole_features and response.manufacturability.measured_holes < len(all_hole_features):
            response.manufacturability.warnings.append(
                "Hole-to-edge distance is available only for openings matched to a planar face."
            )
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
        if response.holes.unknown_holes > 0:
            response.warnings.append(UNKNOWN_HOLE_WARNING)

        response.bends.items = _detect_bends(
            shape,
            detected_thickness,
            analysis_parameters,
            response.part_classification.category,
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

        if all_hole_features and response.bends.count:
            response.manufacturability.warnings.append(
                "Hole-to-bend distance requires a validated flat pattern and is not inferred from the folded STEP model."
            )

        response.complexity_score = _complexity_score(
            shape,
            len(response.holes.circular),
            response.bends.count,
        )
        if response.complexity_score == "high":
            response.warnings.append(
                "Complex sheet-metal part: bend detection may be incomplete"
            )
        if raw_circular_candidate_count >= 20:
            response.warnings.append(
                "High number of circular features detected: hole deduplication applied"
            )

        if response.part_classification.category in {"non_sheet_metal", "multi_solid"}:
            response.cutting.confidence = "low"
            if response.part_classification.category == "multi_solid":
                warning = (
                    "STEP contiene piu solidi/componenti: separare i componenti "
                    "o analizzarli singolarmente."
                )
                response.cutting.warnings.append(
                    "Taglio laser 2D globale non applicabile a uno STEP multi-solid."
                )
                response.warnings.append(warning)
            else:
                response.cutting.warnings.append(
                    "Taglio laser 2D non applicabile: il modello e classificato come pezzo non lamiera."
                )
                response.warnings.append(
                    "Pezzo non lamiera: il normale processo e preventivo di lavorazione lamiera non sono applicabili."
                )
        else:
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
                response.holes.rounded_rectangular,
                response.holes.polygonal,
                response.holes.formed,
                response.holes.unknown,
            )

        response.flat_pattern = _estimate_flat_pattern(
            shape=shape,
            thickness_mm=response.detected_thickness_mm or response.declared_thickness_mm,
            thickness_confidence=response.thickness_confidence,
            bends=response.bends.items,
            holes=all_hole_features,
            cutting_outer_perimeter_mm=response.cutting.outer_cut_length_mm,
            density_g_cm3=response.density_g_cm3,
            parameters=analysis_parameters,
            part_category=response.part_classification.category,
        )

        if response.part_classification.category == "sheet_metal":
            if response.flat_pattern.usable_for_costing:
                response.cutting.outer_cut_length_mm = response.flat_pattern.outer_perimeter_mm
                response.cutting.inner_cut_length_mm = response.flat_pattern.inner_perimeter_mm
                response.cutting.total_cut_length_mm = response.flat_pattern.total_cut_length_mm
                response.cutting.source = "validated_flat_pattern"
                response.cutting.confidence = (
                    "high" if response.flat_pattern.status == "exact" else "medium"
                )
                response.cutting.warnings = [
                    "Lunghezze di taglio derivate esclusivamente dallo sviluppo piano validato."
                ]
            else:
                response.cutting.outer_cut_length_mm = None
                response.cutting.inner_cut_length_mm = None
                response.cutting.total_cut_length_mm = None
                response.cutting.source = "unavailable"
                response.cutting.confidence = "low"
                response.cutting.warnings = [
                    "Costo e lunghezza laser non disponibili: lo sviluppo piano non ha superato la validazione geometrica."
                ]

        flat_bend_distances = [
            feature.flat_bend_distance_mm
            for feature in all_hole_features
            if feature.flat_bend_distance_mm is not None
        ]
        if flat_bend_distances:
            response.manufacturability.min_hole_to_bend_mm = min(flat_bend_distances)
            response.manufacturability.measured_hole_to_bend = len(flat_bend_distances)
            response.manufacturability.hole_to_bend_confidence = (
                "high" if response.flat_pattern.status == "exact" else "medium"
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
