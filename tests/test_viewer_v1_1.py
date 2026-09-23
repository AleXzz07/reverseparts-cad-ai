import hashlib
import math
from pathlib import Path

import pytest

from app import model_exporter as optimized
from scripts import _viewer_v1_baseline as baseline
from scripts import _viewer_v1_1_before_shading as published
from scripts import profile_viewer_v1 as profiler


def test_frozen_v1_exporter_has_input_zip_digest():
    assert hashlib.sha256(Path(baseline.__file__).read_bytes()).hexdigest() == (
        "6b2b967204e73bda73dd450690456e1ed9293f893a9fca95bc01db76ecdacc90"
    )


def test_published_v1_1_exporter_has_input_zip_digest():
    assert hashlib.sha256(Path(published.__file__).read_bytes()).hexdigest() == (
        "3d47cdb2dd0f69d52c9137768ac5c1ebe98bc4ce59ae66c1522b71331e344b47"
    )


class _Vector:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class Plane:
    def __init__(self):
        self.parameters = 0

    def parameter(self, vertex):
        self.parameters += 1
        return vertex.x, vertex.y


class BSplineSurface(Plane):
    pass


class Cylinder(Plane):
    pass


class _Face:
    def __init__(self, surface, *, reversed_normal=False):
        self.Surface = surface
        self.normal_calls = 0
        self.reversed_normal = reversed_normal

    def tessellate(self, _deflection):
        return (
            [_Vector(0, 0, 0), _Vector(1, 0, 0),
             _Vector(1, 1, 0), _Vector(0, 1, 0)],
            [(0, 1, 2), (0, 2, 3)],
        )

    def normalAt(self, _u, _v):
        self.normal_calls += 1
        return _Vector(0, 0, -1 if self.reversed_normal else 1)


def test_planar_occ_normal_once_and_same_triangles_as_published_v1_1():
    old_face = _Face(Plane(), reversed_normal=True)
    new_face = _Face(Plane(), reversed_normal=True)
    old = published._tessellate_brep_faces(
        type("Shape", (), {"Faces": [old_face]})(), .1, curved_refinement=.65,
    )
    new = optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [new_face]})(), .1, curved_refinement=.65,
    )
    assert old[0] == new[0]
    assert [sorted(triangle) for triangle in old[1]] == [
        sorted(triangle) for triangle in new[1]
    ]
    assert len(set(new[2])) == 1
    assert new[2][0] == (0.0, -1.0, -0.0)
    assert old_face.normal_calls == 1
    assert new_face.normal_calls == 1
    assert new_face.Surface.parameters == 1


def test_curved_surface_still_uses_occ_for_every_vertex():
    face = _Face(BSplineSurface())
    optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .1, curved_refinement=.65,
    )
    assert face.normal_calls == 4
    assert face.Surface.parameters == 4


def test_nonplanar_surface_type_can_have_uniform_occ_normals():
    face = _Face(BSplineSurface())
    _, _, normals = optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .1, curved_refinement=.65,
    )
    assert len(set(normals)) == 1
    assert face.normal_calls == 4


def test_curved_cylinder_occ_normal_field_is_preserved_per_vertex():
    face = _Face(Cylinder())
    face.normalAt = lambda u, v: _Vector(u, v, 1)
    vertices = face.tessellate(.1)[0]
    expected = [optimized._normalize(optimized._vector(face.normalAt(
        *face.Surface.parameter(vertex)))) for vertex in vertices]
    _, _, actual = optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .1, curved_refinement=.65,
    )
    assert actual == expected
    assert len(set(actual)) == len(vertices)


def test_planar_normal_failure_uses_face_local_mesh_fallback():
    face = _Face(Plane())
    def fail(_u, _v):
        raise RuntimeError("OCC failure")
    face.normalAt = fail
    points, facets, normals = optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .1, curved_refinement=.65,
    )
    assert len(set(normals)) == 1
    assert all(optimized._dot(optimized._normal(*(points[i] for i in facet)),
                              normals[0]) > 0 for facet in facets)


def test_mixed_winding_near_hole_does_not_make_planar_shading_triangular():
    face = _Face(Plane())
    def tessellate(_deflection):
        return (
            [_Vector(0, 0, 0), _Vector(4, 0, 0),
             _Vector(4, 4, 0), _Vector(0, 4, 0),
             _Vector(1, 1, 0), _Vector(3, 1, 0),
             _Vector(3, 3, 0), _Vector(1, 3, 0)],
            [(0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)][::-1],
        )
    face.tessellate = tessellate
    points, facets, normals = optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .1, curved_refinement=.65,
    )
    assert face.normal_calls == 1
    assert len(set(normals)) == 1
    assert all(optimized._dot(optimized._normal(*(points[i] for i in facet)),
                              normals[0]) > 0 for facet in facets)
    assert len(facets) == 8


def test_adjacent_sharp_faces_have_separate_positions_and_normals():
    first = _Face(Plane())
    second = _Face(Plane())
    second.normalAt = lambda _u, _v: _Vector(0, 1, 0)
    second.tessellate = lambda _deflection: (
        [_Vector(0, 0, 0), _Vector(1, 0, 0), _Vector(0, 0, 1)],
        [(0, 1, 2)],
    )
    points, facets, normals = optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [first, second]})(),
        .1, curved_refinement=.65,
    )
    assert facets[-1] == (4, 6, 5) or facets[-1] == (4, 5, 6)
    assert points[0] == points[4]
    assert normals[0] != normals[4]


