import json
from types import SimpleNamespace

import pytest

from app.cad_analyzer import (
    _detect_circular_holes,
    _detect_elongated_holes,
    _detect_polygonal_holes,
    load_analysis_config,
)


def _vector(x=0.0, y=0.0, z=0.0):
    return SimpleNamespace(x=x, y=y, z=z)


def _bbox(x_length, y_length, z_length=0.0):
    return SimpleNamespace(
        XMin=0.0,
        XMax=x_length,
        YMin=0.0,
        YMax=y_length,
        ZMin=0.0,
        ZMax=z_length,
        XLength=x_length,
        YLength=y_length,
        ZLength=z_length,
    )


class _Wire:
    def __init__(self, edges, *, length, bbox, closed=True):
        self.Edges = edges
        self.Length = length
        self.BoundBox = bbox
        self._closed = closed

    def isClosed(self):
        return self._closed


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
