import json
from types import SimpleNamespace

import pytest

from app.cad_analyzer import (
    _annotate_hole_edge_distances,
    _annotate_hole_to_hole_distances,
    _annotate_countersunk_holes,
    _cylindrical_face_angle_deg,
    _classify_part_geometry,
    _detect_bends,
    _detect_circular_holes,
    _detect_cutting_lengths,
    _detect_elongated_holes,
    _detect_polygonal_holes,
    _detect_rounded_rectangular_holes,
    _detect_sheet_thickness,
    _detect_unknown_holes,
    _estimate_flat_pattern,
    _mass_center_components,
    _planar_wire_area,
    load_analysis_config,
)
from app.schemas import BendFeature, HoleFeature


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


class _Wire:
    def __init__(self, edges, *, length, bbox, closed=True, edge_distance=None):
        self.Edges = edges
        self.Length = length
        self.BoundBox = bbox
        self._closed = closed
        self._edge_distance = edge_distance

    def isClosed(self):
        return self._closed

    def distToShape(self, _other):
        if self._edge_distance is None:
            raise AttributeError("distance not configured")
        return self._edge_distance, [], []


def _edge(type_id, *, length=None, **curve_values):
    values = {"Curve": SimpleNamespace(TypeId=type_id, **curve_values)}
    if length is not None:
        values["Length"] = length
    return SimpleNamespace(**values)


def _planar_face(inner_wires, *, position=None, axis=None, area=100.0):
    outer_wire = _Wire([], length=0.0, bbox=_bbox(0.0, 0.0), closed=False)
    return SimpleNamespace(
        Surface=SimpleNamespace(
            TypeId="Part::GeomPlane",
            Axis=axis or _vector(z=1.0),
            Position=position or _vector(),
        ),
        Wires=[outer_wire, *inner_wires],
        Area=area,
    )


def _planar_shape(inner_wire):
    return SimpleNamespace(Faces=[_planar_face([inner_wire])], Edges=[])


def _paired_planar_shape(inner_wire, opposite_wire, *, thickness=2.0):
    return SimpleNamespace(
        Faces=[
            _planar_face([inner_wire]),
            _planar_face(
                [opposite_wire],
                position=_vector(z=thickness),
            ),
        ],
        Edges=[],
    )


def _circular_wire(radius, z):
    return _Wire(
        [_edge("Part::GeomCircle", Radius=radius)],
        length=2.0 * 3.141592653589793 * radius,
        bbox=_bbox(radius * 2.0, radius * 2.0, z_min=z),
    )


def _full_cylinder_face(radius, depth, z=0.0, *, surface_center_z=None, edges=None):
    return SimpleNamespace(
        Surface=SimpleNamespace(
            TypeId="Part::GeomCylinder",
            Radius=radius,
            Axis=_vector(z=1.0),
            Center=_vector(
                x=radius,
                y=radius,
                z=z if surface_center_z is None else surface_center_z,
            ),
        ),
        ParameterRange=(0.0, 2.0 * 3.141592653589793, 0.0, depth),
        BoundBox=_bbox(radius * 2.0, radius * 2.0, depth, z_min=z),
        Edges=edges or [],
    )


def test_circular_hole_uses_one_full_cylindrical_wall_at_three_mm_thickness():
    bottom = _circular_wire(3.0, 0.0)
    top = _circular_wire(3.0, 3.0)
    shape = SimpleNamespace(
        Faces=[
            _planar_face([bottom]),
            _planar_face([top], position=_vector(z=3.0)),
            _full_cylinder_face(3.0, 3.0),
        ],
        Edges=[],
    )

    holes, cylindrical_evidence = _detect_circular_holes(
        shape,
        load_analysis_config(),
        detected_thickness_mm=3.0,
    )

    assert cylindrical_evidence == 1
    assert len(holes) == 1
    assert holes[0].diameter_mm == 6.0
    assert holes[0].depth_mm == 3.0


