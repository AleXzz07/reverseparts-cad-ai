from __future__ import annotations

import importlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .schemas import CadAnalysisResponse, Dimensions, HoleFeature


VALID_STEP_SUFFIXES = {".stp", ".step"}
FREECAD_PATH_CANDIDATES = (
    "/usr/lib/freecad-python3/lib",
    "/usr/lib/freecad/lib",
    "/usr/lib/freecad-python3",
)


@dataclass(frozen=True)
class FreeCadStatus:
    available: bool
    error: str | None = None


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


def _is_duplicate_hole(candidate: HoleFeature, existing: HoleFeature) -> bool:
    if candidate.diameter_mm is None or existing.diameter_mm is None:
        return False
    if candidate.center is None or existing.center is None:
        return False
    if candidate.axis is None or existing.axis is None:
        return False

    if abs(candidate.diameter_mm - existing.diameter_mm) > 0.2:
        return False

    candidate_axis = tuple(candidate.axis)
    existing_axis = tuple(existing.axis)
    if not _axis_aligned(candidate_axis, existing_axis):
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

    max_radius = max(candidate.radius_mm or 0.0, existing.radius_mm or 0.0)
    return radial_offset <= 0.75 and projected_offset <= max_depth + max_radius


def _append_unique_hole(holes: list[HoleFeature], candidate: HoleFeature) -> None:
    for existing in holes:
        if _is_duplicate_hole(candidate, existing):
            if existing.confidence != "high" and candidate.confidence == "high":
                existing.confidence = "high"
            return
    holes.append(candidate)


def _detect_circular_holes(shape) -> list[HoleFeature]:
    face_candidates: list[HoleFeature] = []
    for face in shape.Faces:
        surface = face.Surface
        if getattr(surface, "TypeId", "") != "Part::GeomCylinder":
            continue

        radius = float(surface.Radius)
        diameter = radius * 2.0
        if not 4.0 <= diameter <= 8.0:
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
        if not 4.0 <= diameter <= 8.0:
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
            if abs(left["diameter"] - right["diameter"]) > 0.2:
                continue
            if not _axis_aligned(left["axis"], right["axis"]):
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
            if radial_offset > 0.75:
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
        _append_unique_hole(holes, candidate)

    holes.sort(key=lambda hole: (hole.diameter_mm or 0.0, hole.center or []))
    return holes


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
                response.volume_cm3 * density_g_cm3 * max(quantity, 1) / 1000.0
            )

        response.holes.circular = _detect_circular_holes(shape)
        if len(response.holes.circular) >= 4:
            response.holes.confidence = "medium"

        if not response.holes.circular:
            response.warnings.append(
                "Circular hole detection found no high-confidence candidates in the configured diameter range."
            )

        response.warnings.extend(
            [
                "Bend detection is not reported because this implementation has no high-confidence sheet-metal classifier yet.",
                "Detected thickness is not reported because wall-thickness inference is not yet reliable.",
            ]
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
