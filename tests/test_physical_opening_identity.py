from __future__ import annotations

from types import SimpleNamespace

from app.cad_analyzer import (
    _build_physical_opening_context,
    _detect_physical_openings,
    load_analysis_config,
)


def _vector(x=0.0, y=0.0, z=0.0):
    return SimpleNamespace(x=x, y=y, z=z)


def _bbox(x_length, y_length, z_length=0.0, *, x_min=0.0, y_min=0.0, z_min=0.0):
    return SimpleNamespace(
        XMin=x_min,
        XMax=x_min + x_length,
        YMin=y_min,
        YMax=y_min + y_length,
        ZMin=z_min,
        ZMax=z_min + z_length,
        XLength=x_length,
        YLength=y_length,
        ZLength=z_length,
    )


class _Edge:
    _next_hash = 1

    def __init__(self, type_id, *, radius=None, center=None, direction=None, length=1.0):
        self.Curve = SimpleNamespace(
            TypeId=type_id,
            Radius=radius,
            Center=center,
            Direction=direction,
        )
        self.Length = length
        self._hash = _Edge._next_hash
        _Edge._next_hash += 1

    def isSame(self, other):
        return self is other

    def hashCode(self):
        return self._hash


class _Wire:
    def __init__(self, edges, *, length, bbox, closed=True):
        self.Edges = list(edges)
        self.Length = length
        self.BoundBox = bbox
        self._closed = closed

    def isClosed(self):
        return self._closed

    def isSame(self, other):
        return self is other


def _circular_wire(radius, z, edges=None):
    circle_edges = edges or [
        _Edge(
            "Part::GeomCircle",
            radius=radius,
            center=_vector(10.0, 10.0, z),
            length=2.0 * 3.141592653589793 * radius,
        )
    ]
    return _Wire(
        circle_edges,
        length=2.0 * 3.141592653589793 * radius,
        bbox=_bbox(
            radius * 2.0,
            radius * 2.0,
            x_min=10.0 - radius,
            y_min=10.0 - radius,
            z_min=z,
        ),
    )


def _planar_face(inner_wires, *, z, inner_first=False):
    outer = _Wire([], length=100.0, bbox=_bbox(40.0, 20.0, z_min=z))
    wires = [*inner_wires, outer] if inner_first else [outer, *inner_wires]
    return SimpleNamespace(
        Surface=SimpleNamespace(
            TypeId="Part::GeomPlane",
            Axis=_vector(z=1.0),
            Position=_vector(z=z),
        ),
        OuterWire=outer,
        Wires=wires,
        Edges=[edge for wire in wires for edge in wire.Edges],
        Area=800.0,
    )


def _wall_face(type_id, edges, *, radius=3.0, depth=2.0, span=3.141592653589793):
    values = {
        "TypeId": type_id,
        "Axis": _vector(z=1.0),
        "Center": _vector(10.0, 10.0, 0.0),
    }
    if type_id == "Part::GeomCylinder":
        values["Radius"] = radius
    return SimpleNamespace(
        Surface=SimpleNamespace(**values),
        ParameterRange=(0.0, span, 0.0, depth),
        BoundBox=_bbox(radius * 2.0, radius * 2.0, depth),
        Edges=list(edges),
        Area=10.0,
    )


def _topology_for_panels(shape, face_pairs):
    def faces_share_edge(left, right):
        return any(
            left_edge.isSame(right_edge)
            for left_edge in getattr(left, "Edges", [])
            for right_edge in getattr(right, "Edges", [])
        )

    return SimpleNamespace(
        shape=shape,
        faces=tuple(shape.Faces),
        panels=[
            SimpleNamespace(id=f"panel_{index:03d}", faces=pair)
            for index, pair in enumerate(face_pairs, start=1)
        ],
        bends=[],
        faces_share_edge=faces_share_edge,
    )


def test_inner_wire_at_index_zero_is_inventoried_and_paired():
    bottom = _circular_wire(3.0, 0.0)
    top = _circular_wire(3.0, 2.0)
    shape = SimpleNamespace(
        Faces=[
            _planar_face([bottom], z=0.0, inner_first=True),
            _planar_face([top], z=2.0, inner_first=True),
        ]
    )

    context = _build_physical_opening_context(
        shape,
        load_analysis_config(),
        2.0,
        topology_context=_topology_for_panels(shape, [(shape.Faces[0], shape.Faces[1])]),
    )

    assert context.raw_contour_count == 2
    assert len(context.identities) == 1


def test_opposite_skins_at_342_mm_form_one_physical_opening():
    shape = SimpleNamespace(
        Faces=[
            _planar_face([_circular_wire(4.0, 0.0)], z=0.0),
            _planar_face([_circular_wire(4.0, 3.42)], z=3.42),
        ]
    )
    parameters = load_analysis_config()
    context = _build_physical_opening_context(
        shape,
        parameters,
        3.42,
        topology_context=_topology_for_panels(shape, [(shape.Faces[0], shape.Faces[1])]),
    )
    holes, _ = _detect_physical_openings(shape, context, parameters, 3.42)

    assert len(context.identities) == 1
    assert len(holes.circular) == 1
    assert holes.circular[0].depth_mm == 3.42