def test_circular_hole_fallback_pairs_opposite_contours_at_three_mm_thickness():
    shape = _paired_planar_shape(
        _circular_wire(4.0, 0.0),
        _circular_wire(4.0, 3.0),
        thickness=3.0,
    )

    holes, cylindrical_evidence = _detect_circular_holes(
        shape,
        load_analysis_config(),
        detected_thickness_mm=3.0,
    )

    assert cylindrical_evidence == 0
    assert len(holes) == 1
    assert holes[0].diameter_mm == 8.0
    assert holes[0].depth_mm == 3.0


def test_cylindrical_topology_anchors_opposite_rim_when_surface_origin_is_remote():
    shared_edge = _edge("Part::GeomCircle", Radius=3.0)
    shared_edge.isSame = lambda other: other is shared_edge
    bottom = _Wire(
        [shared_edge],
        length=2.0 * 3.141592653589793 * 3.0,
        bbox=_bbox(6.0, 6.0, z_min=0.0),
    )
    top = _circular_wire(3.0, 2.0)
    shape = SimpleNamespace(
        Faces=[
            _planar_face([bottom]),
            _planar_face([top], position=_vector(z=2.0)),
            _full_cylinder_face(
                3.0,
                2.0,
                surface_center_z=100.0,
                edges=[shared_edge],
            ),
        ],
        Edges=[],
    )

    holes, _ = _detect_circular_holes(
        shape,
        load_analysis_config(),
        detected_thickness_mm=2.0,
    )

    assert len(holes) == 1
    assert holes[0].center == pytest.approx([3.0, 3.0, 1.0])


def test_distinct_coaxial_holes_are_not_merged_by_axis_alone():
    faces = []
    for z in (0.0, 10.0):
        faces.extend(
            [
                _planar_face([_circular_wire(3.0, z)], position=_vector(z=z)),
                _planar_face([_circular_wire(3.0, z + 3.0)], position=_vector(z=z + 3.0)),
                _full_cylinder_face(3.0, 3.0, z=z),
            ]
        )
    holes, _ = _detect_circular_holes(
        SimpleNamespace(Faces=faces, Edges=[]),
        load_analysis_config(),
        detected_thickness_mm=3.0,
    )

    assert len(holes) == 2


def test_full_cylindrical_hole_surfaces_are_not_bends():
    shape = SimpleNamespace(Faces=[_full_cylinder_face(4.0, 20.0)])

    assert _detect_bends(
        shape,
        detected_thickness_mm=2.0,
        parameters=load_analysis_config(),
        part_category="sheet_metal",
    ) == []


def test_compact_solid_without_sheet_thickness_is_non_sheet_metal():
    shape = SimpleNamespace(
        Volume=45423.894,
        Area=9673.3628,
        BoundBox=_bbox(60.0, 40.0, 20.0),
    )

    result = _classify_part_geometry(shape, None, "low")

    assert result.category == "non_sheet_metal"
    assert result.confidence == "high"


def test_multi_solid_classification_precedes_detected_sheet_thickness():
    shape = SimpleNamespace(
        Solids=[object(), object()],
        Volume=6992.92,
        Area=7760.89,
        BoundBox=_bbox(120.0, 40.0, 2.0),
    )

    result = _classify_part_geometry(shape, 2.0, "high")

    assert result.category == "multi_solid"
    assert result.confidence == "high"
    assert "2 solidi" in result.reason


