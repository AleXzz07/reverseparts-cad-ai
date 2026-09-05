import json
from types import SimpleNamespace

import pytest

from app.cad_analyzer import (
    _annotate_hole_edge_distances,
    _annotate_hole_to_hole_distances,
    _cylindrical_face_angle_deg,
    _detect_circular_holes,
    _detect_elongated_holes,
    _detect_polygonal_holes,
    _mass_center_components,
    _planar_wire_area,
    load_analysis_config,
)
from app.schemas import HoleFeature


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


def _edge(type_id, **curve_values):
    return SimpleNamespace(
        Curve=SimpleNamespace(TypeId=type_id, **curve_values)
    )


def _planar_shape(inner_wire):
    outer_wire = _Wire([], length=0.0, bbox=_bbox(0.0, 0.0), closed=False)
    face = SimpleNamespace(
        Surface=SimpleNamespace(TypeId="Part::GeomPlane", Axis=_vector(z=1.0)),
        Wires=[outer_wire, inner_wire],
    )
    return SimpleNamespace(Faces=[face], Edges=[])


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
    arcs = [
        _edge("Part::GeomCircle", Radius=3.0, Center=_vector()),
        _edge("Part::GeomCircle", Radius=3.0, Center=_vector(x=30.0)),
    ]
    lines = [
        _edge("Part::GeomLine", Direction=_vector(x=1.0)),
        _edge("Part::GeomLine", Direction=_vector(x=-1.0)),
    ]
    inner_wire = _Wire(
        [*arcs, *lines],
        length=100.0,
        bbox=_bbox(36.0, 6.0),
    )

    holes = _detect_elongated_holes(
        _planar_shape(inner_wire), load_analysis_config()
    )

    assert len(holes) == 1
    assert holes[0].length_mm == 100.0
    assert holes[0].overall_length_mm == 36.0
    assert holes[0].straight_length_mm == 30.0
    assert holes[0].end_radius_mm == 3.0
    assert holes[0].perimeter_mm == 100.0
    assert holes[0].area_mm2 == 208.27
    assert holes[0].width_mm == 6.0


def test_planar_polygon_is_not_limited_to_old_20_35_mm_perimeter():
    inner_wire = _Wire(
        [_edge("Part::GeomLine") for _ in range(4)],
        length=80.0,
        bbox=_bbox(30.0, 10.0),
    )

    holes = _detect_polygonal_holes(
        _planar_shape(inner_wire), load_analysis_config()
    )

    assert len(holes) == 1
    assert holes[0].num_sides == 4
    assert holes[0].max_dimension_mm == 80.0


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
        HoleFeature(center=[7.0, 7.0, 0.0], axis=[0.0, 0.0, 1.0]),
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