def test_two_distinct_coaxial_passages_remain_two_identities():
    shape = SimpleNamespace(
        Faces=[
            _planar_face([_circular_wire(3.0, 0.0)], z=0.0),
            _planar_face([_circular_wire(3.0, 3.42)], z=3.42),
            _planar_face([_circular_wire(3.0, 10.0)], z=10.0),
            _planar_face([_circular_wire(3.0, 13.42)], z=13.42),
        ]
    )

    context = _build_physical_opening_context(
        shape,
        load_analysis_config(),
        3.42,
        topology_context=_topology_for_panels(
            shape,
            [
                (shape.Faces[0], shape.Faces[1]),
                (shape.Faces[2], shape.Faces[3]),
            ],
        ),
    )

    assert len(context.identities) == 2
    assert sorted(round(identity.center[2], 2) for identity in context.identities) == [1.71, 11.71]


def test_different_opposite_profiles_require_direct_wall_evidence():
    top_rim = _Edge("Part::GeomCircle", radius=6.0, center=_vector(10.0, 10.0, 2.0))
    bottom_rim = _Edge("Part::GeomCircle", radius=3.0, center=_vector(10.0, 10.0, 0.0))
    transition = _Edge("Part::GeomCircle", radius=3.0, center=_vector(10.0, 10.0, 1.0))
    top = _circular_wire(6.0, 2.0, [top_rim])
    bottom = _circular_wire(3.0, 0.0, [bottom_rim])
    bottom_face = _planar_face([bottom], z=0.0)
    top_face = _planar_face([top], z=2.0)
    shape_without_wall = SimpleNamespace(Faces=[bottom_face, top_face])
    parameters = load_analysis_config()

    without_wall = _build_physical_opening_context(shape_without_wall, parameters, 2.0)
    assert without_wall.identities == []

    cone = _wall_face("Part::GeomCone", [top_rim, transition], depth=1.0)
    cylinder = _wall_face(
        "Part::GeomCylinder",
        [bottom_rim, transition],
        radius=3.0,
        depth=1.0,
    )
    shape_with_wall = SimpleNamespace(Faces=[bottom_face, top_face, cone, cylinder])
    with_wall = _build_physical_opening_context(shape_with_wall, parameters, 2.0)
    holes, _ = _detect_physical_openings(shape_with_wall, with_wall, parameters, 2.0)

    assert len(with_wall.identities) == 1
    assert with_wall.identities[0].evidence == "direct_wall"
    assert len(holes.circular) == 1
    assert holes.circular[0].diameter_mm == 6.0


