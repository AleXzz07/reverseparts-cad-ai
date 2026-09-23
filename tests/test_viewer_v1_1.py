import hashlib
from pathlib import Path

import pytest

from app import model_exporter as optimized
from scripts import _viewer_v1_baseline as baseline
from scripts import profile_viewer_v1 as profiler


def test_frozen_v1_exporter_has_input_zip_digest():
    assert hashlib.sha256(Path(baseline.__file__).read_bytes()).hexdigest() == (
        "6b2b967204e73bda73dd450690456e1ed9293f893a9fca95bc01db76ecdacc90"
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


def test_planar_occ_normal_once_and_same_geometry_as_frozen_v1():
    old_face = _Face(Plane(), reversed_normal=True)
    new_face = _Face(Plane(), reversed_normal=True)
    old = baseline._tessellate_brep_faces(
        type("Shape", (), {"Faces": [old_face]})(), .1, curved_refinement=.65,
    )
    new = optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [new_face]})(), .1, curved_refinement=.65,
    )
    assert old == new
    assert old_face.normal_calls == 4
    assert new_face.normal_calls == 1
    assert new_face.Surface.parameters == 1


def test_curved_surface_still_uses_occ_for_every_vertex():
    face = _Face(BSplineSurface())
    optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .1, curved_refinement=.65,
    )
    assert face.normal_calls == 4
    assert face.Surface.parameters == 4


def test_planar_normal_failure_uses_face_local_mesh_fallback():
    face = _Face(Plane())
    def fail(_u, _v):
        raise RuntimeError("OCC failure")
    face.normalAt = fail
    points, facets, normals = optimized._tessellate_brep_faces(
        type("Shape", (), {"Faces": [face]})(), .1, curved_refinement=.65,
    )
    assert normals == optimized._vertex_normals(points, facets)


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


def test_profiler_retains_frozen_v1_and_reports_phase_and_buffer_equivalence():
    old_face, new_face = _Face(Plane()), _Face(Plane())

    class Shape:
        Faces = [old_face]
        Edges = []

    shape = Shape()
    old = profiler._v1_export(shape, 10., "normal", lambda _phase: None,
                              module=baseline)
    shape.Faces = [new_face]
    new = profiler._v1_export(shape, 10., "normal", lambda _phase: None,
                              module=optimized)
    for row in (old, new):
        assert set(("tessellation_sec", "occ_normals_sec", "brep_edges_sec",
                    "glb_assembly_sec", "buffer_sha256")) <= row.keys()
        assert row["tessellation_sec"] >= 0
        assert row["occ_normals_sec"] >= 0
    assert old["buffer_sha256"] == new["buffer_sha256"]
    rows = [dict(case=case, mode=mode, status="completed", **value)
            for case, _, _ in profiler.DEFAULT_CASES
            for mode, value in (("v1", old), ("v1_1", new))]
    assert all(item["status"] == "pass" and item["counts_equal"]
               for item in profiler._equivalence(rows))