@pytest.mark.parametrize("with_edges", [False, True])
def test_glb_binary_buffer_optimization_is_byte_exact(with_edges):
    points = [(0., 0., 0.), (1.2, 0., 0.), (1.2, 3., 0.), (0., 3., 0.)]
    facets = [(0, 1, 2), (0, 2, 3)]
    normals = [(0., 0., 1.)] * 4
    edges = [(points[0], points[1]), (points[1], points[2])] if with_edges else []
    kwargs = {"normals": normals, "edge_segments": edges}
    old = baseline._build_glb(points, facets, **kwargs)
    new = optimized._build_glb(points, facets, **kwargs)
    assert old == new
    assert profiler._glb_buffer_hashes(old) == profiler._glb_buffer_hashes(new)


def test_profiler_retains_frozen_v1_and_reports_intentional_normal_changes():
    old_face, new_face = (_Face(Plane(), reversed_normal=True),
                          _Face(Plane(), reversed_normal=True))

    class Shape:
        Faces = [old_face]
        Edges = []

    shape = Shape()
    old = profiler._v1_export(shape, 10., "normal", lambda _phase: None,
                              module=published)
    shape.Faces = [new_face]
    new = profiler._v1_export(shape, 10., "normal", lambda _phase: None,
                              module=optimized)
    for row in (old, new):
        assert set(("tessellation_sec", "occ_normals_sec", "brep_edges_sec",
                    "glb_assembly_sec", "buffer_sha256")) <= row.keys()
        assert row["tessellation_sec"] >= 0
        assert row["occ_normals_sec"] >= 0
    assert old["buffer_sha256"]["positions"] == new["buffer_sha256"]["positions"]
    assert (old["buffer_sha256"]["undirected_triangles"] ==
            new["buffer_sha256"]["undirected_triangles"])
    assert old["buffer_sha256"]["normals"] != new["buffer_sha256"]["normals"]
    rows = [dict(case=case, mode=mode, status="completed", **value)
            for case, _, _ in profiler.DEFAULT_CASES
            for mode, value in (("v1_1", old), ("shading_fix", new))]
    assert all(item["status"] == "pass" and item["counts_equal"]
               for item in profiler._equivalence(rows))


def test_real_step_plate_holes_fillet_and_complex_shape_keep_mesh_geometry():
    try:
        import FreeCAD  # noqa: F401 - FreeCAD must load before Part
        import Part
    except ImportError:
        pytest.skip("Requires the FreeCAD Docker runtime")

    root = Path(__file__).resolve().parents[1]
    cases = (
        "tests/dataset/lamiera_piana_test_1/input.stp",
        "tests/test_files/STAFFA TEST 1.stp",
        "tests/dataset/staffa_16_pieghe_stress_test/input.stp",
    )
    for relative in cases:
        shape = Part.Shape()
        shape.read(str(root / relative))
        old = published._tessellate_brep_faces(shape, .3, curved_refinement=.65)
        new = optimized._tessellate_brep_faces(shape, .3, curved_refinement=.65)
        assert new[0] == old[0]
        assert [sorted(f) for f in new[1]] == [sorted(f) for f in old[1]]
        assert len(new[2]) == len(new[0])
        assert all(abs(optimized._dot(n, n) - 1) < 1e-4 for n in new[2])
        if "lamiera_piana" in relative:
            # The large planar face contains the four holes. Its OCC normal
            # must be identical for every position, despite fan triangles.
            top = max((f for f in shape.Faces if optimized._is_planar_face(f)),
                      key=lambda f: float(f.Area))
            assert len(top.Wires) == 5
            pts, facets, normals = optimized._tessellate_brep_faces(
                type("Shape", (), {"Faces": [top]})(), .3,
                curved_refinement=.65,
            )
            assert len(set(normals)) == 1
            assert all(optimized._dot(
                optimized._normal(*(pts[i] for i in facet)), normals[0],
            ) > .99 for facet in facets)
        # A B-spline can describe a mathematically almost planar patch; the
        # largest non-Plane face need not exhibit visible curvature. Select a
        # cylinder whose OCC normals actually vary at tessellated positions.
        cylinders = sorted((f for f in shape.Faces
                            if type(f.Surface).__name__.lower() in
                            {"cylinder", "geomcylinder"}),
                           key=lambda f: float(f.Area), reverse=True)
        assert cylinders, relative
        for curved in cylinders:
            source_vertices, _ = curved.tessellate(.3 * .65)
            if len(source_vertices) < 3:
                continue
            cad_normals = []
            for vertex in source_vertices:
                u, v = curved.Surface.parameter(vertex)
                cad_normals.append(optimized._normalize(
                    optimized._vector(curved.normalAt(u, v)),
                ))
            # Five degrees is well above numerical normal noise or a narrow
            # trimmed patch, and well below the sweep of an actual bend/hole.
            if min(optimized._dot(cad_normals[0], normal)
                   for normal in cad_normals) < math.cos(math.radians(5)):
                break
        else:
            pytest.fail(f"No cylinder with >=5 degrees sampled normal sweep: {relative}")

        curved_points, _, curved_normals = optimized._tessellate_brep_faces(
            type("Shape", (), {"Faces": [curved]})(), .3,
            curved_refinement=.65,
        )
        assert len(curved_points) == len(source_vertices)
        assert min(optimized._dot(curved_normals[0], normal)
                   for normal in curved_normals) < math.cos(math.radians(5))
        # The exporter must preserve the OCC normal at every vertex on the
        # selected face, not merely produce multiple arbitrary directions.
        assert all(optimized._dot(actual, expected) > math.cos(math.radians(.1))
                   for actual, expected in zip(curved_normals, cad_normals))
