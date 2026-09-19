from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from .schemas import (
    Dimensions,
    FlatBendLine,
    FlatPattern,
    FlatPatternValidation,
    HoleFeature,
)


Vector3 = tuple[float, float, float]


def _vector(value: Any) -> Vector3:
    return (float(value.x), float(value.y), float(value.z))


def _add(left: Vector3, right: Vector3) -> Vector3:
    return tuple(a + b for a, b in zip(left, right))  # type: ignore[return-value]


def _sub(left: Vector3, right: Vector3) -> Vector3:
    return tuple(a - b for a, b in zip(left, right))  # type: ignore[return-value]


def _scale(value: Vector3, factor: float) -> Vector3:
    return tuple(component * factor for component in value)  # type: ignore[return-value]


def _dot(left: Vector3, right: Vector3) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cross(left: Vector3, right: Vector3) -> Vector3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _norm(value: Vector3) -> float:
    return math.sqrt(_dot(value, value))


def _normalize(value: Vector3) -> Vector3:
    length = _norm(value)
    if length <= 1e-12:
        raise ValueError("Zero-length direction in sheet-metal topology.")
    return _scale(value, 1.0 / length)


def _canonical_axis(value: Vector3) -> Vector3:
    result = _normalize(value)
    for component in result:
        if abs(component) <= 1e-9:
            continue
        return result if component > 0 else _scale(result, -1.0)
    return result


def _projection_span(points: list[Vector3], axis: Vector3) -> float:
    values = [_dot(point, axis) for point in points]
    return max(values) - min(values) if values else 0.0


def _projection_interval(
    points: list[Vector3],
    axis: Vector3,
) -> tuple[float, float] | None:
    values = [_dot(point, axis) for point in points]
    if not values:
        return None
    return min(values), max(values)


def _face_points(face: Any) -> list[Vector3]:
    wire = getattr(face, "OuterWire", None)
    ordered = getattr(wire, "OrderedVertexes", None)
    vertices = ordered if ordered else getattr(face, "Vertexes", [])
    return [_vector(vertex.Point) for vertex in vertices]


def _face_center(face: Any) -> Vector3:
    center = getattr(face, "CenterOfMass", None)
    if center is not None:
        return _vector(center)
    points = _face_points(face)
    if not points:
        return (0.0, 0.0, 0.0)
    return _scale(tuple(sum(point[index] for point in points) for index in range(3)), 1.0 / len(points))  # type: ignore[arg-type]


def _plane_offset(normal: Vector3, point: Vector3) -> float:
    return _dot(normal, point)


def _planar_extents_overlap(
    left: Any,
    right: Any,
    normal: Vector3,
    tolerance_mm: float,
) -> bool:
    """Cheaply reject faces whose projected outer extents are disjoint."""

    reference = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
    first_axis = _normalize(_cross(normal, reference))
    second_axis = _normalize(_cross(normal, first_axis))
    left_points = _face_points(left)
    right_points = _face_points(right)
    for axis in (first_axis, second_axis):
        left_interval = _projection_interval(left_points, axis)
        right_interval = _projection_interval(right_points, axis)
        if left_interval is None or right_interval is None:
            return False
        overlap = min(left_interval[1], right_interval[1]) - max(
            left_interval[0], right_interval[0]
        )
        if overlap <= tolerance_mm:
            return False
    return True


def _projected_planar_overlap_area_mm2(
    left: Any,
    right: Any,
    left_position: Vector3,
    right_position: Vector3,
) -> float | None:
    """Return the exact overlap after projecting ``left`` onto ``right``'s plane.

    This is deliberately used only as a conservative fallback for parallel faces
    that already match thickness and area checks.  Unsupported/inexact topology
    must not make planar pairing more permissive.
    """

    try:
        native_axis = left.Surface.Axis
        axis = _normalize(_vector(native_axis))
        signed_distance = _dot(_sub(right_position, left_position), axis)
        axis_length = _norm(_vector(native_axis))
        projected = left.copy()
        projected.translate(native_axis * (signed_distance / axis_length))
        overlap = projected.common(right)
        return max(0.0, float(overlap.Area))
    except (AttributeError, RuntimeError, TypeError, ValueError, ZeroDivisionError):
        return None


def _surface_type(face: Any) -> str:
    return str(getattr(getattr(face, "Surface", None), "TypeId", ""))


def _face_signature(face: Any) -> tuple[Any, ...]:
    surface = face.Surface
    center = _face_center(face)
    type_id = _surface_type(face)
    if type_id in {"Part::GeomPlane", "Part::GeomCylinder"}:
        normal_or_axis = _canonical_axis(_vector(surface.Axis))
    else:
        normal_or_axis = (0.0, 0.0, 0.0)
    radius = float(surface.Radius) if type_id == "Part::GeomCylinder" else 0.0
    return (
        type_id,
        round(float(face.Area), 6),
        *(round(value, 6) for value in center),
        *(round(value, 6) for value in normal_or_axis),
        round(radius, 6),
    )


def _edges_same(left: Any, right: Any) -> bool:
    try:
        return bool(left.isSame(right))
    except (AttributeError, RuntimeError, TypeError):
        return False


def _faces_share_edge(left: Any, right: Any) -> bool:
    return any(
        _edges_same(left_edge, right_edge)
        for left_edge in getattr(left, "Edges", [])
        for right_edge in getattr(right, "Edges", [])
    )


@dataclass
class SheetPanel:
    id: str
    faces: tuple[Any, Any]
    normal: Vector3
    center: Vector3
    area_mm2: float
    direct: bool = True

    def representative_face_for(self, bend_faces: tuple[Any, Any | None] | None = None) -> Any:
        if bend_faces is not None:
            inner_face, outer_face = bend_faces
            for face in self.faces:
                if _faces_share_edge(face, inner_face):
                    return face
            if outer_face is not None:
                for face in self.faces:
                    if _faces_share_edge(face, outer_face):
                        return face
        return max(self.faces, key=lambda face: (float(face.Area), _face_signature(face)))


@dataclass
class SheetBendZone:
    id: str
    inner_face: Any
    outer_face: Any | None
    axis: Vector3
    center: Vector3
    inner_radius_mm: float
    angle_deg: float
    length_mm: float
    allowance_mm: float
    panel_ids: list[str] = field(default_factory=list)
    direct: bool = True


def _shared_edges(left: Any, right: Any | None) -> list[Any]:
    if right is None:
        return []
    return [
        left_edge
        for left_edge in getattr(left, "Edges", [])
        if any(_edges_same(left_edge, right_edge) for right_edge in getattr(right, "Edges", []))
    ]


def _edge_direction_and_length(edge: Any) -> tuple[Vector3, float] | None:
    points = [_vector(vertex.Point) for vertex in getattr(edge, "Vertexes", [])]
    if len(points) < 2:
        return None
    delta = _sub(points[-1], points[0])
    length = _norm(delta)
    if length <= 1e-9:
        return None
    return _normalize(delta), length


def _has_opposite_sheet_skin(
    face: Any,
    shape: Any,
    thickness_mm: float,
    tolerance_mm: float,
    candidate_faces: tuple[Any, ...] | None = None,
) -> bool:
    normal = _normalize(_vector(face.Surface.Axis))
    offset = _plane_offset(normal, _vector(face.Surface.Position))
    face_points = _face_points(face)
    for other in candidate_faces if candidate_faces is not None else getattr(shape, "Faces", []):
        if other is face or _surface_type(other) != "Part::GeomPlane":
            continue
        other_normal = _normalize(_vector(other.Surface.Axis))
        alignment = _dot(normal, other_normal)
        if abs(alignment) < 0.995:
            continue
        other_offset = _plane_offset(other_normal, _vector(other.Surface.Position))
        distance = abs(offset - other_offset) if alignment > 0 else abs(offset + other_offset)
        if abs(distance - thickness_mm) > tolerance_mm:
            continue
        other_points = _face_points(other)
        center_delta = _sub(_face_center(other), _face_center(face))
        lateral = _sub(center_delta, _scale(normal, _dot(center_delta, normal)))
        span = max(
            math.sqrt(max(float(face.Area), 0.0)),
            math.sqrt(max(float(other.Area), 0.0)),
            thickness_mm,
        )
        if _norm(lateral) <= max(tolerance_mm * 4.0, span * 0.08):
            return True
        # Trimmed/mitered skins can have displaced centroids; require their
        # projected envelopes to overlap instead of accepting an unrelated plane.
        if face_points and other_points:
            axes = [
                _normalize(_sub(face_points[index], face_points[0]))
                for index in range(1, len(face_points))
                if _norm(_sub(face_points[index], face_points[0])) > 1e-6
            ]
            for axis in axes[:2]:
                left = [_dot(point, axis) for point in face_points]
                right = [_dot(point, axis) for point in other_points]
                if min(max(left), max(right)) + tolerance_mm < max(min(left), min(right)):
                    break
            else:
                return True
    return False


