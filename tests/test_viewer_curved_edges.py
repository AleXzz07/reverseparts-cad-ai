"""Check real circular holes, slotted rims, and spline edges in FreeCAD."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app import model_exporter as current
from scripts import _viewer_before_edge_refinement as before


ROOT = Path(__file__).resolve().parents[1]


def _shape(relative):
    try:
        import FreeCAD  # noqa: F401 - initialize before Part
        import Part
    except ImportError:
        pytest.skip("FreeCAD Docker runtime is required")
    shape = Part.Shape()
    shape.read(str(ROOT / relative))
    return shape


def _circle_sag(points, circle):
    center = current._vector(circle.Center)
    radius = float(circle.Radius)
    return max(radius - math.dist(
        tuple((first[axis] + second[axis]) / 2 for axis in range(3)), center,
    ) for first, second in zip(points, points[1:]))


def _mesh_circle_sag(points, facets, circle):
    center = current._vector(circle.Center)
    axis = current._normalize(current._vector(circle.Axis))
    radius = float(circle.Radius)
    counts = {}
    for a, b, c in facets:
        for x, y in ((a, b), (b, c), (c, a)):
            edge = tuple(sorted((x, y)))
            counts[edge] = counts.get(edge, 0) + 1
    chords = []
    for (a, b), count in counts.items():
        if count != 1 or not all(
            abs(math.dist(points[i], center) - radius) < .002
            and abs(current._dot(tuple(points[i][j] - center[j]
                                       for j in range(3)), axis)) < .002
            for i in (a, b)
        ):
            continue
        midpoint = tuple((points[a][axis] + points[b][axis]) / 2
                         for axis in range(3))
        chords.append(radius - math.dist(midpoint, center))
    assert chords, "No mesh boundary segments on the slotted circle"
    return max(chords)


@pytest.mark.parametrize("path", [
    "tests/dataset/lamiera_piana_test_1/input.stp",
    "tests/dataset/staffa_16_pieghe_stress_test/input.stp",
])
def test_real_circular_hole_brep_edges_have_small_chord_error(path):
    shape = _shape(path)
    curves = [edge for edge in shape.Edges
              if type(edge.Curve).__name__.lower() in {"circle", "geomcircle"}
              and 2 <= float(edge.Curve.Radius) <= 12]
    assert curves, path
    coarse = max(math.sqrt(sum(float(getattr(shape.BoundBox, axis)) ** 2
                               for axis in ("XLength", "YLength", "ZLength"))) / 600, .02)
    improvements = 0
    for edge in curves:
        old = before._discretize_brep_edge(edge, coarse)
        new = current._discretize_brep_edge(edge, .02)
        assert len(new) >= len(old)
        assert _circle_sag(new, edge.Curve) <= .03
        improvements += len(new) > len(old)
    assert improvements > 0


def test_real_complex_slot_mesh_boundary_refines_without_changing_occ_normals():
    shape = _shape("tests/dataset/staffa_16_pieghe_stress_test/input.stp")
    slot_face = next((face for face in shape.Faces for wire in face.Wires
                      if sum(type(edge.Curve).__name__.lower() in
                             {"circle", "geomcircle"} for edge in wire.Edges) == 2
                      and sum(type(edge.Curve).__name__.lower() in
                              {"line", "geomline"} for edge in wire.Edges) == 2), None)
    assert slot_face is not None, "No slotted rim in real STEP"
    slot_circle = next(edge.Curve for wire in slot_face.Wires
                       if sum(type(edge.Curve).__name__.lower() in
                              {"circle", "geomcircle"} for edge in wire.Edges) == 2
                       and sum(type(edge.Curve).__name__.lower() in
                               {"line", "geomline"} for edge in wire.Edges) == 2
                       for edge in wire.Edges
                       if type(edge.Curve).__name__.lower() in {"circle", "geomcircle"})
    baseline = current._tessellate_brep_faces(
        type("Shape", (), {"Faces": [slot_face]})(), .9344,
        curved_refinement=.8,
    )
    refined = current._tessellate_brep_faces(
        type("Shape", (), {"Faces": [slot_face]})(), .9344,
        curved_refinement=.8, boundary_deflection=.02,
        max_triangles=50000,
    )
    # Equal triangle counts are acceptable only if the analytic circular
    # boundary moves closer to its true curve; compare the actual mesh rim.
    assert len(refined[1]) >= len(baseline[1])
    old_sag = _mesh_circle_sag(baseline[0], baseline[1], slot_circle)
    new_sag = _mesh_circle_sag(refined[0], refined[1], slot_circle)
    assert new_sag < .05
    assert new_sag <= old_sag + 1e-6
    if old_sag >= .05:
        assert new_sag < old_sag - 1e-4
    assert all(abs(current._dot(normal, normal) - 1) < 1e-4
               for normal in refined[2])


def test_real_plate_hole_mesh_boundary_has_smaller_sagitta():
    shape = _shape("tests/dataset/lamiera_piana_test_1/input.stp")
    plate = max((face for face in shape.Faces if current._is_planar_face(face)
                 and len(face.Wires) == 5), key=lambda face: face.Area)
    circle = next(edge.Curve for edge in plate.Edges
                  if type(edge.Curve).__name__.lower() in {"circle", "geomcircle"})
    shape_one_face = type("Shape", (), {"Faces": [plate]})()
    before = current._tessellate_brep_faces(shape_one_face, .3,
                                            curved_refinement=.65)
    after = current._tessellate_brep_faces(shape_one_face, .3,
                                           curved_refinement=.65,
                                           boundary_deflection=.02,
                                           max_triangles=50000)
    assert len(after[1]) >= len(before[1])
    old_sag = _mesh_circle_sag(before[0], before[1], circle)
    new_sag = _mesh_circle_sag(after[0], after[1], circle)
    assert new_sag < .05
    assert new_sag <= old_sag + 1e-6
    if old_sag >= .05:
        assert new_sag < old_sag - 1e-4
    assert len(set(after[2])) == 1  # Analytic planar OCC normal at the hole.


def test_real_bspline_edges_are_curved_polylines_not_a_single_chord():
    shape = _shape("tests/dataset/staffa_16_pieghe_stress_test/input.stp")
    curved = []
    for edge in shape.Edges:
        if "bspline" not in type(edge.Curve).__name__.lower():
            continue
        first = current._vector(edge.valueAt(edge.FirstParameter))
        middle = current._vector(edge.valueAt((edge.FirstParameter + edge.LastParameter)/2))
        last = current._vector(edge.valueAt(edge.LastParameter))
        if math.dist(middle, tuple((a + b)/2 for a, b in zip(first, last))) > .1:
            curved.append(edge)
    assert curved, "No non-linear B-spline edge in real STEP"
    edge = curved[0]
    old = before._discretize_brep_edge(edge, .02)
    new = current._discretize_brep_edge(edge, .02)
    assert len(old) == 2
    assert len(new) > 2
    assert math.dist(new[0], new[-1]) > 0