def test_countersink_is_attached_to_one_through_hole():
    shared_minor_edge = _edge(
        "Part::GeomCircle",
        Radius=3.0,
        Center=_vector(x=72.0, y=35.0, z=2.0),
    )
    shared_minor_edge.isSame = lambda other: other is shared_minor_edge
    major_edge = _edge(
        "Part::GeomCircle",
        Radius=6.0,
        Center=_vector(x=72.0, y=35.0, z=4.0),
    )
    major_edge.isSame = lambda other: other is major_edge
    cone = SimpleNamespace(
        Surface=SimpleNamespace(TypeId="Part::GeomCone", Axis=_vector(z=1.0)),
        ParameterRange=(0.0, 2.0 * 3.141592653589793, 0.0, 2.0),
        Edges=[shared_minor_edge, major_edge],
    )
    cylinder = _full_cylinder_face(3.0, 2.0, edges=[shared_minor_edge])
    cylinder.Surface.Center = _vector(x=72.0, y=35.0, z=0.0)
    feature = HoleFeature(
        diameter_mm=6.0,
        radius_mm=3.0,
        perimeter_mm=18.85,
        circumference_mm=18.85,
        area_mm2=28.27,
        center=[72.0, 35.0, 2.0],
        axis=[0.0, 0.0, 1.0],
        depth_mm=4.0,
        confidence="high",
    )

    count = _annotate_countersunk_holes(
        SimpleNamespace(Faces=[cylinder, cone]),
        [feature],
        load_analysis_config(),
        4.0,
    )

    assert count == 1
    assert feature.type == "countersunk"
    assert feature.diameter_mm == 6.0
    assert feature.through_diameter_mm == 6.0
    assert feature.countersink_major_diameter_mm == 12.0
    assert feature.countersink_depth_mm == 2.0
    assert feature.area_mm2 == 28.27
    assert feature.confidence == "high"


def test_unrelated_conical_face_is_not_attached_without_sheet_depth_evidence():
    cone = SimpleNamespace(
        Surface=SimpleNamespace(TypeId="Part::GeomCone", Axis=_vector(z=1.0)),
        ParameterRange=(0.0, 2.0 * 3.141592653589793, 0.0, 8.0),
        Edges=[
            _edge("Part::GeomCircle", Radius=3.0, Center=_vector(z=0.0)),
            _edge("Part::GeomCircle", Radius=8.0, Center=_vector(z=8.0)),
        ],
    )
    feature = HoleFeature(
        diameter_mm=6.0,
        center=[0.0, 0.0, 0.0],
        axis=[0.0, 0.0, 1.0],
    )

    count = _annotate_countersunk_holes(
        SimpleNamespace(Faces=[cone]),
        [feature],
        load_analysis_config(),
        4.0,
    )

    assert count == 0
    assert feature.type is None


def test_large_planar_circular_opening_is_not_limited_to_20_mm():
    inner_wire = _Wire(
        [_edge("Part::GeomCircle", Radius=15.0)],
        length=94.2478,
        bbox=_bbox(30.0, 30.0),
    )

    holes, _ = _detect_circular_holes(
        _planar_shape(inner_wire), load_analysis_config()
    )

    assert len(holes) == 1
    assert holes[0].diameter_mm == 30.0
    assert holes[0].circumference_mm == 94.25
    assert holes[0].area_mm2 == 706.86


def test_planar_slot_is_not_limited_to_old_45_60_mm_perimeter():
    def slot_wire(z, *, reverse_orientation=False):
        arc_centers = [
            _vector(z=z),
            _vector(x=30.0, z=z),
        ]
        if reverse_orientation:
            arc_centers.reverse()
        arcs = [
            _edge("Part::GeomCircle", Radius=3.0, Center=center)
            for center in arc_centers
        ]
        lines = [
            _edge("Part::GeomLine", Direction=_vector(x=1.0)),
            _edge("Part::GeomLine", Direction=_vector(x=-1.0)),
        ]
        return _Wire(
            [*arcs, *lines],
            length=100.0,
            bbox=_bbox(36.0, 6.0, z_min=z),
        )

    holes = _detect_elongated_holes(
        _paired_planar_shape(
            slot_wire(0.0),
            slot_wire(2.0, reverse_orientation=True),
        ),
        load_analysis_config(),
        2.0,
    )

    assert len(holes) == 1
    assert holes[0].length_mm == 100.0
    assert holes[0].overall_length_mm == 36.0
    assert holes[0].straight_length_mm == 30.0
    assert holes[0].end_radius_mm == 3.0
    assert holes[0].perimeter_mm == 100.0
    assert holes[0].area_mm2 == 208.27
    assert holes[0].width_mm == 6.0
    assert holes[0].axis == [0.0, 0.0, 1.0]
    assert holes[0].orientation_axis == [1.0, 0.0, 0.0]
    assert holes[0].confidence == "high"