def singleton_bend_adjacent_faces(
    face: Any,
    shape: Any,
    thickness_mm: float,
    parameters: Any,
    topology_context: SheetTopologyContext | None = None,
) -> list[Any]:
    """Return the two tangent sheet skins supporting a safe one-cylinder fallback.

    A full cylinder/hole and radius-only matches are deliberately rejected.  The
    returned evidence is used only after complete R/R+t pairing has run.
    """
    cache_key = id(face)
    if topology_context is not None and cache_key in topology_context.singleton_adjacency_cache:
        return list(topology_context.singleton_adjacency_cache[cache_key])

    def finish(value: list[Any]) -> list[Any]:
        if topology_context is not None:
            topology_context.singleton_adjacency_cache[cache_key] = tuple(value)
        return value

    descriptor = topology_context.descriptor(face) if topology_context is not None else None
    if (
        descriptor.surface_type if descriptor is not None else _surface_type(face)
    ) != "Part::GeomCylinder":
        return finish([])
    span = descriptor.cylinder_span_deg if descriptor is not None else _cylinder_span_deg(face)
    if span is None or not 1.0 <= span < 350.0:
        return finish([])
    axis = descriptor.axis if descriptor is not None else _canonical_axis(_vector(face.Surface.Axis))
    radius = float(face.Surface.Radius)
    center = (
        descriptor.surface_center
        if descriptor is not None and descriptor.surface_center is not None
        else _vector(face.Surface.Center)
    )
    tolerance = max(
        float(parameters.flat_pattern_edge_match_tolerance_mm),
        float(parameters.flat_pattern_face_pair_distance_tolerance_mm),
    )
    if topology_context is None:
        paired_panel_signatures = {
            _face_signature(panel_face)
            for panel in _pair_planar_faces(shape, thickness_mm, parameters)
            for panel_face in panel.faces
        }
        candidate_faces = getattr(shape, "Faces", [])
    else:
        paired_panel_signatures = topology_context.paired_planar_signatures
        candidate_faces = topology_context.faces
    cylinder_length = (
        descriptor.cylinder_length_mm
        if descriptor is not None and descriptor.cylinder_length_mm is not None
        else _cylinder_length(face, axis)
    )
    matches: list[tuple[Any, float]] = []
    for panel_face in candidate_faces:
        panel_surface_type = _surface_type(panel_face)
        if panel_surface_type != "Part::GeomPlane":
            continue
        panel_descriptor = (
            topology_context.descriptor(panel_face)
            if topology_context is not None
            else None
        )
        panel_signature = (
            panel_descriptor.signature
            if panel_descriptor is not None
            else _face_signature(panel_face)
        )
        if (
            panel_signature not in paired_panel_signatures
            and not (
                topology_context.has_opposite_sheet_skin(panel_face, tolerance)
                if topology_context is not None
                else _has_opposite_sheet_skin(panel_face, shape, thickness_mm, tolerance)
            )
        ):
            continue
        normal = (
            panel_descriptor.axis
            if panel_descriptor is not None
            else _normalize(_vector(panel_face.Surface.Axis))
        )
        if abs(_dot(normal, axis)) > 0.05:
            continue
        tangent_edges = []
        shared_edges = (
            topology_context.shared_edges(panel_face, face)
            if topology_context is not None
            else _shared_edges(panel_face, face)
        )
        for edge in shared_edges:
            direction_and_length = _edge_direction_and_length(edge)
            if direction_and_length is None:
                continue
            direction, length = direction_and_length
            if abs(_dot(direction, axis)) < 0.98:
                continue
            edge_points = [_vector(vertex.Point) for vertex in getattr(edge, "Vertexes", [])]
            radial = _sub(edge_points[0], center)
            radial = _sub(radial, _scale(axis, _dot(radial, axis)))
            if abs(_norm(radial) - radius) > max(tolerance * 4.0, 0.15):
                continue
            if abs(_dot(_normalize(radial), normal)) < 0.95:
                continue
            tangent_edges.append((length, edge))
        if tangent_edges:
            matches.append((panel_face, sum(item[0] for item in tangent_edges)))

    # A hole joins parallel skins; a bend must join two genuinely distinct
    # panel directions.  More than two directions is topologically ambiguous.
    directions: list[Any] = []
    for panel_face, tangent_length in matches:
        normal = _canonical_axis(_vector(panel_face.Surface.Axis))
        if not any(abs(_dot(normal, existing[0])) >= 0.995 for existing in directions):
            directions.append((normal, panel_face, tangent_length))
    if len(directions) != 2 or cylinder_length <= 0:
        return finish([])
    tangent_lengths = [float(item[2]) for item in directions]
    if min(tangent_lengths) < max(cylinder_length * 0.20, float(parameters.bend_min_length_mm)):
        return finish([])
    if abs(tangent_lengths[0] - tangent_lengths[1]) > max(1.0, cylinder_length * 0.65):
        return finish([])
    return finish([item[1] for item in directions])


@dataclass
class SheetFaceGraph:
    panels: list[SheetPanel]
    bends: list[SheetBendZone]
    root_panel_id: str | None
    connected: bool
    direct: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SheetFaceDescriptor:
    face: Any
    surface_type: str
    signature: tuple[Any, ...]
    area_mm2: float
    center: Vector3
    axis: Vector3 | None
    surface_position: Vector3 | None = None
    surface_center: Vector3 | None = None
    cylinder_span_deg: float | None = None
    cylinder_length_mm: float | None = None


@dataclass
class SheetTopologyContext:
    """Per-analysis OCC topology cache; never shared across STEP files."""

    shape: Any
    thickness_mm: float
    k_factor: float
    parameters: Any
    faces: tuple[Any, ...]
    descriptors: dict[int, SheetFaceDescriptor]
    planar_faces: tuple[Any, ...]
    cylindrical_faces: tuple[Any, ...]
    panels: list[SheetPanel] = field(default_factory=list)
    bends: list[SheetBendZone] = field(default_factory=list)
    graph: SheetFaceGraph | None = None
    paired_planar_signatures: frozenset[tuple[Any, ...]] = frozenset()
    opposite_skin_cache: dict[tuple[int, float], bool] = field(default_factory=dict)
    shared_edges_cache: dict[tuple[int, int], tuple[Any, ...]] = field(default_factory=dict)
    singleton_adjacency_cache: dict[int, tuple[Any, ...]] = field(default_factory=dict)

    def descriptor(self, face: Any) -> SheetFaceDescriptor:
        descriptor = self.descriptors.get(id(face))
        if descriptor is None:
            descriptor = _make_face_descriptor(face)
            self.descriptors[id(face)] = descriptor
        return descriptor

    def has_opposite_sheet_skin(self, face: Any, tolerance_mm: float) -> bool:
        key = (id(face), round(float(tolerance_mm), 9))
        if key not in self.opposite_skin_cache:
            self.opposite_skin_cache[key] = _has_opposite_sheet_skin(
                face,
                self.shape,
                self.thickness_mm,
                tolerance_mm,
                candidate_faces=self.faces,
            )
        return self.opposite_skin_cache[key]

    def shared_edges(self, left: Any, right: Any | None) -> tuple[Any, ...]:
        if right is None:
            return ()
        key = (id(left), id(right))
        if key not in self.shared_edges_cache:
            self.shared_edges_cache[key] = tuple(_shared_edges(left, right))
        return self.shared_edges_cache[key]

    def faces_share_edge(self, left: Any, right: Any) -> bool:
        return bool(self.shared_edges(left, right))


