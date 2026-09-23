from __future__ import annotations

import math
from types import SimpleNamespace

from app.cad_analyzer import (
    _build_physical_opening_context,
    _detect_physical_openings,
    _forming_features_from_context,
    _lance_retained_panel_is_proven,
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


class _Vertex:
    _next_hash = 1

    def __init__(self, x, y, z):
        self.Point = _vector(x, y, z)
        self._hash = _Vertex._next_hash
        _Vertex._next_hash += 1

    def isSame(self, other):
        return self is other

    def hashCode(self):
        return self._hash


class _Edge:
    _next_hash = 1

    def __init__(
        self,
        type_id,
        *,
        radius=None,
        center=None,
        direction=None,
        length=1.0,
        vertices=None,
    ):
        self.Curve = SimpleNamespace(
            TypeId=type_id,
            Radius=radius,
            Center=center,
            Direction=direction,
        )
        self.Length = length
        self.Vertexes = list(vertices or [])
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


def _bend_crossing_slot_fixture(*, open_rail=False):
    radius = 3.175
    thickness = 1.21412
    inner_bend_length = 1.329941
    outer_bend_length = 2.601364
    panel_lengths = (3.502314, 0.882034, 0.882034, 3.502314)

    rail_a_vertices = [_Vertex(index, 0.0, 0.0) for index in range(8)]
    rail_b_vertices = [_Vertex(index, 0.0, thickness) for index in range(8)]

    kinds = (
        ("Part::GeomCircle", math.pi * radius, radius),
        ("Part::GeomLine", panel_lengths[0], None),
        ("Part::GeomCircle", inner_bend_length, 1.27),
        ("Part::GeomLine", panel_lengths[1], None),
        ("Part::GeomCircle", math.pi * radius, radius),
        ("Part::GeomLine", panel_lengths[2], None),
        ("Part::GeomCircle", inner_bend_length, 1.27),
        ("Part::GeomLine", panel_lengths[3], None),
    )
    outer_kinds = list(kinds)
    outer_kinds[2] = ("Part::GeomCircle", outer_bend_length, 2.48412)
    outer_kinds[6] = ("Part::GeomCircle", outer_bend_length, 2.48412)

    def make_rail(vertices, specifications):
        return [
            _Edge(
                type_id,
                radius=radius_value,
                length=length,
                vertices=(vertices[index], vertices[(index + 1) % len(vertices)]),
            )
            for index, (type_id, length, radius_value) in enumerate(specifications)
        ]

    rail_a = make_rail(rail_a_vertices, kinds)
    rail_b = make_rail(rail_b_vertices, outer_kinds)
    if open_rail:
        rail_a[-1].Vertexes[-1] = _Vertex(99.0, 0.0, 0.0)
    connectors = [
        _Edge(
            "Part::GeomLine",
            length=thickness,
            vertices=(rail_a_vertices[index], rail_b_vertices[index]),
        )
        for index in range(8)
    ]
    wall_faces = [
        _wall_face(
            "Part::GeomBSplineSurface",
            [
                rail_a[index],
                connectors[(index + 1) % 8],
                rail_b[index],
                connectors[index],
            ],
            depth=thickness,
        )
        for index in range(8)
    ]

    panel_1_a = _planar_face([], z=0.0)
    panel_1_b = _planar_face([], z=thickness)
    panel_2_a = _planar_face([], z=10.0)
    panel_2_b = _planar_face([], z=10.0 + thickness)
    for face, edges in (
        (panel_1_a, [rail_a[0], rail_a[1], rail_a[7]]),
        (panel_1_b, [rail_b[0], rail_b[1], rail_b[7]]),
        (panel_2_a, [rail_a[3], rail_a[4], rail_a[5]]),
        (panel_2_b, [rail_b[3], rail_b[4], rail_b[5]]),
    ):
        face.Edges.extend(edges)
        face.OuterWire.Edges.extend(edges)

    bend_1_inner = _wall_face("Part::GeomCylinder", [rail_a[2]], radius=1.27)
    bend_1_outer = _wall_face("Part::GeomCylinder", [rail_b[2]], radius=2.48412)
    bend_2_inner = _wall_face("Part::GeomCylinder", [rail_a[6]], radius=1.27)
    bend_2_outer = _wall_face("Part::GeomCylinder", [rail_b[6]], radius=2.48412)
    shape = SimpleNamespace(
        Faces=[
            panel_1_a,
            panel_1_b,
            panel_2_a,
            panel_2_b,
            bend_1_inner,
            bend_1_outer,
            bend_2_inner,
            bend_2_outer,
            *wall_faces,
        ]
    )
    topology = _topology_for_panels(
        shape,
        [(panel_1_a, panel_1_b), (panel_2_a, panel_2_b)],
    )
    topology.bends = [
        SimpleNamespace(
            id=f"bend_{index:03d}",
            inner_face=inner,
            outer_face=outer,
            panel_ids=["panel_001", "panel_002"],
            axis=(1.0, 0.0, 0.0),
            inner_radius_mm=1.27,
            angle_deg=60.0,
        )
        for index, (inner, outer) in enumerate(
            ((bend_1_inner, bend_1_outer), (bend_2_inner, bend_2_outer)),
            start=1,
        )
    ]
    return shape, topology, thickness


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


def test_closed_wall_component_crossing_bend_becomes_one_slot_identity():
    shape, topology, thickness = _bend_crossing_slot_fixture()
    parameters = load_analysis_config()

    context = _build_physical_opening_context(
        shape,
        parameters,
        thickness,
        topology_context=topology,
    )
    holes, _ = _detect_physical_openings(shape, context, parameters, thickness)

    assert context.raw_contour_count == 0
    assert len(context.identities) == 1
    identity = context.identities[0]
    assert identity.evidence == "through_bend_wall_component"
    assert identity.panel_ids == ("panel_001", "panel_002")
    assert identity.bend_zone_ids == ("bend_001", "bend_002")
    assert identity.axis is None
    assert len(holes.elongated) == 1
    assert holes.elongated[0].overall_length_mm == 12.7
    assert holes.elongated[0].width_mm == 6.35
    assert holes.elongated[0].axis is None


def test_open_bend_relief_wall_component_is_not_an_opening():
    shape, topology, thickness = _bend_crossing_slot_fixture(open_rail=True)

    context = _build_physical_opening_context(
        shape,
        load_analysis_config(),
        thickness,
        topology_context=topology,
    )

    assert context.raw_contour_count == 0
    assert context.identities == []


def test_uninterrupted_bend_without_wall_component_is_not_an_opening():
    shape, topology, thickness = _bend_crossing_slot_fixture()
    shape.Faces = shape.Faces[:8]
    topology.faces = tuple(shape.Faces)

    context = _build_physical_opening_context(
        shape,
        load_analysis_config(),
        thickness,
        topology_context=topology,
    )

    assert context.identities == []


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


def test_closed_circular_draw_is_not_a_physical_opening():
    bottom_rim = _Edge(
        "Part::GeomCircle",
        radius=10.0,
        center=_vector(10.0, 10.0, 0.0),
        length=2.0 * math.pi * 10.0,
    )
    top_rim = _Edge(
        "Part::GeomCircle",
        radius=10.0,
        center=_vector(10.0, 10.0, 2.0),
        length=2.0 * math.pi * 10.0,
    )
    bottom = _circular_wire(10.0, 0.0, [bottom_rim])
    top = _circular_wire(10.0, 2.0, [top_rim])
    bottom_skin = _planar_face([bottom], z=0.0)
    top_skin = _planar_face([top], z=2.0)
    shape = SimpleNamespace(
        Faces=[
            bottom_skin,
            top_skin,
            _wall_face("Part::GeomBSplineSurface", [bottom_rim], depth=4.0),
            _wall_face("Part::GeomBSplineSurface", [top_rim], depth=4.0),
        ]
    )
    parameters = load_analysis_config()

    context = _build_physical_opening_context(
        shape,
        parameters,
        2.0,
        topology_context=_topology_for_panels(shape, [(bottom_skin, top_skin)]),
    )
    holes, _ = _detect_physical_openings(shape, context, parameters, 2.0)

    assert context.identities == []
    assert len(context.forming_identities) == 1
    assert context.forming_identities[0].feature_kind == "closed_forming_feature"
    assert context.forming_identities[0].evidence == "opposite_skin_closed_form_caps"
    assert holes.circular == []
    forming_features = _forming_features_from_context(context)
    assert len(forming_features) == 1
    assert forming_features[0].type == "closed circular draw"
    assert forming_features[0].diameter_mm == 20.0


def test_u_cut_tab_connected_by_one_bend_is_a_forming_feature_not_an_opening():
    parameters = load_analysis_config()
    bottom_vertices = [
        _Vertex(0.0, 0.0, 0.0),
        _Vertex(20.0, 0.0, 0.0),
        _Vertex(20.0, 10.0, 0.0),
        _Vertex(0.0, 10.0, 0.0),
    ]
    top_vertices = [
        _Vertex(0.0, 0.0, 1.0),
        _Vertex(20.0, 0.0, 1.0),
        _Vertex(20.0, 10.0, 1.0),
        _Vertex(0.0, 10.0, 1.0),
    ]

    def profile(vertices):
        return [
            _Edge(
                "Part::GeomLine",
                length=(20.0 if index % 2 == 0 else 10.0),
                vertices=(vertices[index], vertices[(index + 1) % 4]),
            )
            for index in range(4)
        ]

    bottom_edges = profile(bottom_vertices)
    top_edges = profile(top_vertices)
    bottom_wire = _Wire(
        bottom_edges,
        length=60.0,
        bbox=_bbox(20.0, 10.0, z_min=0.0),
    )
    top_wire = _Wire(
        top_edges,
        length=60.0,
        bbox=_bbox(20.0, 10.0, z_min=1.0),
    )
    bottom_wire.Vertexes = bottom_vertices
    top_wire.Vertexes = top_vertices
    bottom_skin = _planar_face([bottom_wire], z=0.0)
    top_skin = _planar_face([top_wire], z=1.0)
    cut_wall = _wall_face(
        "Part::GeomPlane",
        [*bottom_edges[1:], *top_edges[1:]],
        depth=1.0,
    )
    bend_inner = _wall_face(
        "Part::GeomCylinder",
        [bottom_edges[0]],
        radius=1.0,
        depth=20.0,
    )
    bend_outer = _wall_face(
        "Part::GeomCylinder",
        [top_edges[0]],
        radius=2.0,
        depth=20.0,
    )
    shape = SimpleNamespace(
        Faces=[bottom_skin, top_skin, cut_wall, bend_inner, bend_outer]
    )
    topology = _topology_for_panels(shape, [(bottom_skin, top_skin)])
    topology.panels.append(
        SimpleNamespace(id="panel_002", faces=(), area_mm2=300.0)
    )
    topology.parameters = parameters
    topology.bends = [
        SimpleNamespace(
            id="bend_001",
            inner_face=bend_inner,
            outer_face=bend_outer,
            panel_ids=["panel_001", "panel_002"],
            axis=(1.0, 0.0, 0.0),
            angle_deg=90.0,
        )
    ]

    context = _build_physical_opening_context(
        shape,
        parameters,
        1.0,
        topology_context=topology,
    )
    holes, _ = _detect_physical_openings(shape, context, parameters, 1.0)
    features = _forming_features_from_context(context)

    assert context.identities == []
    assert holes.total_holes == 0
    assert len(context.forming_identities) == 1
    assert context.forming_identities[0].feature_kind == "lance_tab_forming_feature"
    assert len(features) == 1
    assert features[0].type == "lance/tab formed"
    assert features[0].length_mm == 20.0
    assert features[0].width_mm == 10.0
    assert features[0].cut_length_mm == 40.0
    assert features[0].connected_edge_length_mm == 20.0
    assert features[0].angle_deg == 90.0


def test_bend_contact_without_a_retained_tab_panel_is_not_a_lance():
    bend = SimpleNamespace(panel_ids=["panel_host", "panel_flange"])
    topology = SimpleNamespace(
        panels=[
            SimpleNamespace(id="panel_host", area_mm2=2000.0, faces=()),
            # A narrow collar bordering the profile is not retained tab
            # material: its area covers only one tenth of the U-profile.
            SimpleNamespace(id="panel_flange", area_mm2=40.0, faces=()),
        ]
    )

    assert not _lance_retained_panel_is_proven(
        host_panel_id="panel_host",
        bend=bend,
        topology_context=topology,
        connected_edge_length_mm=20.0,
        profile_dimensions_mm=(20.0, 10.0),
    )


def test_lance_requires_a_retained_panel_covering_the_tab_footprint():
    bend = SimpleNamespace(panel_ids=["panel_host", "panel_tab"])
    topology = SimpleNamespace(
        panels=[
            SimpleNamespace(id="panel_host", area_mm2=2000.0, faces=()),
            SimpleNamespace(id="panel_tab", area_mm2=300.0, faces=()),
        ]
    )

    assert _lance_retained_panel_is_proven(
        host_panel_id="panel_host",
        bend=bend,
        topology_context=topology,
        connected_edge_length_mm=20.0,
        profile_dimensions_mm=(20.0, 10.0),
    )


def test_closed_cutout_without_attached_bend_remains_a_physical_opening():
    bottom = _circular_wire(4.0, 0.0)
    top = _circular_wire(4.0, 1.0)
    bottom_skin = _planar_face([bottom], z=0.0)
    top_skin = _planar_face([top], z=1.0)
    shape = SimpleNamespace(Faces=[bottom_skin, top_skin])
    parameters = load_analysis_config()
    topology = _topology_for_panels(shape, [(bottom_skin, top_skin)])
    topology.parameters = parameters

    context = _build_physical_opening_context(
        shape,
        parameters,
        1.0,
        topology_context=topology,
    )

    assert len(context.identities) == 1


def test_true_through_wall_touching_a_bend_is_not_reclassified_as_a_lance():
    parameters = load_analysis_config()
    bottom_vertices = [
        _Vertex(0.0, 0.0, 0.0),
        _Vertex(20.0, 0.0, 0.0),
        _Vertex(20.0, 10.0, 0.0),
        _Vertex(0.0, 10.0, 0.0),
    ]
    top_vertices = [
        _Vertex(vertex.Point.x, vertex.Point.y, 1.0)
        for vertex in bottom_vertices
    ]

    def edges(vertices):
        return [
            _Edge(
                "Part::GeomLine",
                length=(20.0 if index % 2 == 0 else 10.0),
                vertices=[vertices[index], vertices[(index + 1) % 4]],
            )
            for index in range(4)
        ]

    bottom_edges = edges(bottom_vertices)
    top_edges = edges(top_vertices)
    bottom_wire = _Wire(bottom_edges, length=60.0, bbox=_bbox(20.0, 10.0))
    top_wire = _Wire(top_edges, length=60.0, bbox=_bbox(20.0, 10.0, z_min=1.0))
    bottom_wire.Vertexes = bottom_vertices
    top_wire.Vertexes = top_vertices
    bottom_skin = _planar_face([bottom_wire], z=0.0)
    top_skin = _planar_face([top_wire], z=1.0)
    # Unlike a lance, the complete contour—including the edge adjacent to the
    # bend—is connected by an actual through-thickness wall.
    through_wall = _wall_face(
        "Part::GeomPlane",
        [*bottom_edges, *top_edges],
        depth=1.0,
    )
    bend_inner = _wall_face(
        "Part::GeomCylinder", [bottom_edges[0]], radius=1.0, depth=20.0
    )
    bend_outer = _wall_face(
        "Part::GeomCylinder", [top_edges[0]], radius=2.0, depth=20.0
    )
    shape = SimpleNamespace(
        Faces=[bottom_skin, top_skin, through_wall, bend_inner, bend_outer]
    )
    topology = _topology_for_panels(shape, [(bottom_skin, top_skin)])
    topology.parameters = parameters
    topology.bends = [
        SimpleNamespace(
            id="bend_001",
            inner_face=bend_inner,
            outer_face=bend_outer,
            panel_ids=["panel_001", "panel_002"],
            axis=(1.0, 0.0, 0.0),
            angle_deg=90.0,
        )
    ]

    context = _build_physical_opening_context(
        shape,
        parameters,
        1.0,
        topology_context=topology,
    )

    assert len(context.identities) == 1
    assert context.forming_identities == []
    assert context.identities[0].evidence == "direct_wall"
    assert context.forming_identities == []


def test_closed_draw_can_terminate_on_a_paired_planar_floor():
    bottom_rim = _Edge(
        "Part::GeomCircle",
        radius=10.0,
        center=_vector(10.0, 10.0, 0.0),
        length=2.0 * math.pi * 10.0,
    )
    top_rim = _Edge(
        "Part::GeomCircle",
        radius=10.0,
        center=_vector(10.0, 10.0, 2.0),
        length=2.0 * math.pi * 10.0,
    )
    bottom_floor_edge = _Edge(
        "Part::GeomCircle",
        radius=8.0,
        center=_vector(10.0, 10.0, 4.0),
        length=2.0 * math.pi * 8.0,
    )
    top_floor_edge = _Edge(
        "Part::GeomCircle",
        radius=8.0,
        center=_vector(10.0, 10.0, 6.0),
        length=2.0 * math.pi * 8.0,
    )
    bottom_skin = _planar_face([_circular_wire(10.0, 0.0, [bottom_rim])], z=0.0)
    top_skin = _planar_face([_circular_wire(10.0, 2.0, [top_rim])], z=2.0)
    bottom_floor = _planar_face([], z=4.0)
    bottom_floor.OuterWire.Edges = [bottom_floor_edge]
    bottom_floor.Edges = [bottom_floor_edge]
    top_floor = _planar_face([], z=6.0)
    top_floor.OuterWire.Edges = [top_floor_edge]
    top_floor.Edges = [top_floor_edge]
    bottom_transition = _wall_face(
        "Part::GeomBSplineSurface", [bottom_rim, bottom_floor_edge], depth=4.0
    )
    top_transition = _wall_face(
        "Part::GeomBSplineSurface", [top_rim, top_floor_edge], depth=4.0
    )
    shape = SimpleNamespace(
        Faces=[
            bottom_skin,
            top_skin,
            bottom_floor,
            top_floor,
            bottom_transition,
            top_transition,
        ]
    )
    topology = _topology_for_panels(
        shape,
        [(bottom_skin, top_skin), (bottom_floor, top_floor)],
    )

    context = _build_physical_opening_context(
        shape,
        load_analysis_config(),
        2.0,
        topology_context=topology,
    )

    assert context.identities == []
    assert len(context.forming_identities) == 1
    assert context.forming_identities[0].evidence == "opposite_skin_closed_form_caps"


def test_shared_through_wall_remains_a_physical_opening():
    bottom_rim = _Edge(
        "Part::GeomCircle",
        radius=4.0,
        center=_vector(10.0, 10.0, 0.0),
        length=2.0 * math.pi * 4.0,
    )
    top_rim = _Edge(
        "Part::GeomCircle",
        radius=4.0,
        center=_vector(10.0, 10.0, 2.0),
        length=2.0 * math.pi * 4.0,
    )
    bottom = _circular_wire(4.0, 0.0, [bottom_rim])
    top = _circular_wire(4.0, 2.0, [top_rim])
    bottom_skin = _planar_face([bottom], z=0.0)
    top_skin = _planar_face([top], z=2.0)
    wall = _wall_face(
        "Part::GeomCylinder",
        [bottom_rim, top_rim],
        radius=4.0,
        depth=2.0,
    )
    shape = SimpleNamespace(Faces=[bottom_skin, top_skin, wall])
    parameters = load_analysis_config()

    context = _build_physical_opening_context(
        shape,
        parameters,
        2.0,
        topology_context=_topology_for_panels(shape, [(bottom_skin, top_skin)]),
    )
    holes, _ = _detect_physical_openings(shape, context, parameters, 2.0)

    assert len(context.identities) == 1
    assert context.forming_identities == []
    assert context.identities[0].evidence == "direct_wall"
    assert len(holes.circular) == 1


def test_closed_circular_form_with_central_hole_keeps_only_center_passage():
    draw_bottom_edge = _Edge(
        "Part::GeomCircle",
        radius=10.0,
        center=_vector(10.0, 10.0, 0.0),
        length=2.0 * math.pi * 10.0,
    )
    draw_top_edge = _Edge(
        "Part::GeomCircle",
        radius=10.0,
        center=_vector(10.0, 10.0, 2.0),
        length=2.0 * math.pi * 10.0,
    )
    hole_bottom_edge = _Edge(
        "Part::GeomCircle",
        radius=3.0,
        center=_vector(10.0, 10.0, 0.0),
        length=2.0 * math.pi * 3.0,
    )
    hole_top_edge = _Edge(
        "Part::GeomCircle",
        radius=3.0,
        center=_vector(10.0, 10.0, 2.0),
        length=2.0 * math.pi * 3.0,
    )
    draw_bottom = _circular_wire(10.0, 0.0, [draw_bottom_edge])
    draw_top = _circular_wire(10.0, 2.0, [draw_top_edge])
    hole_bottom = _circular_wire(3.0, 0.0, [hole_bottom_edge])
    hole_top = _circular_wire(3.0, 2.0, [hole_top_edge])
    bottom_skin = _planar_face([draw_bottom, hole_bottom], z=0.0)
    top_skin = _planar_face([draw_top, hole_top], z=2.0)
    shape = SimpleNamespace(
        Faces=[
            bottom_skin,
            top_skin,
            _wall_face("Part::GeomBSplineSurface", [draw_bottom_edge], depth=4.0),
            _wall_face("Part::GeomBSplineSurface", [draw_top_edge], depth=4.0),
            _wall_face(
                "Part::GeomCylinder",
                [hole_bottom_edge, hole_top_edge],
                radius=3.0,
                depth=2.0,
            ),
        ]
    )
    parameters = load_analysis_config()

    context = _build_physical_opening_context(
        shape,
        parameters,
        2.0,
        topology_context=_topology_for_panels(shape, [(bottom_skin, top_skin)]),
    )
    holes, _ = _detect_physical_openings(shape, context, parameters, 2.0)

    assert len(context.forming_identities) == 1
    assert len(context.identities) == 1
    assert len(holes.circular) == 1
    assert holes.circular[0].diameter_mm == 6.0


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