def test_planar_slot_accepts_split_semicircle_edges_from_step_export():
    def slot_wire(z):
        arcs = [
            _edge("Part::GeomCircle", Radius=5.0, Center=_vector(x=5.0, z=z)),
            _edge("Part::GeomCircle", Radius=5.0, Center=_vector(x=25.0, z=z)),
            _edge("Part::GeomCircle", Radius=5.0, Center=_vector(x=25.0, z=z)),
        ]
        lines = [
            _edge("Part::GeomLine", Direction=_vector(x=1.0)),
            _edge("Part::GeomLine", Direction=_vector(x=-1.0)),
        ]
        return _Wire(
            [*arcs, *lines],
            length=71.4159,
            bbox=_bbox(30.0, 10.0, z_min=z),
        )

    holes = _detect_elongated_holes(
        _paired_planar_shape(slot_wire(0.0), slot_wire(2.0)),
        load_analysis_config(),
        2.0,
    )

    assert len(holes) == 1
    assert holes[0].overall_length_mm == 30.0
    assert holes[0].straight_length_mm == 20.0
    assert holes[0].width_mm == 10.0
    assert holes[0].perimeter_mm == 71.42


def test_rounded_rectangular_opening_reports_size_radius_perimeter_and_area():
    def rounded_rectangle_wire(z):
        arcs = [
            _edge(
                "Part::GeomCircle",
                Radius=4.0,
                Center=_vector(x=x, y=y, z=z),
            )
            for x, y in ((4.0, 4.0), (22.0, 4.0), (22.0, 12.0), (4.0, 12.0))
        ]
        lines = [
            _edge("Part::GeomLine", length=18.0, Direction=_vector(x=1.0)),
            _edge("Part::GeomLine", length=18.0, Direction=_vector(x=-1.0)),
            _edge("Part::GeomLine", length=8.0, Direction=_vector(y=1.0)),
            _edge("Part::GeomLine", length=8.0, Direction=_vector(y=-1.0)),
        ]
        return _Wire(
            [*arcs, *lines],
            length=77.1327,
            bbox=_bbox(26.0, 16.0, z_min=z),
        )

    holes = _detect_rounded_rectangular_holes(
        _paired_planar_shape(
            rounded_rectangle_wire(0.0),
            rounded_rectangle_wire(2.0),
        ),
        load_analysis_config(),
        2.0,
    )

    assert len(holes) == 1
    assert holes[0].type == "rounded rectangular opening"
    assert holes[0].overall_length_mm == 26.0
    assert holes[0].width_mm == 16.0
    assert holes[0].corner_radius_mm == 4.0
    assert holes[0].max_dimension_mm == 26.0
    assert holes[0].perimeter_mm == 77.13
    assert holes[0].area_mm2 == 402.27
    assert holes[0].confidence == "high"


def test_slot_is_not_classified_as_rounded_rectangular_opening():
    def slot_wire(z):
        return _Wire(
            [
                _edge("Part::GeomCircle", Radius=5.0, Center=_vector(x=5.0, z=z)),
                _edge("Part::GeomCircle", Radius=5.0, Center=_vector(x=25.0, z=z)),
                _edge("Part::GeomLine", length=20.0, Direction=_vector(x=1.0)),
                _edge("Part::GeomLine", length=20.0, Direction=_vector(x=-1.0)),
            ],
            length=71.4159,
            bbox=_bbox(30.0, 10.0, z_min=z),
        )

    assert _detect_rounded_rectangular_holes(
        _paired_planar_shape(slot_wire(0.0), slot_wire(2.0)),
        load_analysis_config(),
        2.0,
    ) == []


def test_planar_polygon_is_not_limited_to_old_20_35_mm_perimeter():
    def polygon_wire(z):
        return _Wire(
            [_edge("Part::GeomLine") for _ in range(4)],
            length=80.0,
            bbox=_bbox(30.0, 10.0, z_min=z),
        )

    holes = _detect_polygonal_holes(
        _paired_planar_shape(polygon_wire(0.0), polygon_wire(2.0)),
        load_analysis_config(),
        2.0,
    )

    assert len(holes) == 1
    assert holes[0].num_sides == 4
    assert holes[0].max_dimension_mm == 30.0
    assert holes[0].perimeter_mm == 80.0