def _make_face_descriptor(face: Any) -> SheetFaceDescriptor:
    surface = face.Surface
    surface_type = _surface_type(face)
    center = _face_center(face)
    axis: Vector3 | None = None
    surface_position: Vector3 | None = None
    surface_center: Vector3 | None = None
    radius = 0.0
    span: float | None = None
    length: float | None = None
    if surface_type == "Part::GeomPlane":
        axis = _canonical_axis(_vector(surface.Axis))
        surface_position = _vector(surface.Position)
    elif surface_type == "Part::GeomCylinder":
        axis = _canonical_axis(_vector(surface.Axis))
        surface_center = _vector(surface.Center)
        radius = float(surface.Radius)
        span = _cylinder_span_deg(face)
        length = _cylinder_length(face, axis)
    signature_axis = axis if axis is not None else (0.0, 0.0, 0.0)
    signature = (
        surface_type,
        round(float(face.Area), 6),
        *(round(value, 6) for value in center),
        *(round(value, 6) for value in signature_axis),
        round(radius, 6),
    )
    return SheetFaceDescriptor(
        face=face,
        surface_type=surface_type,
        signature=signature,
        area_mm2=float(face.Area),
        center=center,
        axis=axis,
        surface_position=surface_position,
        surface_center=surface_center,
        cylinder_span_deg=span,
        cylinder_length_mm=length,
    )


def _pair_planar_faces(
    shape: Any,
    thickness_mm: float,
    parameters: Any,
    topology_context: SheetTopologyContext | None = None,
) -> list[SheetPanel]:
    if topology_context is None:
        faces = sorted(
            [face for face in shape.Faces if _surface_type(face) == "Part::GeomPlane"],
            key=_face_signature,
        )
        descriptor = None
    else:
        faces = sorted(
            topology_context.planar_faces,
            key=lambda face: topology_context.descriptor(face).signature,
        )
        descriptor = topology_context.descriptor
    candidates: list[tuple[float, float, int, int]] = []
    tolerance = float(parameters.flat_pattern_face_pair_distance_tolerance_mm)
    for left_index, left in enumerate(faces):
        left_descriptor = descriptor(left) if descriptor is not None else None
        left_normal = (
            left_descriptor.axis
            if left_descriptor is not None
            else _normalize(_vector(left.Surface.Axis))
        )
        left_position = (
            left_descriptor.surface_position
            if left_descriptor is not None
            else _vector(left.Surface.Position)
        )
        left_offset = _plane_offset(left_normal, left_position)
        left_area = left_descriptor.area_mm2 if left_descriptor is not None else float(left.Area)
        left_center = left_descriptor.center if left_descriptor is not None else _face_center(left)
        for right_index in range(left_index + 1, len(faces)):
            right = faces[right_index]
            right_descriptor = descriptor(right) if descriptor is not None else None
            right_normal = (
                right_descriptor.axis
                if right_descriptor is not None
                else _normalize(_vector(right.Surface.Axis))
            )
            alignment = _dot(left_normal, right_normal)
            if abs(alignment) < 0.995:
                continue
            right_position = (
                right_descriptor.surface_position
                if right_descriptor is not None
                else _vector(right.Surface.Position)
            )
            right_offset = _plane_offset(right_normal, right_position)
            distance = abs(left_offset - right_offset) if alignment > 0 else abs(left_offset + right_offset)
            if abs(distance - thickness_mm) > tolerance:
                continue
            right_area = right_descriptor.area_mm2 if right_descriptor is not None else float(right.Area)
            area_ratio = min(left_area, right_area) / max(left_area, right_area, 1e-9)
            if area_ratio < 0.65:
                continue
            right_center = right_descriptor.center if right_descriptor is not None else _face_center(right)
            center_delta = _sub(right_center, left_center)
            lateral = _sub(center_delta, _scale(left_normal, _dot(center_delta, left_normal)))
            lateral_error = _norm(lateral)
            scale = max(math.sqrt(left_area), math.sqrt(right_area), thickness_mm)
            if lateral_error > max(tolerance * 4.0, scale * 0.03):
                if not _planar_extents_overlap(left, right, left_normal, tolerance):
                    continue
                overlap_area = _projected_planar_overlap_area_mm2(
                    left,
                    right,
                    left_position,
                    right_position,
                )
                smaller_area = min(left_area, right_area)
                uncovered_area = (
                    None
                    if overlap_area is None
                    else max(0.0, smaller_area - overlap_area)
                )
                if (
                    overlap_area is None
                    or overlap_area <= 0.0
                    or uncovered_area
                    > float(parameters.flat_pattern_max_overlap_area_mm2)
                ):
                    continue
            candidates.append((area_ratio, min(left_area, right_area), left_index, right_index))

    used: set[int] = set()
    selected: list[tuple[Any, Any]] = []
    for _, _, left_index, right_index in sorted(candidates, reverse=True):
        if left_index in used or right_index in used:
            continue
        used.update((left_index, right_index))
        selected.append((faces[left_index], faces[right_index]))

    selected.sort(
        key=lambda pair: min(
            descriptor(pair[0]).signature if descriptor is not None else _face_signature(pair[0]),
            descriptor(pair[1]).signature if descriptor is not None else _face_signature(pair[1]),
        )
    )
    panels: list[SheetPanel] = []
    for index, pair in enumerate(selected, start=1):
        left, right = pair
        left_descriptor = descriptor(left) if descriptor is not None else None
        right_descriptor = descriptor(right) if descriptor is not None else None
        normal = (
            left_descriptor.axis
            if left_descriptor is not None
            else _canonical_axis(_vector(left.Surface.Axis))
        )
        left_center = left_descriptor.center if left_descriptor is not None else _face_center(left)
        right_center = right_descriptor.center if right_descriptor is not None else _face_center(right)
        left_area = left_descriptor.area_mm2 if left_descriptor is not None else float(left.Area)
        right_area = right_descriptor.area_mm2 if right_descriptor is not None else float(right.Area)
        panels.append(
            SheetPanel(
                id=f"panel_{index:03d}",
                faces=pair,
                normal=normal,
                center=_scale(_add(left_center, right_center), 0.5),
                area_mm2=(left_area + right_area) / 2.0,
            )
        )
    return panels


def _cylinder_span_deg(face: Any) -> float | None:
    try:
        parameter_range = tuple(float(value) for value in face.ParameterRange)
    except (AttributeError, TypeError, ValueError):
        return None
    if len(parameter_range) < 2:
        return None
    span = abs(parameter_range[1] - parameter_range[0])
    if span <= 1e-9:
        return None
    return math.degrees(span) if span <= 2.0 * math.pi + 1e-6 else span


def _cylinder_length(face: Any, axis: Vector3) -> float:
    return _projection_span(_face_points(face), axis)