def test_bend_contact_and_boundary_wall_preserve_real_features_conservatively():
    bottom_bspline = _Edge("Part::GeomBSplineCurve", length=20.0)
    top_bspline = _Edge("Part::GeomBSplineCurve", length=20.0)
    shared_bottom = _Edge("Part::GeomLine", direction=_vector(x=1.0), length=10.0)
    shared_top = _Edge("Part::GeomLine", direction=_vector(x=1.0), length=10.0)
    bottom = _Wire(
        [bottom_bspline, shared_bottom],
        length=30.0,
        bbox=_bbox(10.0, 8.0, x_min=5.0, y_min=6.0),
    )
    top = _Wire(
        [top_bspline, shared_top],
        length=30.0,
        bbox=_bbox(10.0, 8.0, x_min=5.0, y_min=6.0, z_min=2.0),
    )
    bottom_face = _planar_face([bottom], z=0.0)
    top_face = _planar_face([top], z=2.0)
    bottom_bend_face = _wall_face(
        "Part::GeomCylinder",
        [shared_bottom],
        radius=3.0,
        depth=2.0,
    )
    top_bend_face = _wall_face(
        "Part::GeomCylinder",
        [shared_top],
        radius=5.0,
        depth=2.0,
    )
    shape = SimpleNamespace(
        Faces=[bottom_face, top_face, bottom_bend_face, top_bend_face]
    )
    topology = SimpleNamespace(
        shape=shape,
        faces=tuple(shape.Faces),
        panels=[SimpleNamespace(id="panel_001", faces=(bottom_face, top_face))],
        bends=[
            SimpleNamespace(
                inner_face=bottom_bend_face,
                outer_face=top_bend_face,
            )
        ],
        faces_share_edge=lambda left, right: any(
            left_edge.isSame(right_edge)
            for left_edge in getattr(left, "Edges", [])
            for right_edge in getattr(right, "Edges", [])
        ),
    )

    context = _build_physical_opening_context(
        shape,
        load_analysis_config(),
        2.0,
        topology_context=topology,
    )
    holes, _ = _detect_physical_openings(
        shape,
        context,
        load_analysis_config(),
        2.0,
    )

    assert context.raw_contour_count == 2
    assert len(context.identities) == 1
    assert context.identities[0].wall_faces == ()
    assert len(holes.formed) == 1

    # The other historical representation is an open-to-boundary cutout:
    # one bounded wall spans both already-paired skins but has no inner wire.
    bottom_skin = _planar_face([], z=0.0)
    top_skin = _planar_face([], z=2.0)
    bottom_fragment = _planar_face([], z=0.0)
    top_fragment = _planar_face([], z=2.0)
    bottom_edge = _Edge("Part::GeomLine", direction=_vector(x=1.0), length=8.0)
    top_edge = _Edge("Part::GeomLine", direction=_vector(x=1.0), length=8.0)
    bottom_fragment_edge = _Edge(
        "Part::GeomLine", direction=_vector(x=1.0), length=3.0
    )
    top_fragment_edge = _Edge(
        "Part::GeomLine", direction=_vector(x=1.0), length=3.0
    )
    bottom_skin.OuterWire.Edges.append(bottom_edge)
    bottom_skin.Edges.append(bottom_edge)
    top_skin.OuterWire.Edges.append(top_edge)
    top_skin.Edges.append(top_edge)
    bottom_fragment.OuterWire.Edges.append(bottom_fragment_edge)
    bottom_fragment.Edges.append(bottom_fragment_edge)
    top_fragment.OuterWire.Edges.append(top_fragment_edge)
    top_fragment.Edges.append(top_fragment_edge)
    boundary_edges = [
        bottom_edge,
        top_edge,
        bottom_fragment_edge,
        top_fragment_edge,
        *[
            _Edge("Part::GeomLine", direction=_vector(y=1.0), length=3.5)
            for _ in range(2)
        ],
    ]
    boundary_wire = _Wire(
        boundary_edges,
        length=30.0,
        bbox=_bbox(8.0, 5.0, 2.0, x_min=15.0, y_min=4.0),
    )
    boundary_face = SimpleNamespace(
        Surface=SimpleNamespace(
            TypeId="Part::GeomPlane",
            Axis=_vector(y=1.0),
            Position=_vector(y=4.0),
        ),
        OuterWire=boundary_wire,
        Wires=[boundary_wire],
        Edges=boundary_edges,
        Area=30.0,
    )
    boundary_shape = SimpleNamespace(
        Faces=[
            bottom_skin,
            top_skin,
            bottom_fragment,
            top_fragment,
            boundary_face,
        ]
    )
    boundary_topology = _topology_for_panels(
        boundary_shape,
        [
            (bottom_skin, top_skin),
            (bottom_fragment, top_fragment),
        ],
    )
    boundary_context = _build_physical_opening_context(
        boundary_shape,
        load_analysis_config(),
        2.0,
        topology_context=boundary_topology,
    )
    boundary_holes, _ = _detect_physical_openings(
        boundary_shape,
        boundary_context,
        load_analysis_config(),
        2.0,
    )

    assert len(boundary_context.identities) == 1
    assert boundary_context.identities[0].evidence == "through_thickness_boundary_wall"
    assert len(boundary_holes.polygonal) == 1

    # Merely touching both skins of two genuinely different panels remains
    # ambiguous and must not create a boundary-opening identity.
    bottom_fragment.Surface.Axis = _vector(y=1.0)
    bottom_fragment.Surface.Position = _vector(y=0.0)
    top_fragment.Surface.Axis = _vector(y=1.0)
    top_fragment.Surface.Position = _vector(y=2.0)
    rejected_context = _build_physical_opening_context(
        boundary_shape,
        load_analysis_config(),
        2.0,
        topology_context=boundary_topology,
    )
    assert rejected_context.identities == []


def test_segmented_cylindrical_wall_produces_one_opening():
    top_left = _Edge("Part::GeomCircle", radius=3.0, center=_vector(3.0, 3.0, 2.0))
    top_right = _Edge("Part::GeomCircle", radius=3.0, center=_vector(3.0, 3.0, 2.0))
    bottom_left = _Edge("Part::GeomCircle", radius=3.0, center=_vector(3.0, 3.0, 0.0))
    bottom_right = _Edge("Part::GeomCircle", radius=3.0, center=_vector(3.0, 3.0, 0.0))
    seam_a = _Edge("Part::GeomLine", direction=_vector(z=1.0), length=2.0)
    seam_b = _Edge("Part::GeomLine", direction=_vector(z=1.0), length=2.0)
    top = _circular_wire(3.0, 2.0, [top_left, top_right])
    bottom = _circular_wire(3.0, 0.0, [bottom_left, bottom_right])
    first_half = _wall_face(
        "Part::GeomCylinder",
        [top_left, bottom_left, seam_a, seam_b],
        radius=3.0,
        depth=2.0,
    )
    second_half = _wall_face(
        "Part::GeomCylinder",
        [top_right, bottom_right, seam_a, seam_b],
        radius=3.0,
        depth=2.0,
    )
    shape = SimpleNamespace(
        Faces=[
            _planar_face([bottom], z=0.0),
            _planar_face([top], z=2.0),
            first_half,
            second_half,
        ]
    )
    parameters = load_analysis_config()

    context = _build_physical_opening_context(shape, parameters, 2.0)
    holes, _ = _detect_physical_openings(shape, context, parameters, 2.0)

    assert len(context.identities) == 1
    assert len(context.identities[0].wall_faces) == 2
    assert len(holes.circular) == 1