def test_unpaired_bend_transition_wire_is_not_a_polygonal_hole():
    bend_transition = _Wire(
        [_edge("Part::GeomLine") for _ in range(4)],
        length=114.928,
        bbox=_bbox(54.0, 3.464),
    )
    shape = SimpleNamespace(
        Faces=[
            _planar_face([bend_transition]),
            _planar_face([], position=_vector(z=2.0)),
        ],
        Edges=[],
    )

    holes = _detect_polygonal_holes(shape, load_analysis_config(), 2.0)

    assert holes == []
    assert _detect_unknown_holes(shape, [], load_analysis_config(), 2.0) == []


def test_sheet_thickness_prefers_supported_skin_area_over_smaller_side_gap():
    shape = SimpleNamespace(
        Faces=[
            _planar_face([], position=_vector(z=0.0), area=8000.0),
            _planar_face([], position=_vector(z=2.0), area=8000.0),
            _planar_face(
                [],
                position=_vector(y=62.0),
                axis=_vector(y=1.0),
                area=40.0,
            ),
            _planar_face(
                [],
                position=_vector(y=63.0),
                axis=_vector(y=1.0),
                area=40.0,
            ),
        ],
        Volume=16000.0,
        Area=17000.0,
    )

    thickness, confidence = _detect_sheet_thickness(shape)

    assert thickness == 2.0
    assert confidence == "medium"


def test_cutting_length_uses_polygon_perimeter_not_max_dimension():
    outer_wire = _Wire(
        [_edge("Part::GeomLine") for _ in range(4)],
        length=300.0,
        bbox=_bbox(100.0, 50.0),
    )
    shape = SimpleNamespace(Faces=[_planar_face([])], Edges=[])
    shape.Faces[0].Wires[0] = outer_wire

    _, inner, total, _, _ = _detect_cutting_lengths(
        shape,
        circular=[],
        elongated=[],
        rounded_rectangular=[],
        polygonal=[HoleFeature(max_dimension_mm=30.0, perimeter_mm=80.0)],
        formed=[],
        unknown=[],
    )

    assert inner == 80.0
    assert total == 380.0


def test_cutting_length_includes_rounded_rectangular_perimeter():
    outer_wire = _Wire(
        [_edge("Part::GeomLine") for _ in range(4)],
        length=300.0,
        bbox=_bbox(100.0, 50.0),
    )
    shape = SimpleNamespace(Faces=[_planar_face([])], Edges=[])
    shape.Faces[0].Wires[0] = outer_wire

    _, inner, total, _, _ = _detect_cutting_lengths(
        shape,
        circular=[],
        elongated=[],
        rounded_rectangular=[HoleFeature(perimeter_mm=77.13)],
        polygonal=[],
        formed=[],
        unknown=[],
    )

    assert inner == 77.13
    assert total == 377.13


def test_planar_wire_area_fails_safely_for_an_invalid_wire(monkeypatch):
    class FakePart:
        @staticmethod
        def Face(_wire):
            raise RuntimeError("invalid planar wire")

    monkeypatch.setattr(
        "app.cad_analyzer.importlib.import_module",
        lambda _name: FakePart,
    )

    assert _planar_wire_area(object()) is None


def test_flat_pattern_reports_exact_planar_blank():
    result = _estimate_flat_pattern(
        shape=SimpleNamespace(Volume=11774.0, BoundBox=_bbox(100.0, 60.0, 2.0)),
        thickness_mm=2.0,
        thickness_confidence="high",
        bends=[],
        holes=[HoleFeature(area_mm2=113.04)],
        cutting_outer_perimeter_mm=320.0,
        density_g_cm3=2.7,
        parameters=load_analysis_config(),
    )

    assert result.available is True
    assert result.status == "exact"
    assert result.is_estimate is False
    assert result.blank_dimensions_mm is not None
    assert result.blank_dimensions_mm.x == 100.0
    assert result.blank_dimensions_mm.y == 60.0
    assert result.net_developed_area_mm2 == 5887.0
    assert result.gross_blank_area_mm2 == 6000.04
    assert result.outer_perimeter_mm == 320.0
    assert result.blank_weight_kg == 0.032
    assert result.confidence == "high"