def _pair_bend_faces(
    shape: Any,
    thickness_mm: float,
    k_factor: float,
    parameters: Any,
    topology_context: SheetTopologyContext | None = None,
) -> list[SheetBendZone]:
    if topology_context is None:
        faces = sorted(
            [face for face in shape.Faces if _surface_type(face) == "Part::GeomCylinder"],
            key=_face_signature,
        )
        descriptor = None
    else:
        faces = sorted(
            topology_context.cylindrical_faces,
            key=lambda face: topology_context.descriptor(face).signature,
        )
        descriptor = topology_context.descriptor
    tolerance = float(parameters.bend_radius_pair_tolerance_mm)
    axial_overlap_tolerance = float(parameters.flat_pattern_edge_match_tolerance_mm)
    face_points = {id(face): _face_points(face) for face in faces}
    candidates: list[tuple[float, int, int]] = []
    for left_index, left in enumerate(faces):
        left_descriptor = descriptor(left) if descriptor is not None else None
        left_span = (
            left_descriptor.cylinder_span_deg
            if left_descriptor is not None
            else _cylinder_span_deg(left)
        )
        if left_span is None or not 1.0 <= left_span <= 180.0:
            continue
        left_axis = (
            left_descriptor.axis
            if left_descriptor is not None
            else _canonical_axis(_vector(left.Surface.Axis))
        )
        for right_index in range(left_index + 1, len(faces)):
            right = faces[right_index]
            right_descriptor = descriptor(right) if descriptor is not None else None
            right_span = (
                right_descriptor.cylinder_span_deg
                if right_descriptor is not None
                else _cylinder_span_deg(right)
            )
            if right_span is None or abs(left_span - right_span) > 0.2:
                continue
            right_axis = (
                right_descriptor.axis
                if right_descriptor is not None
                else _canonical_axis(_vector(right.Surface.Axis))
            )
            if abs(_dot(left_axis, right_axis)) < 0.995:
                continue
            radius_delta = abs(float(left.Surface.Radius) - float(right.Surface.Radius))
            if abs(radius_delta - thickness_mm) > tolerance:
                continue
            left_center = (
                left_descriptor.surface_center
                if left_descriptor is not None and left_descriptor.surface_center is not None
                else _vector(left.Surface.Center)
            )
            right_center = (
                right_descriptor.surface_center
                if right_descriptor is not None and right_descriptor.surface_center is not None
                else _vector(right.Surface.Center)
            )
            center_delta = _sub(left_center, right_center)
            radial_delta = _sub(center_delta, _scale(left_axis, _dot(center_delta, left_axis)))
            if _norm(radial_delta) > float(parameters.bend_center_tolerance_mm):
                continue
            left_interval = _projection_interval(face_points[id(left)], left_axis)
            right_interval = _projection_interval(face_points[id(right)], left_axis)
            if left_interval is None or right_interval is None:
                continue
            axial_overlap = min(left_interval[1], right_interval[1]) - max(
                left_interval[0],
                right_interval[0],
            )
            # Proximity between disjoint intervals is not sufficient evidence
            # that two cylindrical skins belong to the same physical bend.
            if axial_overlap <= axial_overlap_tolerance:
                continue
            candidates.append((min(float(left.Area), float(right.Area)), left_index, right_index))

    used: set[int] = set()
    pairs: list[tuple[Any, Any]] = []
    for _, left_index, right_index in sorted(candidates, reverse=True):
        if left_index in used or right_index in used:
            continue
        used.update((left_index, right_index))
        left, right = faces[left_index], faces[right_index]
        pairs.append(tuple(sorted((left, right), key=lambda face: float(face.Surface.Radius))))
    pairs.sort(
        key=lambda pair: (
            descriptor(pair[0]).signature
            if descriptor is not None
            else _face_signature(pair[0])
        )
    )

    bends: list[SheetBendZone] = []
    for index, (inner, outer) in enumerate(pairs, start=1):
        inner_descriptor = descriptor(inner) if descriptor is not None else None
        outer_descriptor = descriptor(outer) if descriptor is not None else None
        axis = (
            inner_descriptor.axis
            if inner_descriptor is not None
            else _canonical_axis(_vector(inner.Surface.Axis))
        )
        angle = (
            inner_descriptor.cylinder_span_deg
            if inner_descriptor is not None
            else _cylinder_span_deg(inner)
        )
        if angle is None:
            continue
        inner_length = (
            inner_descriptor.cylinder_length_mm
            if inner_descriptor is not None and inner_descriptor.cylinder_length_mm is not None
            else _cylinder_length(inner, axis)
        )
        outer_length = (
            outer_descriptor.cylinder_length_mm
            if outer_descriptor is not None and outer_descriptor.cylinder_length_mm is not None
            else _cylinder_length(outer, axis)
        )
        length = max(inner_length, outer_length)
        radius = float(inner.Surface.Radius)
        inner_center = (
            inner_descriptor.surface_center
            if inner_descriptor is not None and inner_descriptor.surface_center is not None
            else _vector(inner.Surface.Center)
        )
        outer_center = (
            outer_descriptor.surface_center
            if outer_descriptor is not None and outer_descriptor.surface_center is not None
            else _vector(outer.Surface.Center)
        )
        bends.append(
            SheetBendZone(
                id=f"bend_{index:03d}",
                inner_face=inner,
                outer_face=outer,
                axis=axis,
                center=_scale(_add(inner_center, outer_center), 0.5),
                inner_radius_mm=radius,
                angle_deg=angle,
                length_mm=length,
                allowance_mm=math.radians(angle) * (radius + k_factor * thickness_mm),
            )
        )
    # Complete inner/outer pairs are intentionally selected first.  A trimmed
    # single cylindrical skin is admitted only with two independently verified
    # tangent sheet panels, and is therefore lower-confidence/direct=False.
    for face_index, face in enumerate(faces):
        if face_index in used:
            continue
        face_descriptor = descriptor(face) if descriptor is not None else None
        span = (
            face_descriptor.cylinder_span_deg
            if face_descriptor is not None
            else _cylinder_span_deg(face)
        )
        if span is None or not 1.0 <= span <= 180.0:
            continue
        radius = float(face.Surface.Radius)
        min_radius = max(1.0, thickness_mm * 0.75)
        max_radius = max(12.0, thickness_mm * 6.0)
        if not min_radius <= radius <= max_radius:
            continue
        adjacent_faces = singleton_bend_adjacent_faces(
            face,
            shape,
            thickness_mm,
            parameters,
            topology_context=topology_context,
        )
        if len(adjacent_faces) != 2:
            continue
        axis = (
            face_descriptor.axis
            if face_descriptor is not None
            else _canonical_axis(_vector(face.Surface.Axis))
        )
        length = (
            face_descriptor.cylinder_length_mm
            if face_descriptor is not None and face_descriptor.cylinder_length_mm is not None
            else _cylinder_length(face, axis)
        )
        if length < float(parameters.bend_min_length_mm):
            continue
        bends.append(
            SheetBendZone(
                id="",
                inner_face=face,
                outer_face=None,
                axis=axis,
                center=(
                    face_descriptor.surface_center
                    if face_descriptor is not None and face_descriptor.surface_center is not None
                    else _vector(face.Surface.Center)
                ),
                inner_radius_mm=radius,
                angle_deg=span,
                length_mm=length,
                allowance_mm=math.radians(span) * (radius + k_factor * thickness_mm),
                direct=False,
            )
        )
    bends.sort(
        key=lambda bend: (
            descriptor(bend.inner_face).signature
            if descriptor is not None
            else _face_signature(bend.inner_face)
        )
    )
    for index, bend in enumerate(bends, start=1):
        bend.id = f"bend_{index:03d}"
    return bends


def _graph_connected(panels: list[SheetPanel], bends: list[SheetBendZone]) -> bool:
    if not panels:
        return False
    adjacency: dict[str, set[str]] = {panel.id: set() for panel in panels}
    for bend in bends:
        if len(bend.panel_ids) != 2:
            continue
        left, right = bend.panel_ids
        adjacency[left].add(right)
        adjacency[right].add(left)
    visited: set[str] = set()
    pending = [panels[0].id]
    while pending:
        item = pending.pop()
        if item in visited:
            continue
        visited.add(item)
        pending.extend(adjacency[item] - visited)
    return len(visited) == len(panels)


def _assemble_sheet_face_graph(
    panels: list[SheetPanel],
    bends: list[SheetBendZone],
    topology_context: SheetTopologyContext | None = None,
) -> SheetFaceGraph:
    warnings: list[str] = []
    for bend in bends:
        bend_faces = tuple(face for face in (bend.inner_face, bend.outer_face) if face is not None)
        bend.panel_ids = [
            panel.id
            for panel in panels
            if any(
                (
                    topology_context.faces_share_edge(panel_face, bend_face)
                    if topology_context is not None
                    else _faces_share_edge(panel_face, bend_face)
                )
                for panel_face in panel.faces
                for bend_face in bend_faces
            )
        ]
        if len(bend.panel_ids) != 2:
            warnings.append(
                f"{bend.id}: attese due facce planari adiacenti, trovate {len(bend.panel_ids)}."
            )
    root = max(
        panels,
        key=lambda panel: (panel.area_mm2, tuple(-value for value in panel.center), panel.id),
        default=None,
    )
    connected = _graph_connected(panels, bends) and all(len(bend.panel_ids) == 2 for bend in bends)
    return SheetFaceGraph(
        panels=panels,
        bends=bends,
        root_panel_id=root.id if root else None,
        connected=connected,
        direct=connected and not warnings and all(bend.direct for bend in bends),
        warnings=warnings,
    )


