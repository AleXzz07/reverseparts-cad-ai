"""Exercise OCC UV reuse, unchanged geometry, and safe projection fallback."""

import math
from pathlib import Path

import pytest

from app import model_exporter as exporter
from scripts import _viewer_normals_before_optimization as previous
from scripts.compare_viewer_normals_glb import _buffers


class Vector:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class BSplineSurface:
    def __init__(self):
        self.projections = 0

    def parameter(self, point):
        self.projections += 1
        return point.x, point.y


class Face:
    def __init__(self, uv=None):
        self.Surface = BSplineSurface()
        self.uv = uv if uv is not None else [(0., 0.), (1., 0.),
                                              (1., 1.), (0., 1.)]
        self.normal_calls = 0

    def tessellate(self, _deflection):
        return ([Vector(0, 0, 0), Vector(1, 0, 0), Vector(1, 1, 0),
                 Vector(0, 1, 0)], [(0, 1, 2), (0, 2, 3)])

    def getUVNodes(self):
        return self.uv

    def valueAt(self, u, v):
        return Vector(u, v, 0)

    def normalAt(self, u, v):
        self.normal_calls += 1
        return Vector(u * 0.01, v * 0.01, 1)


def _run(module, face):
    return module._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .3, curved_refinement=.8,
    )


def test_matching_mesh_uvs_skip_projection_keep_occ_normals_and_glb_geometry(monkeypatch):
    old_face, new_face = Face(), Face()
    old = _run(previous, old_face)

    def forbidden_mesh_normals(*_args):
        raise AssertionError("No mesh fallback when every OCC normal succeeds")

    monkeypatch.setattr(exporter, "_vertex_normals", forbidden_mesh_normals)
    new = _run(exporter, new_face)
    assert old == new
    assert new_face.Surface.projections == 0
    assert new_face.normal_calls == 4
    assert previous._build_glb(*old[:2], normals=old[2]) == exporter._build_glb(
        *new[:2], normals=new[2],
    )


def test_permuted_uv_nodes_match_their_mesh_vertices_not_their_list_indices():
    face = Face(uv=[(1., 1.), (0., 1.), (0., 0.), (1., 0.)])
    previous_result = _run(previous, Face())
    current_result = _run(exporter, face)
    assert current_result == previous_result
    assert face.Surface.projections == 0


def test_multiple_nodes_in_search_radius_can_still_be_unambiguously_matched():
    face = Face(uv=[(.01, 0.), (0., 1.), (1., 1.), (0., 0.)])
    face.tessellate = lambda _deflection: (
        [Vector(0., 0., 0.), Vector(.01, 0., 0.),
         Vector(1., 1., 0.), Vector(0., 1., 0.)],
        [(0, 1, 2), (0, 2, 3)],
    )
    result = _run(exporter, face)
    assert len(result[2]) == 4
    assert face.Surface.projections == 0


def test_coincident_vertices_use_projected_occ_normals_individually():
    face = Face(uv=[(1., 1.), (0., 1.), (0., 0.), (0., 0.), (1., 0.)])
    face.tessellate = lambda _deflection: (
        [Vector(0., 0., 0.), Vector(0., 0., 0.), Vector(1., 0., 0.),
         Vector(1., 1., 0.), Vector(0., 1., 0.)],
        [(0, 2, 3), (1, 3, 4)],
    )
    points, _, normals = _run(exporter, face)
    assert len(points) == len(normals) == 5
    assert face.Surface.projections == 2
    assert normals[0] == normals[1]


def test_periodic_uv_seam_with_coincident_positions_uses_occ_projection():
    face = Face(uv=[(0., 0.), (2 * math.pi, 0.), (1., 0.),
                   (1., 1.), (0., 1.)])
    face.valueAt = lambda u, v: Vector(u % (2 * math.pi), v, 0.)
    face.tessellate = lambda _deflection: (
        [Vector(0., 0., 0.), Vector(0., 0., 0.), Vector(1., 0., 0.),
         Vector(1., 1., 0.), Vector(0., 1., 0.)],
        [(0, 2, 3), (1, 3, 4)],
    )
    _run(exporter, face)
    assert face.Surface.projections == 2


def test_near_coincident_distinct_vertices_are_not_guessed():
    face = Face(uv=[(0., 0.), (.0000002, 0.), (1., 1.), (0., 1.)])
    face.tessellate = lambda _deflection: (
        [Vector(0., 0., 0.), Vector(.0000002, 0., 0.),
         Vector(1., 1., 0.), Vector(0., 1., 0.)],
        [(0, 1, 2), (0, 2, 3)],
    )
    _run(exporter, face)
    assert face.Surface.projections == 2


def test_face_metrics_include_association_time_and_partial_fallback():
    face = Face(uv=[(1., 1.), (0., 1.), (0., 0.), (0., 0.), (1., 0.)])
    face.tessellate = lambda _deflection: (
        [Vector(0., 0., 0.), Vector(0., 0., 0.), Vector(1., 0., 0.),
         Vector(1., 1., 0.), Vector(0., 1., 0.)],
        [(0, 2, 3), (1, 3, 4)],
    )
    timings = {"tessellation_sec": 0., "occ_normals_sec": 0.}
    exporter._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .3,
        curved_refinement=.8, phase_timings=timings,
    )
    assert timings["uv_reused_face_count"] == 1
    assert timings["uv_reused_vertices"] == 3
    assert timings["uv_fallback_vertices"] == 2
    assert 0 <= timings["uv_association_sec"] <= timings["occ_normals_sec"]