def test_flat_pattern_restores_countersink_removal_before_area_projection():
    result = _estimate_flat_pattern(
        shape=SimpleNamespace(Volume=30410.4425, BoundBox=_bbox(110.0, 70.0, 4.0)),
        thickness_mm=4.0,
        thickness_confidence="high",
        bends=[],
        holes=[
            HoleFeature(diameter_mm=8.0, area_mm2=50.27),
            HoleFeature(
                type="countersunk",
                diameter_mm=6.0,
                through_diameter_mm=6.0,
                countersink_major_diameter_mm=12.0,
                countersink_depth_mm=2.0,
                area_mm2=28.27,
            ),
        ],
        cutting_outer_perimeter_mm=360.0,
        density_g_cm3=7.85,
        parameters=load_analysis_config(),
    )

    assert result.status == "exact"
    assert result.net_developed_area_mm2 == pytest.approx(7621.46, abs=0.02)
    assert result.opening_area_mm2 == pytest.approx(78.54, abs=0.01)
    assert result.gross_blank_area_mm2 == pytest.approx(7700.0, abs=0.02)


def test_multi_solid_flat_pattern_is_unavailable_even_with_thickness():
    result = _estimate_flat_pattern(
        shape=SimpleNamespace(Volume=6992.92, BoundBox=_bbox(120.0, 40.0, 2.0)),
        thickness_mm=2.0,
        thickness_confidence="high",
        bends=[],
        holes=[],
        cutting_outer_perimeter_mm=200.0,
        density_g_cm3=7.85,
        parameters=load_analysis_config(),
        part_category="multi_solid",
    )

    assert result.status == "unavailable"
    assert result.available is False
    assert result.gross_blank_area_mm2 is None
    assert any("piu solidi" in warning for warning in result.warnings)


def test_flat_pattern_estimates_simple_parallel_bend_blank():
    bend_items = [
        BendFeature(
            radius_mm=2.0,
            length_mm=50.0,
            angle_deg=90.0,
            axis=[0.0, 1.0, 0.0],
            confidence="high",
        )
        for _ in range(2)
    ]
    result = _estimate_flat_pattern(
        shape=SimpleNamespace(Volume=18488.0, BoundBox=_bbox(102.0, 50.0, 51.0)),
        thickness_mm=2.0,
        thickness_confidence="high",
        bends=bend_items,
        holes=[HoleFeature(area_mm2=556.0)],
        cutting_outer_perimeter_mm=284.0,
        density_g_cm3=2.7,
        parameters=load_analysis_config(),
    )

    assert result.status == "estimated"
    assert result.blank_dimensions_mm is not None
    assert result.blank_dimensions_mm.x == 196.0
    assert result.blank_dimensions_mm.y == 50.0
    assert result.outer_perimeter_mm == 492.0
    assert result.total_bend_length_mm == 100.0
    assert result.total_bend_allowance_mm == 8.8
    assert result.blank_weight_kg == 0.053
    assert result.confidence == "medium"


def test_flat_pattern_keeps_complex_non_parallel_part_partial():
    result = _estimate_flat_pattern(
        shape=SimpleNamespace(Volume=20000.0, BoundBox=_bbox(100.0, 80.0, 50.0)),
        thickness_mm=2.0,
        thickness_confidence="high",
        bends=[
            BendFeature(length_mm=50.0, axis=[1.0, 0.0, 0.0]),
            BendFeature(length_mm=50.0, axis=[0.0, 1.0, 0.0]),
        ],
        holes=[],
        cutting_outer_perimeter_mm=300.0,
        density_g_cm3=2.7,
        parameters=load_analysis_config(),
    )

    assert result.available is True
    assert result.status == "partial"
    assert result.blank_dimensions_mm is None
    assert result.net_developed_area_mm2 == 10000.0
    assert result.confidence == "low"
    assert result.warnings