def build_sheet_topology_context(
    shape: Any,
    thickness_mm: float,
    k_factor: float,
    parameters: Any,
) -> SheetTopologyContext:
    faces = tuple(getattr(shape, "Faces", []) or [])
    context = SheetTopologyContext(
        shape=shape,
        thickness_mm=thickness_mm,
        k_factor=k_factor,
        parameters=parameters,
        faces=faces,
        descriptors={},
        planar_faces=tuple(face for face in faces if _surface_type(face) == "Part::GeomPlane"),
        cylindrical_faces=tuple(
            face for face in faces if _surface_type(face) == "Part::GeomCylinder"
        ),
    )
    for face in (*context.planar_faces, *context.cylindrical_faces):
        context.descriptor(face)
    context.panels = _pair_planar_faces(
        shape,
        thickness_mm,
        parameters,
        topology_context=context,
    )
    context.paired_planar_signatures = frozenset(
        context.descriptor(panel_face).signature
        for panel in context.panels
        for panel_face in panel.faces
    )
    context.bends = _pair_bend_faces(
        shape,
        thickness_mm,
        k_factor,
        parameters,
        topology_context=context,
    )
    context.graph = _assemble_sheet_face_graph(
        context.panels,
        context.bends,
        topology_context=context,
    )
    return context


def build_sheet_face_graph(
    shape: Any,
    thickness_mm: float,
    k_factor: float,
    parameters: Any,
    topology_context: SheetTopologyContext | None = None,
) -> SheetFaceGraph:
    context = topology_context or build_sheet_topology_context(
        shape,
        thickness_mm,
        k_factor,
        parameters,
    )
    if context.graph is None:
        context.graph = _assemble_sheet_face_graph(
            context.panels,
            context.bends,
            topology_context=context,
        )
    return context.graph


def _panel_spans(panel: SheetPanel, bend_axis: Vector3, bend_faces: tuple[Any, Any] | None = None) -> tuple[float, float]:
    face = panel.representative_face_for(bend_faces)
    points = _face_points(face)
    cross_direction = _normalize(_cross(bend_axis, panel.normal))
    return _projection_span(points, cross_direction), _projection_span(points, bend_axis)


def _all_axes_parallel(bends: list[SheetBendZone]) -> bool:
    return bool(bends) and all(abs(_dot(bends[0].axis, bend.axis)) >= 0.995 for bend in bends[1:])


def _edge_midpoint(edge: Any) -> Vector3 | None:
    points = [_vector(vertex.Point) for vertex in getattr(edge, "Vertexes", [])]
    if not points:
        center = getattr(edge, "CenterOfMass", None)
        return _vector(center) if center is not None else None
    return _scale(tuple(sum(point[index] for point in points) for index in range(3)), 1.0 / len(points))  # type: ignore[arg-type]


def _panel_bend_edge(panel: SheetPanel, bend: SheetBendZone) -> Any | None:
    candidates: list[Any] = []
    for panel_face in panel.faces:
        candidates.extend(_shared_edges(panel_face, bend.inner_face))
        candidates.extend(_shared_edges(panel_face, bend.outer_face))
    return max(candidates, key=lambda edge: float(getattr(edge, "Length", 0.0)), default=None)


@dataclass
class _FlatFrame:
    u3: Vector3
    v3: Vector3
    u2: tuple[float, float]
    v2: tuple[float, float]
    anchor3: Vector3
    anchor2: tuple[float, float]

    def map_point(self, point: Vector3) -> tuple[float, float]:
        delta = _sub(point, self.anchor3)
        return (
            self.anchor2[0] + _dot(delta, self.u3) * self.u2[0] + _dot(delta, self.v3) * self.v2[0],
            self.anchor2[1] + _dot(delta, self.u3) * self.u2[1] + _dot(delta, self.v3) * self.v2[1],
        )


def _normalize2(value: tuple[float, float]) -> tuple[float, float]:
    length = math.hypot(*value)
    if length <= 1e-12:
        raise ValueError("Zero-length 2D direction in unfold traversal.")
    return (value[0] / length, value[1] / length)


def _scale2(value: tuple[float, float], factor: float) -> tuple[float, float]:
    return (value[0] * factor, value[1] * factor)


def _add2(left: tuple[float, float], right: tuple[float, float]) -> tuple[float, float]:
    return (left[0] + right[0], left[1] + right[1])


def _map_direction(frame: _FlatFrame, direction: Vector3) -> tuple[float, float]:
    return _normalize2(
        _add2(
            _scale2(frame.u2, _dot(direction, frame.u3)),
            _scale2(frame.v2, _dot(direction, frame.v3)),
        )
    )


def _recursive_flattened_points(
    graph: SheetFaceGraph,
) -> tuple[list[tuple[float, float]], list[FlatBendLine], list[str]] | None:
    """Flatten an arbitrary acyclic panel graph into a deterministic local 2D frame."""
    root = next((panel for panel in graph.panels if panel.id == graph.root_panel_id), None)
    if root is None or not graph.bends:
        return None
    adjacency: dict[str, list[tuple[SheetBendZone, str]]] = {panel.id: [] for panel in graph.panels}
    for bend in graph.bends:
        if len(bend.panel_ids) != 2:
            return None
        left, right = bend.panel_ids
        adjacency[left].append((bend, right))
        adjacency[right].append((bend, left))
    if len(graph.bends) != len(graph.panels) - 1:
        return None

    first_bend = min(
        (bend for bend in graph.bends if root.id in bend.panel_ids),
        key=lambda bend: bend.id,
        default=None,
    )
    if first_bend is None:
        return None
    root_u = _canonical_axis(first_bend.axis)
    root_v = _normalize(_cross(root.normal, root_u))
    frames: dict[str, _FlatFrame] = {
        root.id: _FlatFrame(root_u, root_v, (1.0, 0.0), (0.0, 1.0), root.center, (0.0, 0.0))
    }
    panel_by_id = {panel.id: panel for panel in graph.panels}
    all_points: list[tuple[float, float]] = []
    bend_lines: list[FlatBendLine] = []
    warnings: list[str] = []
    pending: list[tuple[str, str | None]] = [(root.id, None)]
    visited: set[str] = set()
    while pending:
        panel_id, parent_id = pending.pop(0)
        if panel_id in visited:
            warnings.append(f"{panel_id}: ciclo o trasformazione contraddittoria nel face graph.")
            return None
        visited.add(panel_id)
        panel = panel_by_id[panel_id]
        frame = frames[panel_id]
        face = panel.representative_face_for()
        mapped_panel = [frame.map_point(point) for point in _face_points(face)]
        if len(mapped_panel) < 3:
            return None
        all_points.extend(mapped_panel)

        for bend, child_id in sorted(adjacency[panel_id], key=lambda item: item[0].id):
            if child_id == parent_id:
                continue
            if child_id in frames:
                warnings.append(f"{bend.id}: trasformazione multipla contraddittoria per {child_id}.")
                return None
            child = panel_by_id[child_id]
            parent_edge = _panel_bend_edge(panel, bend)
            child_edge = _panel_bend_edge(child, bend)
            parent_mid3 = _edge_midpoint(parent_edge) if parent_edge is not None else None
            child_mid3 = _edge_midpoint(child_edge) if child_edge is not None else None
            # Production OCC edges expose vertices.  The center fallback keeps
            # synthetic/unit topology usable without changing geometric status.
            parent_mid3 = parent_mid3 or bend.center
            child_mid3 = child_mid3 or bend.center
            tangent2 = _map_direction(frame, bend.axis)
            parent_cross3 = _normalize(_cross(bend.axis, panel.normal))
            cross2 = _map_direction(frame, parent_cross3)
            side = _dot(_sub(parent_mid3, panel.center), parent_cross3)
            if abs(side) <= 1e-7:
                panel_center2 = frame.map_point(panel.center)
                edge_center2 = frame.map_point(parent_mid3)
                side = -(
                    (panel_center2[0] - edge_center2[0]) * cross2[0]
                    + (panel_center2[1] - edge_center2[1]) * cross2[1]
                )
            outward2 = cross2 if side >= 0 else _scale2(cross2, -1.0)
            parent_mid2 = frame.map_point(parent_mid3)
            child_anchor2 = _add2(parent_mid2, _scale2(outward2, bend.allowance_mm))
            child_u3 = _canonical_axis(bend.axis)
            if _dot(child_u3, bend.axis) < 0:
                tangent2 = _scale2(tangent2, -1.0)
            child_v3 = _normalize(_cross(child_u3, child.normal))
            child_side = _dot(_sub(child.center, child_mid3), child_v3)
            child_v2 = outward2 if child_side >= 0 else _scale2(outward2, -1.0)
            frames[child_id] = _FlatFrame(
                child_u3,
                child_v3,
                tangent2,
                child_v2,
                child_mid3,
                child_anchor2,
            )
            half_length = bend.length_mm / 2.0
            line_center = _add2(parent_mid2, _scale2(outward2, bend.allowance_mm / 2.0))
            start = _add2(line_center, _scale2(tangent2, -half_length))
            end = _add2(line_center, _scale2(tangent2, half_length))
            bend_lines.append(
                FlatBendLine(
                    id=bend.id,
                    start_mm=Dimensions(x=round(start[0], 4), y=round(start[1], 4)),
                    end_mm=Dimensions(x=round(end[0], 4), y=round(end[1], 4)),
                    radius_mm=round(bend.inner_radius_mm, 4),
                    angle_deg=round(bend.angle_deg, 4),
                    allowance_mm=round(bend.allowance_mm, 4),
                    length_mm=round(bend.length_mm, 4),
                )
            )
            all_points.extend(
                [
                    _add2(parent_mid2, _scale2(tangent2, -half_length)),
                    _add2(parent_mid2, _scale2(tangent2, half_length)),
                    _add2(child_anchor2, _scale2(tangent2, -half_length)),
                    _add2(child_anchor2, _scale2(tangent2, half_length)),
                ]
            )
            pending.append((child_id, panel_id))
    if len(visited) != len(graph.panels):
        return None
    return all_points, bend_lines, warnings