def test_stale_uv_nodes_revert_entire_face_to_projection():
    face = Face(uv=[(4., 4.)] * 4)
    before = _run(previous, Face())
    after = _run(exporter, face)
    assert after == before
    assert face.Surface.projections == 4
    assert face.normal_calls == 4


def test_failed_occ_normal_still_uses_face_local_mesh_fallback():
    face = Face()
    original = face.normalAt

    def partial_failure(u, v):
        if u == 1. and v == 0.:
            raise ValueError("undefined OCC normal")
        return original(u, v)

    face.normalAt = partial_failure
    previous_face = Face()
    previous_face.normalAt = partial_failure
    assert _run(exporter, face) == _run(previous, previous_face)
    assert face.Surface.projections == 1


def test_singular_mesh_uv_retries_projected_occ_normal():
    face = Face(uv=[(.001, 0.), (1., 0.), (1., 1.), (0., 1.)])
    original = face.normalAt

    def singular_at_mesh_uv(u, v):
        if u == .001 and v == 0.:
            raise ValueError("undefined at mesh UV")
        return original(u, v)

    face.normalAt = singular_at_mesh_uv
    assert _run(exporter, face) == _run(previous, Face())
    assert face.Surface.projections == 1


def test_missing_uv_api_preserves_previous_path():
    face = Face()
    face.getUVNodes = None
    assert _run(exporter, face) == _run(previous, Face())
    assert face.Surface.projections == 4


def test_distinct_valid_occ_uv_normal_bytes_leave_positions_indices_edges_untouched():
    old = _run(previous, Face())
    new = _run(exporter, Face(uv=[(.001, 0.), (1., 0.),
                                   (1., 1.), (0., 1.)]))
    assert old[0:2] == new[0:2]
    edges = [(old[0][0], old[0][1])]
    a = _buffers(previous._build_glb(*old[:2], normals=old[2], edge_segments=edges))
    b = _buffers(exporter._build_glb(*new[:2], normals=new[2], edge_segments=edges))
    assert a["positions"] == b["positions"]
    assert a["indices"] == b["indices"]
    assert a["brep_edges"] == b["brep_edges"]
    assert a["normals"] != b["normals"]


def test_real_complex_step_uv_normals_remain_analytic_and_aligned():
    try:
        import FreeCAD  # noqa: F401 - must initialize before Part
        import Part
    except ImportError:
        pytest.skip("Requires the FreeCAD Docker runtime")
    path = (Path(__file__).resolve().parent / "dataset" /
            "staffa_16_pieghe_stress_test" / "input.stp")
    shape = Part.Shape()
    shape.read(str(path))
    bbox = shape.BoundBox
    diagonal = math.sqrt(sum(v * v for v in
                             (bbox.XLength, bbox.YLength, bbox.ZLength)))
    deflection = max(diagonal / 300., .04) * .8
    expected_faces = (79, 97, 123, 177)
    measurements = {}
    for face_index in expected_faces:
        face = shape.Faces[face_index - 1]
        assert "bspline" in type(face.Surface).__name__.lower()
        vertices, facets = face.tessellate(deflection)
        assert vertices and facets
        points = [exporter._vector(vertex) for vertex in vertices]
        uv_nodes = exporter._verified_tessellation_uv_nodes(face, points, deflection)
        assert uv_nodes is not None, face_index
        reused = sum(uv is not None for uv in uv_nodes)
        measurements[face_index] = {"reused": reused, "fallback": len(vertices) - reused}
        assert reused > 0, measurements
        baseline_normals = previous._analytic_face_normals(face, vertices, points, facets)
        actual_normals = exporter._analytic_face_normals(
            face, vertices, points, facets, uv_nodes=uv_nodes,
        )
        max_angle = 0.
        for index, vertex in enumerate(vertices):
            if uv_nodes[index] is None:
                assert actual_normals[index] == baseline_normals[index]
                continue
            u, v = uv_nodes[index]
            old_u, old_v = face.Surface.parameter(vertex)
            baseline = exporter._normalize(exporter._vector(face.normalAt(old_u, old_v)))
            candidate = exporter._normalize(exporter._vector(face.normalAt(u, v)))
            assert actual_normals[index] == candidate
            max_angle = max(max_angle, math.degrees(math.acos(
                max(-1., min(1., exporter._dot(baseline, candidate))))))
        assert max_angle < .2, (face_index, measurements, max_angle)
        assert exporter._orient_facets_to_normals(points, facets, actual_normals) == (
            previous._orient_facets_to_normals(points, facets, baseline_normals)
        ), (face_index, "triangle orientation changed")
    assert measurements[97]["reused"] == 65, measurements
    assert measurements[177]["reused"] == 4, measurements