def test_legacy_analysis_config_uses_safe_planar_opening_defaults(tmp_path):
    config_path = tmp_path / "legacy-analysis.json"
    config_path.write_text(
        json.dumps(
            {
                "circular_hole_deduplication": {
                    "center_tolerance_mm": 1.0,
                    "diameter_tolerance_mm": 0.2,
                    "axis_angle_tolerance_deg": 5.0,
                },
                "bend_detection": {
                    "center_tolerance_mm": 3.0,
                    "radius_pair_tolerance_mm": 0.5,
                    "axis_angle_tolerance_deg": 5.0,
                    "min_length_mm": 2.5,
                },
            }
        ),
        encoding="utf-8",
    )

    parameters = load_analysis_config(config_path)

    assert parameters.opening_min_dimension_mm == 0.5
    assert parameters.opening_max_perimeter_mm == 5000.0


def test_analysis_config_rejects_inverted_opening_limits(tmp_path):
    config = {
        "circular_hole_deduplication": {
            "center_tolerance_mm": 1.0,
            "diameter_tolerance_mm": 0.2,
            "axis_angle_tolerance_deg": 5.0,
        },
        "planar_opening_detection": {
            "min_dimension_mm": 20.0,
            "max_dimension_mm": 10.0,
            "min_perimeter_mm": 2.0,
            "max_perimeter_mm": 5000.0,
        },
        "bend_detection": {
            "center_tolerance_mm": 3.0,
            "radius_pair_tolerance_mm": 0.5,
            "axis_angle_tolerance_deg": 5.0,
            "min_length_mm": 2.5,
        },
    }
    config_path = tmp_path / "invalid-analysis.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="dimension limits"):
        load_analysis_config(config_path)


def test_cylindrical_face_angle_uses_parameter_span():
    face = SimpleNamespace(ParameterRange=(0.0, 1.57079632679, 0.0, 20.0))

    assert _cylindrical_face_angle_deg(face) == 90.0


def test_cylindrical_face_angle_rejects_full_cylinder():
    face = SimpleNamespace(ParameterRange=(0.0, 6.28318530718, 0.0, 20.0))

    assert _cylindrical_face_angle_deg(face) is None


def test_hole_to_edge_distance_is_annotated_without_changing_classification():
    outer_wire = _Wire([], length=100.0, bbox=_bbox(40.0, 20.0))
    inner_wire = _Wire(
        [],
        length=18.0,
        bbox=_bbox(6.0, 6.0),
        edge_distance=7.25,
    )
    face = SimpleNamespace(
        Surface=SimpleNamespace(TypeId="Part::GeomPlane", Axis=_vector(z=1.0)),
        Wires=[outer_wire, inner_wire],
    )
    feature = HoleFeature(
        diameter_mm=6.0,
        center=[3.0, 3.0, 0.0],
        axis=[0.0, 0.0, 1.0],
        orientation_axis=[1.0, 0.0, 0.0],
        confidence="high",
    )

    minimum, confidence, measured = _annotate_hole_edge_distances(
        SimpleNamespace(Faces=[face]),
        [feature],
    )

    assert minimum == 7.25
    assert confidence == "high"
    assert measured == 1
    assert feature.edge_distance_mm == 7.25
    assert feature.diameter_mm == 6.0


def test_mass_center_falls_back_to_volume_weighted_solid_centers():
    solid_a = SimpleNamespace(
        CenterOfMass=SimpleNamespace(X=0.0, Y=2.0, Z=4.0),
        Volume=1.0,
    )
    solid_b = SimpleNamespace(
        CenterOfMass=SimpleNamespace(X=10.0, Y=4.0, Z=8.0),
        Volume=3.0,
    )
    shape = SimpleNamespace(Solids=[solid_a, solid_b])

    assert _mass_center_components(shape) == (7.5, 3.5, 7.0)