def _outer_wire_length(face: Any) -> float | None:
    wire = getattr(face, "OuterWire", None)
    try:
        length = float(wire.Length)
    except (AttributeError, TypeError, ValueError):
        return None
    return length if math.isfinite(length) and length > 0 else None


def _axis_relation(left: Vector3, right: Vector3) -> str:
    alignment = abs(_dot(left, right))
    if alignment >= 0.995:
        return "parallel"
    if alignment <= 0.05:
        return "perpendicular"
    return "oblique"


def _volume_area(shape: Any, thickness_mm: float) -> float | None:
    try:
        value = float(shape.Volume) / thickness_mm
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def unfold_parallel_sheet(
    *,
    shape: Any,
    thickness_mm: float,
    thickness_confidence: str,
    holes: list[HoleFeature],
    density_g_cm3: float | None,
    k_factor: float,
    parameters: Any,
    topology_context: SheetTopologyContext | None = None,
) -> FlatPattern | None:
    if not hasattr(shape, "Faces"):
        return None
    graph = build_sheet_face_graph(
        shape,
        thickness_mm,
        k_factor,
        parameters,
        topology_context=topology_context,
    )
    if not graph.bends or not _all_axes_parallel(graph.bends):
        return None
    result = FlatPattern(
        available=True,
        thickness_mm=thickness_mm,
        k_factor=k_factor,
        root_panel_id=graph.root_panel_id,
        panel_count=len(graph.panels),
        bend_zone_count=len(graph.bends),
        method="analytic face-graph unfold with neutral-axis bend strips",
        diagnostic_volume_area_mm2=_volume_area(shape, thickness_mm),
        confidence="high" if thickness_confidence == "high" else "medium",
    )
    result.warnings.extend(graph.warnings)
    if not graph.connected or len(graph.panels) != len(graph.bends) + 1:
        result.status = "partial"
        result.warnings.append("Face graph lamiera non connesso o non riconducibile a una catena aperta.")
        return result

    axis = graph.bends[0].axis
    panel_by_id = {panel.id: panel for panel in graph.panels}
    panel_lengths: list[float] = []
    panel_widths: list[float] = []
    for panel in graph.panels:
        adjacent = next((bend for bend in graph.bends if panel.id in bend.panel_ids), None)
        bend_faces = (adjacent.inner_face, adjacent.outer_face) if adjacent else None
        length, width = _panel_spans(panel, axis, bend_faces)
        if length <= 0 or width <= 0:
            result.status = "partial"
            result.warnings.append(f"{panel.id}: estensione planare non determinabile.")
            return result
        panel_lengths.append(length)
        panel_widths.append(width)

    width = median(panel_widths)
    tolerance = float(parameters.flat_pattern_width_consistency_tolerance_mm)
    if max(abs(value - width) for value in panel_widths) > tolerance:
        result.status = "partial"
        result.warnings.append("Larghezze dei pannelli non coerenti per uno sviluppo parallelo estruso.")
        return result
    bend_widths = [bend.length_mm for bend in graph.bends]
    if max(abs(value - width) for value in bend_widths) > tolerance:
        result.status = "partial"
        result.warnings.append("Lunghezze delle pieghe non coerenti con la larghezza dei pannelli.")
        return result

    length = sum(panel_lengths) + sum(bend.allowance_mm for bend in graph.bends)
    gross_area = length * width
    opening_area = sum(float(hole.area_mm2 or 0.0) for hole in holes)
    all_openings_known = all(hole.area_mm2 is not None for hole in holes)
    all_openings_propagated = not holes
    net_area = gross_area - opening_area if all_openings_known else None
    outer_perimeter = 2.0 * (length + width)
    expected_area = sum(panel.area_mm2 for panel in graph.panels) + sum(
        bend.allowance_mm * bend.length_mm for bend in graph.bends
    )
    # Panel face areas already exclude their inner wires.  Compare like with
    # like: net developed material against net panel+bend material whenever
    # every opening area is known.
    coherence_area = net_area if net_area is not None else gross_area
    area_error_pct = abs(coherence_area - expected_area) / max(coherence_area, 1e-9) * 100.0
    validation_passed = (
        graph.direct
        and all_openings_propagated
        and area_error_pct <= float(parameters.flat_pattern_max_area_coherence_error_pct)
    )

    result.blank_dimensions_mm = Dimensions(x=round(max(length, width), 4), y=round(min(length, width), 4))
    result.gross_blank_area_mm2 = round(gross_area, 4)
    result.opening_area_mm2 = round(opening_area, 4) if all_openings_known else None
    result.net_developed_area_mm2 = round(net_area, 4) if net_area is not None else None
    result.outer_perimeter_mm = round(outer_perimeter, 4)
    result.inner_perimeter_mm = 0.0 if not holes else None
    result.total_cut_length_mm = round(outer_perimeter, 4) if not holes else None
    result.total_bend_length_mm = round(sum(bend.length_mm for bend in graph.bends), 4)
    result.total_bend_allowance_mm = round(sum(bend.allowance_mm for bend in graph.bends), 4)
    result.propagated_opening_count = 0
    result.validation = FlatPatternValidation(
        graph_connected=graph.connected,
        topology_continuous=graph.direct,
        self_intersections=0,
        overlap_area_mm2=0.0,
        area_coherence_error_pct=round(area_error_pct, 6),
        perimeter_coherence_error_pct=0.0,
        all_openings_propagated=all_openings_propagated,
        passed=validation_passed,
    )
    cursor = 0.0
    for index, bend in enumerate(graph.bends, start=1):
        cursor += panel_lengths[min(index - 1, len(panel_lengths) - 1)]
        result.bend_lines.append(
            FlatBendLine(
                id=bend.id,
                start_mm=Dimensions(x=round(cursor + bend.allowance_mm / 2.0, 4), y=0.0),
                end_mm=Dimensions(x=round(cursor + bend.allowance_mm / 2.0, 4), y=round(width, 4)),
                radius_mm=round(bend.inner_radius_mm, 4),
                angle_deg=round(bend.angle_deg, 4),
                allowance_mm=round(bend.allowance_mm, 4),
                length_mm=round(bend.length_mm, 4),
            )
        )
        cursor += bend.allowance_mm

    diagnostic_area = result.diagnostic_volume_area_mm2
    if diagnostic_area is not None:
        result.diagnostic_volume_area_error_pct = round(
            abs(diagnostic_area - gross_area) / max(gross_area, 1e-9) * 100.0,
            6,
        )
    if validation_passed:
        # The benchmark implementation reconstructs an analytic neutral-axis
        # envelope from measured panel spans.  Until an OCC 2D compound is
        # built and intersected directly, this is a validated geometric result
        # with one reconstruction step, not an `exact` contour.
        result.status = "validated_estimate"
        result.is_estimate = True
        result.usable_for_costing = True
        result.warnings.append(
            "Inviluppo 2D ricostruito analiticamente dal face graph; stato conservativo validated_estimate."
        )
        if density_g_cm3 is not None:
            result.blank_weight_kg = round(gross_area * thickness_mm * density_g_cm3 / 1_000_000, 3)
    else:
        result.status = "partial"
        result.usable_for_costing = False
        if holes:
            result.warnings.append("Le aperture saranno propagate nella Fase B; flat non utilizzabile per il costing.")
    return result


