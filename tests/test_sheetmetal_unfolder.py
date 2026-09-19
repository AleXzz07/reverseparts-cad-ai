import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cad_analyzer import _detect_bends, load_analysis_config
from app.sheetmetal_unfolder import (
    _pair_bend_faces,
    build_sheet_face_graph,
    build_sheet_topology_context,
    propagate_openings_and_hole_to_bend,
    singleton_bend_adjacent_faces,
    unfold_orthogonal_sheet,
    unfold_parallel_sheet,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeVector:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z


class FakeVertex:
    def __init__(self, point):
        self.Point = FakeVector(*point)


class FakeEdge:
    def __init__(self, edge_id, points=()):
        self.edge_id = edge_id
        self.Vertexes = [FakeVertex(point) for point in points]
        self.Length = math.dist(points[0], points[-1]) if len(points) >= 2 else 0.0

    def isSame(self, other):
        return self.edge_id == other.edge_id


class FakeBoundBox:
    def __init__(self, center):
        self.XMin = self.XMax = center[0]
        self.YMin = self.YMax = center[1]
        self.ZMin = self.ZMax = center[2]


class FakeWire:
    def __init__(self, wire_id, length, center=None, distance_to_edge=None):
        self.wire_id = wire_id
        self.Length = length
        self.BoundBox = FakeBoundBox(center) if center is not None else None
        self.distance_to_edge = distance_to_edge

    def isSame(self, other):
        return self.wire_id == other.wire_id

    def distToShape(self, _other):
        if self.distance_to_edge is None:
            raise RuntimeError("Distance is not available for this fake wire.")
        return (self.distance_to_edge, [], [])


class FakePlane:
    TypeId = "Part::GeomPlane"

    def __init__(self, normal, position):
        self.Axis = FakeVector(*normal)
        self.Position = FakeVector(*position)


class FakeCylinder:
    TypeId = "Part::GeomCylinder"

    def __init__(self, radius, axis=(0, 1, 0), center=(0, 0, 0)):
        self.Radius = radius
        self.Axis = FakeVector(*axis)
        self.Center = FakeVector(*center)


class FakeBSplineSurface:
    TypeId = "Part::GeomBSplineSurface"


class FakeFace:
    def __init__(self, surface, points, area, edges=(), parameter_range=None):
        self.Surface = surface
        self.Vertexes = [FakeVertex(point) for point in points]
        self.Area = area
        self.Edges = list(edges)
        perimeter = sum(
            math.dist(points[index], points[(index + 1) % len(points)])
            for index in range(len(points))
        )
        self.OuterWire = FakeWire(f"outer-{id(self)}", perimeter)
        self.Wires = [self.OuterWire]
        center = tuple(sum(point[index] for point in points) / len(points) for index in range(3))
        self.CenterOfMass = FakeVector(*center)
        x_values = [point[0] for point in points]
        y_values = [point[1] for point in points]
        z_values = [point[2] for point in points]
        self.BoundBox = SimpleNamespace(
            XLength=max(x_values) - min(x_values),
            YLength=max(y_values) - min(y_values),
            ZLength=max(z_values) - min(z_values),
        )
        if parameter_range is not None:
            self.ParameterRange = parameter_range


def _single_bend_shape():
    root_inner_edge = FakeEdge("root-inner")
    root_outer_edge = FakeEdge("root-outer")
    flange_inner_edge = FakeEdge("flange-inner")
    flange_outer_edge = FakeEdge("flange-outer")
    root_points_0 = [(0, 0, 0), (80, 0, 0), (80, 60, 0), (0, 60, 0)]
    root_points_2 = [(0, 0, 2), (80, 0, 2), (80, 60, 2), (0, 60, 2)]
    flange_points_0 = [(0, 0, 0), (0, 60, 0), (0, 60, 30), (0, 0, 30)]
    flange_points_2 = [(2, 0, 0), (2, 60, 0), (2, 60, 30), (2, 0, 30)]
    faces = [
        FakeFace(FakePlane((0, 0, 1), (0, 0, 0)), root_points_0, 4800, [root_inner_edge]),
        FakeFace(FakePlane((0, 0, 1), (0, 0, 2)), root_points_2, 4800, [root_outer_edge]),
        FakeFace(FakePlane((1, 0, 0), (0, 0, 0)), flange_points_0, 1800, [flange_inner_edge]),
        FakeFace(FakePlane((1, 0, 0), (2, 0, 0)), flange_points_2, 1800, [flange_outer_edge]),
        FakeFace(
            FakeCylinder(2),
            [(0, 0, 0), (0, 60, 0), (2, 0, 2), (2, 60, 2)],
            math.pi * 2 * 60 / 2,
            [root_inner_edge, flange_inner_edge],
            (0, math.pi / 2, 0, 60),
        ),
        FakeFace(
            FakeCylinder(4),
            [(0, 0, 0), (0, 60, 0), (4, 0, 4), (4, 60, 4)],
            math.pi * 4 * 60 / 2,
            [root_outer_edge, flange_outer_edge],
            (0, math.pi / 2, 0, 60),
        ),
    ]
    return SimpleNamespace(Faces=faces, Volume=13765.4867)


def _orthogonal_bend_shape():
    rx_i, rx_o = FakeEdge("rx-i"), FakeEdge("rx-o")
    fx_i, fx_o = FakeEdge("fx-i"), FakeEdge("fx-o")
    ry_i, ry_o = FakeEdge("ry-i"), FakeEdge("ry-o")
    fy_i, fy_o = FakeEdge("fy-i"), FakeEdge("fy-o")
    root0 = [(0, 0, 0), (90, 0, 0), (90, 70, 0), (0, 70, 0)]
    root2 = [(0, 0, 2), (90, 0, 2), (90, 70, 2), (0, 70, 2)]
    flange_x0 = [(0, 0, 0), (0, 70, 0), (0, 70, 25), (0, 0, 25)]
    flange_x2 = [(2, 0, 0), (2, 70, 0), (2, 70, 25), (2, 0, 25)]
    flange_y0 = [(0, 0, 0), (90, 0, 0), (90, 0, 22), (0, 0, 22)]
    flange_y2 = [(0, 2, 0), (90, 2, 0), (90, 2, 22), (0, 2, 22)]
    faces = [
        FakeFace(FakePlane((0, 0, 1), (0, 0, 0)), root0, 6300, [rx_i, ry_i]),
        FakeFace(FakePlane((0, 0, 1), (0, 0, 2)), root2, 6300, [rx_o, ry_o]),
        FakeFace(FakePlane((1, 0, 0), (0, 0, 0)), flange_x0, 1750, [fx_i]),
        FakeFace(FakePlane((1, 0, 0), (2, 0, 0)), flange_x2, 1750, [fx_o]),
        FakeFace(FakePlane((0, 1, 0), (0, 0, 0)), flange_y0, 1980, [fy_i]),
        FakeFace(FakePlane((0, 1, 0), (0, 2, 0)), flange_y2, 1980, [fy_o]),
        FakeFace(FakeCylinder(2, axis=(0, 1, 0), center=(90, 35, 0)), [(90, 0, 0), (90, 70, 0), (92, 0, 2), (92, 70, 2)], math.pi * 2 * 70 / 2, [rx_i, fx_i], (0, math.pi / 2, 0, 70)),
        FakeFace(FakeCylinder(4, axis=(0, 1, 0), center=(90, 35, 0)), [(90, 0, 0), (90, 70, 0), (94, 0, 4), (94, 70, 4)], math.pi * 4 * 70 / 2, [rx_o, fx_o], (0, math.pi / 2, 0, 70)),
        FakeFace(FakeCylinder(3, axis=(1, 0, 0), center=(45, 70, 0)), [(0, 70, 0), (90, 70, 0), (0, 73, 3), (90, 73, 3)], math.pi * 3 * 90 / 2, [ry_i, fy_i], (0, math.pi / 2, 0, 90)),
        FakeFace(FakeCylinder(5, axis=(1, 0, 0), center=(45, 70, 0)), [(0, 70, 0), (90, 70, 0), (0, 75, 5), (90, 75, 5)], math.pi * 5 * 90 / 2, [ry_o, fy_o], (0, math.pi / 2, 0, 90)),
    ]
    return SimpleNamespace(Faces=faces, Volume=21850.7078)


def _trimmed_single_skin_bend_shape():
    root_edge = FakeEdge("root", [(2, 0, 0), (2, 60, 0)])
    flange_edge = FakeEdge("flange", [(0, 0, 2), (0, 60, 2)])
    root0 = [(0, 0, 0), (80, 0, 0), (80, 60, 0), (0, 60, 0)]
    root2 = [(0, 0, 2), (80, 0, 2), (80, 60, 2), (0, 60, 2)]
    flange0 = [(0, 0, 0), (0, 60, 0), (0, 60, 30), (0, 0, 30)]
    flange2 = [(2, 0, 0), (2, 60, 0), (2, 60, 30), (2, 0, 30)]
    faces = [
        FakeFace(FakePlane((0, 0, 1), (0, 0, 0)), root0, 4800, [root_edge]),
        FakeFace(FakePlane((0, 0, 1), (0, 0, 2)), root2, 4800),
        FakeFace(FakePlane((1, 0, 0), (0, 0, 0)), flange0, 1800, [flange_edge]),
        FakeFace(FakePlane((1, 0, 0), (2, 0, 0)), flange2, 1800),
        FakeFace(
            FakeCylinder(2, center=(2, 0, 2)),
            [(2, 0, 0), (2, 60, 0), (0, 0, 2), (0, 60, 2)],
            math.pi * 2 * 60 / 2,
            [root_edge, flange_edge],
            (0, math.pi / 2, 0, 60),
        ),
    ]
    return SimpleNamespace(Faces=faces, Volume=13765.4867)


def _coaxial_cylinder_pair_shape(*, outer_axis_interval):
    inner = FakeFace(
        FakeCylinder(2, axis=(0, 1, 0), center=(0, 0, 0)),
        [(0, 0, 0), (0, 10, 0), (2, 0, 2), (2, 10, 2)],
        math.pi * 2 * 10 / 2,
        parameter_range=(0, math.pi / 2, 0, 10),
    )
    outer_start, outer_end = outer_axis_interval
    outer = FakeFace(
        FakeCylinder(4, axis=(0, 1, 0), center=(0, 0, 0)),
        [
            (0, outer_start, 0),
            (0, outer_end, 0),
            (4, outer_start, 4),
            (4, outer_end, 4),
        ],
        math.pi * 4 * (outer_end - outer_start) / 2,
        parameter_range=(0, math.pi / 2, outer_start, outer_end),
    )
    return SimpleNamespace(Faces=[inner, outer])


def _depth_two_graph_shape():
    shape = _orthogonal_bend_shape()
    root_faces = shape.Faces[0:2]
    first_flange_faces = shape.Faces[2:4]
    second_bend_faces = shape.Faces[8:10]
    for root_face in root_faces:
        root_face.Edges = [edge for edge in root_face.Edges if not edge.edge_id.startswith("ry-")]
    first_flange_faces[0].Edges.append(second_bend_faces[0].Edges[0])
    first_flange_faces[1].Edges.append(second_bend_faces[1].Edges[0])
    second_bend_faces[0].Surface.Axis = FakeVector(0, 0, 1)
    second_bend_faces[1].Surface.Axis = FakeVector(0, 0, 1)
    return shape


def test_canonical_sheetmetal_benchmark_applies_sm02_override():
    path = PROJECT_ROOT / "tests" / "dataset" / "sheetmetal_benchmark_v1.json"
    raw = path.read_text(encoding="utf-8")
    benchmark = json.loads(raw)
    pieces = {piece["id"]: piece for piece in benchmark["pieces"]}
    sm01 = pieces["SM01_1_piega_90"]["freecad_unfold_ground_truth"]
    sm02 = pieces["SM02_1_piega_60"]["freecad_unfold_ground_truth"]

    assert sm01["root_face"] == "Face4"
    assert sm02["root_face"] == "Face5"
    assert sm02["blank_dimensions_mm"] == {"length": 117.9322, "width": 60.0}
    assert sm02["material_area_from_unfold_volume_mm2"] == 7075.9292
    assert "83.4641" not in raw


def test_face_graph_has_deterministic_largest_root_and_direct_bend():
    graph = build_sheet_face_graph(
        _single_bend_shape(),
        thickness_mm=2.0,
        k_factor=0.4,
        parameters=load_analysis_config(),
    )

    assert graph.connected is True
    assert graph.direct is True
    assert len(graph.panels) == 2
    assert len(graph.bends) == 1
    assert graph.root_panel_id == next(panel.id for panel in graph.panels if panel.area_mm2 == 4800)
    assert graph.bends[0].panel_ids == [panel.id for panel in graph.panels]


def test_bend_pair_rejects_coaxial_cylinders_with_disjoint_axial_intervals():
    bends = _pair_bend_faces(
        _coaxial_cylinder_pair_shape(outer_axis_interval=(20, 30)),
        thickness_mm=2.0,
        k_factor=0.4,
        parameters=load_analysis_config(),
    )

    assert bends == []


def test_bend_pair_accepts_coaxial_cylinders_with_positive_axial_overlap():
    shape = _coaxial_cylinder_pair_shape(outer_axis_interval=(5, 15))
    bends = _pair_bend_faces(
        shape,
        thickness_mm=2.0,
        k_factor=0.4,
        parameters=load_analysis_config(),
    )

    assert len(bends) == 1
    assert bends[0].inner_face is shape.Faces[0]
    assert bends[0].outer_face is shape.Faces[1]


def test_trimmed_planar_skins_pair_when_projection_fully_covers_smaller_face(monkeypatch):
    import app.sheetmetal_unfolder as unfolder

    parameters = load_analysis_config()
    larger = FakeFace(
        FakePlane((0, 0, 1), (0, 0, 0)),
        [(0, 0, 0), (20, 0, 0), (20, 10, 0), (0, 10, 0)],
        200.0,
    )
    smaller = FakeFace(
        FakePlane((0, 0, 1), (0, 0, 2)),
        [(1, 0, 2), (20, 0, 2), (20, 10, 2), (1, 10, 2)],
        190.0,
    )
    monkeypatch.setattr(
        unfolder,
        "_projected_planar_overlap_area_mm2",
        lambda *_args: 190.0,
    )

    panels = unfolder._pair_planar_faces(
        SimpleNamespace(Faces=[larger, smaller]),
        thickness_mm=2.0,
        parameters=parameters,
    )

    assert len(panels) == 1
    assert set(panels[0].faces) == {larger, smaller}


def test_trimmed_planar_skins_reject_partial_projected_overlap(monkeypatch):
    import app.sheetmetal_unfolder as unfolder

    parameters = load_analysis_config()
    left = FakeFace(
        FakePlane((0, 0, 1), (0, 0, 0)),
        [(0, 0, 0), (20, 0, 0), (20, 10, 0), (0, 10, 0)],
        200.0,
    )
    right = FakeFace(
        FakePlane((0, 0, 1), (0, 0, 2)),
        [(1, 0, 2), (21, 0, 2), (21, 10, 2), (1, 10, 2)],
        200.0,
    )
    monkeypatch.setattr(
        unfolder,
        "_projected_planar_overlap_area_mm2",
        lambda *_args: 190.0,
    )

    panels = unfolder._pair_planar_faces(
        SimpleNamespace(Faces=[left, right]),
        thickness_mm=2.0,
        parameters=parameters,
    )

    assert panels == []


def test_per_analysis_topology_context_reuses_planar_pairs_and_face_graph(monkeypatch):
    import app.sheetmetal_unfolder as unfolder

    shape = _single_bend_shape()
    parameters = load_analysis_config()
    original = unfolder._pair_planar_faces
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(unfolder, "_pair_planar_faces", counted)
    context = build_sheet_topology_context(shape, 2.0, 0.4, parameters)
    first_graph = build_sheet_face_graph(
        shape,
        2.0,
        0.4,
        parameters,
        topology_context=context,
    )
    graph_snapshot = (
        tuple(panel.id for panel in first_graph.panels),
        tuple((bend.id, tuple(bend.panel_ids)) for bend in first_graph.bends),
        tuple(first_graph.warnings),
    )
    result = unfold_parallel_sheet(
        shape=shape,
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=2.7,
        k_factor=0.4,
        parameters=parameters,
        topology_context=context,
    )
    second_graph = build_sheet_face_graph(
        shape,
        2.0,
        0.4,
        parameters,
        topology_context=context,
    )
    for face in context.cylindrical_faces:
        singleton_bend_adjacent_faces(
            face,
            shape,
            2.0,
            parameters,
            topology_context=context,
        )

    assert result is not None
    assert first_graph is second_graph
    assert graph_snapshot == (
        tuple(panel.id for panel in second_graph.panels),
        tuple((bend.id, tuple(bend.panel_ids)) for bend in second_graph.bends),
        tuple(second_graph.warnings),
    )
    assert calls == 1


def test_cached_topology_context_preserves_bends_and_flat_output():
    shape = _single_bend_shape()
    parameters = load_analysis_config()
    uncached_bends = _detect_bends(shape, 2.0, parameters)
    uncached_flat = unfold_parallel_sheet(
        shape=shape,
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=2.7,
        k_factor=0.4,
        parameters=parameters,
    )

    context = build_sheet_topology_context(shape, 2.0, 0.4, parameters)
    cached_bends = _detect_bends(
        shape,
        2.0,
        parameters,
        topology_context=context,
    )
    cached_flat = unfold_parallel_sheet(
        shape=shape,
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=2.7,
        k_factor=0.4,
        parameters=parameters,
        topology_context=context,
    )

    assert [bend.model_dump() for bend in cached_bends] == [
        bend.model_dump() for bend in uncached_bends
    ]
    assert cached_flat is not None
    assert uncached_flat is not None
    assert cached_flat.model_dump() == uncached_flat.model_dump()


def test_singleton_bend_detection_does_not_recompute_planar_pairs(monkeypatch):
    import app.sheetmetal_unfolder as unfolder

    shape = _trimmed_single_skin_bend_shape()
    parameters = load_analysis_config()
    original = unfolder._pair_planar_faces
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(unfolder, "_pair_planar_faces", counted)
    context = build_sheet_topology_context(shape, 2.0, 0.4, parameters)
    bends = _detect_bends(
        shape,
        2.0,
        parameters,
        topology_context=context,
    )
    result = unfold_parallel_sheet(
        shape=shape,
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=2.7,
        k_factor=0.4,
        parameters=parameters,
        topology_context=context,
    )

    assert len(bends) == 1
    assert result is not None
    assert calls == 1


def test_bspline_surface_is_ignored_without_disabling_topology_context(monkeypatch):
    import app.sheetmetal_unfolder as unfolder

    parameters = load_analysis_config()
    baseline_shape = _trimmed_single_skin_bend_shape()
    baseline_bends = _detect_bends(baseline_shape, 2.0, parameters)
    baseline_flat = unfold_parallel_sheet(
        shape=baseline_shape,
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=2.7,
        k_factor=0.4,
        parameters=parameters,
    )

    shape = _trimmed_single_skin_bend_shape()
    bspline = FakeFace(
        FakeBSplineSurface(),
        [(10, 10, 1), (20, 10, 1), (20, 20, 2), (10, 20, 2)],
        100.0,
    )
    shape.Faces.append(bspline)
    original = unfolder._pair_planar_faces
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(unfolder, "_pair_planar_faces", counted)
    context = build_sheet_topology_context(shape, 2.0, 0.4, parameters)
    bends = _detect_bends(shape, 2.0, parameters, topology_context=context)
    flat = unfold_parallel_sheet(
        shape=shape,
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=2.7,
        k_factor=0.4,
        parameters=parameters,
        topology_context=context,
    )

    assert bspline not in context.planar_faces
    assert bspline not in context.cylindrical_faces
    assert id(bspline) not in context.descriptors
    assert calls == 1
    assert [bend.model_dump() for bend in bends] == [
        bend.model_dump() for bend in baseline_bends
    ]
    assert flat is not None
    assert baseline_flat is not None
    assert flat.model_dump() == baseline_flat.model_dump()


def test_trimmed_single_cylinder_fallback_requires_two_tangent_sheet_panels():
    graph = build_sheet_face_graph(
        _trimmed_single_skin_bend_shape(),
        thickness_mm=2.0,
        k_factor=0.4,
        parameters=load_analysis_config(),
    )

    assert graph.connected is True
    assert graph.direct is False
    assert len(graph.bends) == 1
    assert graph.bends[0].outer_face is None
    assert len(graph.bends[0].panel_ids) == 2


def test_recursive_unfold_traverses_a_panel_tree_beyond_root_leaves():
    shape = _depth_two_graph_shape()
    graph = build_sheet_face_graph(shape, 2.0, 0.4, load_analysis_config())
    root = graph.root_panel_id

    assert graph.connected is True
    assert any(root not in bend.panel_ids for bend in graph.bends)
    result = unfold_orthogonal_sheet(
        shape=shape,
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=7.85,
        k_factor=0.4,
        parameters=load_analysis_config(),
    )

    assert result is not None
    assert result.status == "validated_estimate"
    assert result.usable_for_costing is True
    assert result.panel_count == 3
    assert result.bend_zone_count == 2


def test_parallel_geometric_unfold_matches_sm01_reference():
    result = unfold_parallel_sheet(
        shape=_single_bend_shape(),
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=7.85,
        k_factor=0.4,
        parameters=load_analysis_config(),
    )

    assert result is not None
    assert result.status == "validated_estimate"
    assert result.usable_for_costing is True
    assert result.blank_dimensions_mm.x == pytest.approx(114.3982, abs=0.0001)
    assert result.blank_dimensions_mm.y == pytest.approx(60.0, abs=0.0001)
    assert result.gross_blank_area_mm2 == pytest.approx(6863.8938, abs=0.001)
    assert result.outer_perimeter_mm == pytest.approx(348.7965, abs=0.001)
    assert result.total_bend_allowance_mm == pytest.approx(4.3982, abs=0.001)
    assert result.validation.passed is True
    assert result.diagnostic_volume_area_error_pct == pytest.approx(0.2746, abs=0.001)


def test_parallel_unfold_with_unpropagated_hole_is_partial():
    from app.schemas import HoleFeature

    result = unfold_parallel_sheet(
        shape=_single_bend_shape(),
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[HoleFeature(area_mm2=math.pi * 4**2)],
        density_g_cm3=7.85,
        k_factor=0.4,
        parameters=load_analysis_config(),
    )

    assert result is not None
    assert result.status == "partial"
    assert result.usable_for_costing is False
    assert result.total_cut_length_mm is None
    assert result.validation.all_openings_propagated is False


def test_opening_is_propagated_and_hole_to_bend_uses_flat_bend_zone():
    from app.schemas import HoleFeature

    shape = _single_bend_shape()
    opening = FakeWire("opening", math.pi * 8.0, center=(40, 30, 0), distance_to_edge=10.0)
    shape.Faces[0].Wires.append(opening)
    # OCC planar-face Area is already net of inner wires on both sheet skins.
    shape.Faces[0].Area -= math.pi * 4.0**2
    shape.Faces[1].Area -= math.pi * 4.0**2
    hole = HoleFeature(
        feature_id="hole_001",
        diameter_mm=8.0,
        circumference_mm=math.pi * 8.0,
        area_mm2=math.pi * 4.0**2,
        center=[40.0, 30.0, 0.0],
        axis=[0.0, 0.0, 1.0],
    )
    parameters = load_analysis_config()
    result = unfold_parallel_sheet(
        shape=shape,
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[hole],
        density_g_cm3=7.85,
        k_factor=0.4,
        parameters=parameters,
    )
    assert result is not None

    result, minimum, measured = propagate_openings_and_hole_to_bend(
        result=result,
        shape=shape,
        thickness_mm=2.0,
        holes=[hole],
        k_factor=0.4,
        parameters=parameters,
    )

    assert result.status == "validated_estimate"
    assert result.usable_for_costing is True
    assert result.validation.all_openings_propagated is True
    assert result.propagated_opening_count == 1
    assert result.inner_perimeter_mm == pytest.approx(math.pi * 8.0, abs=0.001)
    assert result.total_cut_length_mm == pytest.approx(
        result.outer_perimeter_mm + math.pi * 8.0,
        abs=0.001,
    )
    assert measured == 1
    assert minimum == pytest.approx(12.1991, abs=0.0001)
    assert hole.flat_contour_propagated is True
    assert hole.flat_bend_distance_mm == pytest.approx(12.1991, abs=0.0001)
    assert not any("aperture saranno propagate" in warning.lower() for warning in result.warnings)
    assert not any("non utilizzabile" in warning.lower() for warning in result.warnings)


def test_orthogonal_automiter_unfold_matches_sm04_reference():
    result = unfold_orthogonal_sheet(
        shape=_orthogonal_bend_shape(),
        thickness_mm=2.0,
        thickness_confidence="high",
        holes=[],
        density_g_cm3=7.85,
        k_factor=0.4,
        parameters=load_analysis_config(),
    )

    assert result is not None
    assert result.status == "validated_estimate"
    assert result.blank_dimensions_mm.x == pytest.approx(119.3982, abs=0.001)
    assert result.blank_dimensions_mm.y == pytest.approx(97.9690, abs=0.001)
    assert result.net_developed_area_mm2 == pytest.approx(10875.0884, abs=0.001)
    assert result.outer_perimeter_mm == pytest.approx(434.7345, abs=0.001)
    assert result.validation.passed is True