def test_hole_to_hole_distance_is_measured_between_planar_opening_wires():
    outer_wire = _Wire([], length=100.0, bbox=_bbox(50.0, 30.0))
    first_wire = _Wire(
        [],
        length=18.0,
        bbox=_bbox(6.0, 6.0, x_min=4.0, y_min=4.0),
        edge_distance=12.0,
    )
    second_wire = _Wire(
        [],
        length=18.0,
        bbox=_bbox(6.0, 6.0, x_min=22.0, y_min=4.0),
        edge_distance=12.0,
    )
    face = SimpleNamespace(
        Surface=SimpleNamespace(TypeId="Part::GeomPlane", Axis=_vector(z=1.0)),
        Wires=[outer_wire, first_wire, second_wire],
    )
    features = [
        HoleFeature(
            center=[7.0, 7.0, 0.0],
            axis=[0.0, 0.0, 1.0],
            orientation_axis=[1.0, 0.0, 0.0],
        ),
        HoleFeature(center=[25.0, 7.0, 0.0], axis=[0.0, 0.0, 1.0]),
    ]

    minimum, confidence, measured_pairs = _annotate_hole_to_hole_distances(
        SimpleNamespace(Faces=[face]),
        features,
    )

    assert minimum == 12.0
    assert confidence == "high"
    assert measured_pairs == 1
    assert features[0].nearest_hole_distance_mm == 12.0
    assert features[1].nearest_hole_distance_mm == 12.0


def test_countersink_distances_use_sheet_profiles_not_internal_annulus():
    def circle_wire(radius, x, y, z, distance):
        return _Wire(
            [
                _edge(
                    "Part::GeomCircle",
                    Radius=radius,
                    Center=_vector(x=x, y=y, z=z),
                )
            ],
            length=2.0 * 3.141592653589793 * radius,
            bbox=_bbox(
                radius * 2.0,
                radius * 2.0,
                x_min=x - radius,
                y_min=y - radius,
                z_min=z,
            ),
            edge_distance=distance,
        )

    top_outer = _Wire([], length=360.0, bbox=_bbox(110.0, 70.0))
    bottom_outer = _Wire([], length=360.0, bbox=_bbox(110.0, 70.0))
    top_plain = circle_wire(4.0, 22.0, 35.0, 4.0, 40.0)
    top_major = circle_wire(6.0, 72.0, 35.0, 4.0, 29.0)
    bottom_plain = circle_wire(4.0, 22.0, 35.0, 0.0, 43.0)
    bottom_through = circle_wire(3.0, 72.0, 35.0, 0.0, 32.0)
    transition_major = circle_wire(6.0, 72.0, 35.0, 2.0, 3.0)
    transition_through = circle_wire(3.0, 72.0, 35.0, 2.0, 3.0)
    faces = [
        SimpleNamespace(
            Surface=SimpleNamespace(TypeId="Part::GeomPlane", Axis=_vector(z=1.0)),
            Wires=[top_outer, top_plain, top_major],
        ),
        SimpleNamespace(
            Surface=SimpleNamespace(TypeId="Part::GeomPlane", Axis=_vector(z=1.0)),
            Wires=[bottom_outer, bottom_plain, bottom_through],
        ),
        SimpleNamespace(
            Surface=SimpleNamespace(TypeId="Part::GeomPlane", Axis=_vector(z=1.0)),
            Wires=[transition_major, transition_through],
        ),
    ]
    features = [
        HoleFeature(
            diameter_mm=8.0,
            center=[22.0, 35.0, 2.0],
            axis=[0.0, 0.0, 1.0],
        ),
        HoleFeature(
            type="countersunk",
            diameter_mm=6.0,
            through_diameter_mm=6.0,
            countersink_major_diameter_mm=12.0,
            countersink_depth_mm=2.0,
            center=[72.0, 35.0, 2.0],
            axis=[0.0, 0.0, 1.0],
        ),
    ]
    shape = SimpleNamespace(Faces=faces)

    edge_minimum, edge_confidence, measured = _annotate_hole_edge_distances(
        shape, features
    )
    hole_minimum, hole_confidence, measured_pairs = _annotate_hole_to_hole_distances(
        shape, features
    )

    assert edge_minimum == 29.0
    assert edge_confidence == "high"
    assert measured == 2
    assert features[1].edge_distance_mm == 29.0
    assert hole_minimum == 40.0
    assert hole_confidence == "high"
    assert measured_pairs == 1