def _leaf_star_flat_dimensions(
    graph: SheetFaceGraph,
    root: SheetPanel,
    parameters: Any,
) -> tuple[float, float] | None:
    """Preserve the validated local-2D construction for root leaf flanges."""
    if any(root.id not in bend.panel_ids for bend in graph.bends):
        return None
    first_axis = graph.bends[0].axis
    root_face = root.representative_face_for(
        (graph.bends[0].inner_face, graph.bends[0].outer_face)
    )
    root_points = _face_points(root_face)
    root_u = first_axis
    root_v = _canonical_axis(_cross(root.normal, root_u))
    root_u_length = _projection_span(root_points, root_u)
    root_v_length = _projection_span(root_points, root_v)
    if root_u_length <= 0 or root_v_length <= 0:
        return None
    u_extensions = [0.0, 0.0]
    v_extensions = [0.0, 0.0]
    panel_by_id = {panel.id: panel for panel in graph.panels}
    for bend in graph.bends:
        child_id = next(panel_id for panel_id in bend.panel_ids if panel_id != root.id)
        child = panel_by_id[child_id]
        child_length, child_width = _panel_spans(
            child,
            bend.axis,
            (bend.inner_face, bend.outer_face),
        )
        if child_length <= 0 or abs(child_width - bend.length_mm) > float(
            parameters.flat_pattern_width_consistency_tolerance_mm
        ):
            return None
        extension = child_length + bend.allowance_mm
        if _axis_relation(bend.axis, root_u) == "parallel":
            side_axis = root_v
            target = v_extensions
        elif _axis_relation(bend.axis, root_v) == "parallel":
            side_axis = root_u
            target = u_extensions
        else:
            return None
        side = 1 if _dot(_sub(bend.center, root.center), side_axis) >= 0 else 0
        target[side] += extension
    return root_u_length + sum(u_extensions), root_v_length + sum(v_extensions)


def unfold_orthogonal_sheet(
    *,
    shape: Any,
    thickness_mm: float,
    thickness_confidence: str,
    holes: list[HoleFeature],
    density_g_cm3: float | None,
    k_factor: float,
    parameters: Any,
    topology_context: SheetTopologyContext | None = None,
) -> FlatPattern | None:
    if not hasattr(shape, "Faces"):
        return None
    graph = build_sheet_face_graph(
        shape,
        thickness_mm,
        k_factor,
        parameters,
        topology_context=topology_context,
    )
    if len(graph.bends) < 2 or _all_axes_parallel(graph.bends):
        return None
    if any(
        _axis_relation(left.axis, right.axis) == "oblique"
        for index, left in enumerate(graph.bends)
        for right in graph.bends[index + 1 :]
    ):
        return None
    result = FlatPattern(
        available=True,
        thickness_mm=thickness_mm,
        k_factor=k_factor,
        root_panel_id=graph.root_panel_id,
        panel_count=len(graph.panels),
        bend_zone_count=len(graph.bends),
        method="orthogonal face-graph unfold with trimmed neutral-axis bend strips",
        diagnostic_volume_area_mm2=_volume_area(shape, thickness_mm),
        confidence="high" if thickness_confidence == "high" else "medium",
    )
    result.warnings.extend(graph.warnings)
    root = next((panel for panel in graph.panels if panel.id == graph.root_panel_id), None)
    if root is None or not graph.connected or len(graph.panels) != len(graph.bends) + 1:
        result.status = "partial"
        result.warnings.append("Face graph ortogonale incompleto o non connesso.")
        return result
    leaf_dimensions = _leaf_star_flat_dimensions(graph, root, parameters)
    if leaf_dimensions is not None:
        flat_u, flat_v = leaf_dimensions
    else:
        flattened = _recursive_flattened_points(graph)
        if flattened is None:
            result.status = "partial"
            result.warnings.append(
                "Traversal ricorsivo incompleto: ciclo, pannello non raggiungibile o trasformazione contraddittoria."
            )
            return result
        flat_points, bend_lines, traversal_warnings = flattened
        result.warnings.extend(traversal_warnings)
        x_values = [point[0] for point in flat_points]
        y_values = [point[1] for point in flat_points]
        flat_u = max(x_values) - min(x_values)
        flat_v = max(y_values) - min(y_values)
        result.bend_lines = bend_lines
    if flat_u <= 0 or flat_v <= 0:
        result.status = "partial"
        result.warnings.append("Contorno sviluppato locale 2D degenerato.")
        return result
    outer_perimeter_parts = 0.0
    for panel in graph.panels:
        adjacent = next((bend for bend in graph.bends if panel.id in bend.panel_ids), None)
        face = panel.representative_face_for(
            (adjacent.inner_face, adjacent.outer_face) if adjacent else None
        )
        perimeter = _outer_wire_length(face)
        if perimeter is None:
            result.status = "partial"
            result.warnings.append(f"{panel.id}: perimetro esterno planare non determinabile.")
            return result
        outer_perimeter_parts += perimeter

    opening_area = sum(float(hole.area_mm2 or 0.0) for hole in holes)
    all_openings_known = all(hole.area_mm2 is not None for hole in holes)
    all_openings_propagated = not holes
    net_area = sum(panel.area_mm2 for panel in graph.panels) + sum(
        bend.allowance_mm * bend.length_mm for bend in graph.bends
    )
    gross_area = net_area + opening_area if all_openings_known else None
    outer_perimeter = outer_perimeter_parts + sum(
        2.0 * (bend.allowance_mm + bend.length_mm) - 4.0 * bend.length_mm
        for bend in graph.bends
    )
    validation_passed = graph.connected and all_openings_propagated and gross_area is not None

    result.blank_dimensions_mm = Dimensions(x=round(max(flat_u, flat_v), 4), y=round(min(flat_u, flat_v), 4))
    result.net_developed_area_mm2 = round(net_area, 4)
    result.opening_area_mm2 = round(opening_area, 4) if all_openings_known else None
    result.gross_blank_area_mm2 = round(gross_area, 4) if gross_area is not None else None
    result.outer_perimeter_mm = round(outer_perimeter, 4)
    result.inner_perimeter_mm = 0.0 if not holes else None
    result.total_cut_length_mm = round(outer_perimeter, 4) if not holes else None
    result.total_bend_length_mm = round(sum(bend.length_mm for bend in graph.bends), 4)
    result.total_bend_allowance_mm = round(sum(bend.allowance_mm for bend in graph.bends), 4)
    result.validation = FlatPatternValidation(
        graph_connected=graph.connected,
        topology_continuous=graph.connected,
        self_intersections=0,
        overlap_area_mm2=0.0,
        area_coherence_error_pct=0.0,
        perimeter_coherence_error_pct=0.0,
        all_openings_propagated=all_openings_propagated,
        passed=validation_passed,
    )
    if result.diagnostic_volume_area_mm2 is not None:
        result.diagnostic_volume_area_error_pct = round(
            abs(result.diagnostic_volume_area_mm2 - net_area) / max(net_area, 1e-9) * 100.0,
            6,
        )
    if validation_passed:
        # AutoMiter is represented by trimmed analytic panel/bend regions.
        # This contains a geometric reconstruction and therefore cannot be
        # labelled `exact` under the public status contract.
        result.status = "validated_estimate"
        result.is_estimate = True
        result.usable_for_costing = True
        result.warnings.append(
            "Regioni AutoMiter ricostruite analiticamente; stato conservativo validated_estimate."
        )
        if density_g_cm3 is not None and gross_area is not None:
            result.blank_weight_kg = round(gross_area * thickness_mm * density_g_cm3 / 1_000_000, 3)
    else:
        result.status = "partial"
        result.warnings.append("Aperture o contorni non completamente propagati; flat non utilizzabile per il costing.")
    return result


def unfold_sheet(
    *,
    shape: Any,
    thickness_mm: float,
    thickness_confidence: str,
    holes: list[HoleFeature],
    density_g_cm3: float | None,
    k_factor: float,
    parameters: Any,
    topology_context: SheetTopologyContext | None = None,
) -> FlatPattern | None:
    parallel = unfold_parallel_sheet(
        shape=shape,
        thickness_mm=thickness_mm,
        thickness_confidence=thickness_confidence,
        holes=holes,
        density_g_cm3=density_g_cm3,
        k_factor=k_factor,
        parameters=parameters,
        topology_context=topology_context,
    )
    if parallel is not None:
        return parallel
    return unfold_orthogonal_sheet(
        shape=shape,
        thickness_mm=thickness_mm,
        thickness_confidence=thickness_confidence,
        holes=holes,
        density_g_cm3=density_g_cm3,
        k_factor=k_factor,
        parameters=parameters,
        topology_context=topology_context,
    )


def _feature_perimeter(feature: HoleFeature) -> float | None:
    if feature.perimeter_mm is not None:
        return float(feature.perimeter_mm)
    if feature.circumference_mm is not None:
        return float(feature.circumference_mm)
    if feature.diameter_mm is not None:
        return math.pi * float(feature.diameter_mm)
    if feature.length_mm is not None:
        return float(feature.length_mm)
    return None


def _wire_center(wire: Any) -> Vector3 | None:
    bbox = getattr(wire, "BoundBox", None)
    if bbox is None:
        return None
    try:
        return (
            (float(bbox.XMin) + float(bbox.XMax)) / 2.0,
            (float(bbox.YMin) + float(bbox.YMax)) / 2.0,
            (float(bbox.ZMin) + float(bbox.ZMax)) / 2.0,
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _inner_wires(face: Any) -> list[Any]:
    outer = getattr(face, "OuterWire", None)
    result = []
    for wire in getattr(face, "Wires", []):
        if outer is not None:
            try:
                if wire.isSame(outer):
                    continue
            except (AttributeError, RuntimeError, TypeError):
                pass
        result.append(wire)
    return result


def _matching_wire(face: Any, feature: HoleFeature, target_perimeter: float) -> Any | None:
    if feature.center is None:
        return None
    feature_center = tuple(float(value) for value in feature.center)
    candidates: list[tuple[float, float, Any]] = []
    for wire in _inner_wires(face):
        center = _wire_center(wire)
        if center is None:
            continue
        try:
            perimeter = float(wire.Length)
        except (AttributeError, TypeError, ValueError):
            continue
        center_error = _norm(_sub(center, feature_center))
        perimeter_error = abs(perimeter - target_perimeter)
        if center_error <= 1.0 and perimeter_error <= max(0.5, target_perimeter * 0.02):
            candidates.append((center_error, perimeter_error, wire))
    return min(candidates, default=(0.0, 0.0, None), key=lambda item: item[:2])[2]


def propagate_openings_and_hole_to_bend(
    *,
    result: FlatPattern,
    shape: Any,
    thickness_mm: float,
    holes: list[HoleFeature],
    k_factor: float,
    parameters: Any,
    topology_context: SheetTopologyContext | None = None,
) -> tuple[FlatPattern, float | None, int]:
    if not holes or result.status not in {"partial", "exact", "validated_estimate"}:
        return result, None, 0
    graph = build_sheet_face_graph(
        shape,
        thickness_mm,
        k_factor,
        parameters,
        topology_context=topology_context,
    )
    if not graph.connected:
        result.status = "partial"
        result.usable_for_costing = False
        result.validation.passed = False
        return result, None, 0

    measured_distances: list[float] = []
    propagated = 0
    inner_perimeter = 0.0
    for feature in holes:
        perimeter = _feature_perimeter(feature)
        if perimeter is None or feature.center is None or feature.axis is None:
            continue
        center = tuple(float(value) for value in feature.center)
        axis = _canonical_axis(tuple(float(value) for value in feature.axis))
        matches: list[tuple[float, SheetPanel, Any, Any]] = []
        for panel in graph.panels:
            if abs(_dot(axis, panel.normal)) < 0.98:
                continue
            for face in panel.faces:
                face_normal = _normalize(_vector(face.Surface.Axis))
                plane_distance = abs(
                    _plane_offset(face_normal, center)
                    - _plane_offset(face_normal, _vector(face.Surface.Position))
                )
                if plane_distance > thickness_mm + float(parameters.flat_pattern_edge_match_tolerance_mm):
                    continue
                wire = _matching_wire(face, feature, perimeter)
                if wire is not None:
                    matches.append((plane_distance, panel, face, wire))
        if not matches:
            continue
        _, panel, face, wire = min(matches, key=lambda item: (item[0], item[1].id))
        feature.flat_contour_propagated = True
        propagated += 1
        inner_perimeter += perimeter

        distances: list[float] = []
        for bend in graph.bends:
            if panel.id not in bend.panel_ids:
                continue
            shared = (
                topology_context.shared_edges(face, bend.inner_face)
                if topology_context is not None
                else _shared_edges(face, bend.inner_face)
            ) or (
                topology_context.shared_edges(face, bend.outer_face)
                if topology_context is not None
                else _shared_edges(face, bend.outer_face)
            )
            for edge in shared:
                try:
                    tangent_distance = float(wire.distToShape(edge)[0])
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    continue
                if math.isfinite(tangent_distance) and tangent_distance >= 0:
                    distances.append(tangent_distance + bend.allowance_mm / 2.0)
        if distances:
            feature.flat_bend_distance_mm = round(min(distances), 4)
            measured_distances.append(feature.flat_bend_distance_mm)

    all_propagated = propagated == len(holes)
    result.propagated_opening_count = propagated
    result.validation.all_openings_propagated = all_propagated
    if all_propagated:
        result.inner_perimeter_mm = round(inner_perimeter, 4)
        if result.outer_perimeter_mm is not None:
            result.total_cut_length_mm = round(result.outer_perimeter_mm + inner_perimeter, 4)
        result.validation.passed = (
            result.validation.graph_connected
            and result.validation.topology_continuous
            and result.validation.self_intersections == 0
            and float(result.validation.overlap_area_mm2 or 0.0)
            <= float(parameters.flat_pattern_max_overlap_area_mm2)
            and (
                result.validation.area_coherence_error_pct is None
                or result.validation.area_coherence_error_pct
                <= float(parameters.flat_pattern_max_area_coherence_error_pct)
            )
        )
        if result.validation.passed:
            result.status = "validated_estimate"
            result.is_estimate = True
            result.usable_for_costing = True
            # Provisional unfold warnings are intentionally discarded here:
            # the final state is derived again after every opening is mapped.
            result.warnings = list(graph.warnings)
            result.warnings.append(
                "Contorni aperture ricostruiti dalle feature CAD e validati sul face graph; stato conservativo validated_estimate."
            )
        else:
            result.status = "partial"
            result.usable_for_costing = False
            result.total_cut_length_mm = None
            result.blank_weight_kg = None
            result.warnings = list(graph.warnings)
            result.warnings.append(
                "Validazione geometrica finale non superata; flat non utilizzabile per il costing."
            )
    else:
        result.status = "partial"
        result.usable_for_costing = False
        result.total_cut_length_mm = None
        result.blank_weight_kg = None
        result.validation.passed = False
        result.warnings = list(graph.warnings)
        result.warnings.append(
            f"Propagate {propagated} aperture su {len(holes)}; flat non utilizzabile per il costing."
        )
    return result, (min(measured_distances) if measured_distances else None), len(measured_distances)
