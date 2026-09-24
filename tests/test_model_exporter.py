import inspect
import json
import struct
from pathlib import Path

import pytest

import app.model_exporter as model_exporter
import app.model_service as model_service
from app.model_exporter import (
    GLB_MAGIC,
    GLB_VERSION,
    _build_glb,
    _extract_brep_edge_segments,
    _tessellate_brep_faces,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _glb_document(glb):
    json_length, json_type = struct.unpack("<II", glb[12:20])
    return (
        json.loads(glb[20:20 + json_length].decode("utf-8")),
        json_type,
    )


def test_build_glb_creates_valid_container():
    glb = _build_glb(
        [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)],
        [(0, 1, 2)],
    )

    magic, version, total_length = struct.unpack("<III", glb[:12])
    document, json_type = _glb_document(glb)

    assert magic == GLB_MAGIC
    assert version == GLB_VERSION
    assert total_length == len(glb)
    assert json_type == 0x4E4F534A
    assert document["asset"]["version"] == "2.0"
    assert document["meshes"][0]["primitives"][0]["attributes"] == {
        "POSITION": 0,
        "NORMAL": 1,
    }


def test_build_glb_exports_brep_edges_as_line_primitive():
    glb = _build_glb(
        [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 10.0, 0.0)],
        [(0, 1, 2)],
        normals=[(0.0, 0.0, 1.0)] * 3,
        edge_segments=[((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))],
    )

    document, _ = _glb_document(glb)

    assert [node["name"] for node in document["nodes"]] == [
        "STEP surfaces",
        "CAD B-Rep edges",
    ]
    line_primitive = document["meshes"][1]["primitives"][0]
    assert line_primitive["mode"] == 1
    assert "NORMAL" not in line_primitive["attributes"]
    line_accessor = document["accessors"][line_primitive["attributes"]["POSITION"]]
    assert line_accessor["count"] == 2


class _Vector:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z


class Line:
    pass


class BSplineCurve:
    pass


def test_bspline_edges_are_discretized_as_curves_not_two_point_lines():
    class CurvedEdge:
        Curve = BSplineCurve()
        Length = 12.

        def __init__(self):
            self.settings = []

        def discretize(self, **settings):
            self.settings.append(settings)
            if settings == {"Number": 2}:
                return [_Vector(0, 0, 0), _Vector(2, 0, 0)]
            return [_Vector(0, 0, 0), _Vector(1, 1, 0), _Vector(2, 0, 0)]

    edge = CurvedEdge()
    points = model_exporter._discretize_brep_edge(edge, .02)
    assert edge.settings == [{"Deflection": .02}]
    assert len(points) == 3
    assert points[1] != tuple((a + b) / 2 for a, b in zip(points[0], points[-1]))


def test_circle_edges_obey_small_chord_deflection_without_changing_lines():
    import math

    class Circle:
        pass

    class CircularEdge:
        Curve = Circle()
        Length = 2 * math.pi * 5

        def discretize(self, **settings):
            chord_error = settings["Deflection"]
            sides = math.ceil(math.pi / math.acos(1 - chord_error / 5))
            return [_Vector(5 * math.cos(i * 2 * math.pi / sides),
                            5 * math.sin(i * 2 * math.pi / sides), 0)
                    for i in range(sides + 1)]

    points = model_exporter._discretize_brep_edge(CircularEdge(), .02)
    assert len(points) > len(model_exporter._discretize_brep_edge(CircularEdge(), .467))
    midpoint = tuple((a + b) / 2 for a, b in zip(points[0], points[1]))
    assert 5 - math.dist(midpoint, (0, 0, 0)) <= .021


class _Surface:
    def parameter(self, point):
        return (point.x, point.y)


class _Face:
    def __init__(self, normal):
        self.Surface = _Surface()
        self._normal = _Vector(*normal)

    def normalAt(self, _u, _v):
        return self._normal


class _Edge:
    Curve = Line()
    FirstParameter = 0.0
    LastParameter = 1.0

    def __init__(self, start, end, *, degenerated=False, seam=False):
        self._start = _Vector(*start)
        self._end = _Vector(*end)
        self.Degenerated = degenerated
        self._seam = seam
        self.Length = sum(
            (right - left) ** 2
            for left, right in zip(start, end)
        ) ** 0.5

    def valueAt(self, parameter):
        return _Vector(
            self._start.x + (self._end.x - self._start.x) * parameter,
            self._start.y + (self._end.y - self._start.y) * parameter,
            self._start.z + (self._end.z - self._start.z) * parameter,
        )

    def discretize(self, **_kwargs):
        return [self._start, self._end]

    def isSeam(self, _face):
        return self._seam


class _Shape:
    def __init__(self, faces, edge_faces):
        self.Faces = faces
        self.Edges = list(edge_faces)
        self._edge_faces = edge_faces

    def ancestorsOfType(self, edge, _face_type):
        return self._edge_faces[edge]


def test_brep_edge_export_filters_seams_degenerate_tangent_and_duplicates():
    sharp_faces = [_Face((0, 0, 1)), _Face((0, 1, 0))]
    tangent_faces = [_Face((0, 0, 1)), _Face((0, 0, -1))]
    visible = _Edge((0, 0, 0), (10, 0, 0))
    duplicate_reversed = _Edge((10, 0, 0), (0, 0, 0))
    seam = _Edge((0, 1, 0), (10, 1, 0), seam=True)
    degenerate = _Edge((0, 2, 0), (10, 2, 0), degenerated=True)
    tangent = _Edge((0, 3, 0), (10, 3, 0))
    shape = _Shape(
        [*sharp_faces, *tangent_faces],
        {
            visible: sharp_faces,
            duplicate_reversed: sharp_faces,
            seam: sharp_faces,
            degenerate: sharp_faces,
            tangent: tangent_faces,
        },
    )

    segments = _extract_brep_edge_segments(
        shape,
        deflection=0.02,
        diagonal=10.0,
    )

    assert segments == [((0.0, 0.0, -0.0), (10.0, 0.0, -0.0))]


class Plane:
    def parameter(self, point):
        return (point.x, point.y)


class Cylinder:
    def parameter(self, point):
        return (point.x, point.y)


class _TessellatedFace:
    def __init__(self, surface):
        self.Surface = surface
        self.deflections = []

    def tessellate(self, deflection):
        self.deflections.append(deflection)
        return (
            [_Vector(0, 0, 0), _Vector(1, 0, 0), _Vector(0, 1, 0)],
            [(0, 1, 2)],
        )

    def normalAt(self, _u, _v):
        return _Vector(0, 0, 1)


def test_per_face_tessellation_preserves_face_boundaries_and_targets_curves():
    planar = _TessellatedFace(Plane())
    curved = _TessellatedFace(Cylinder())
    shape = type("Shape", (), {"Faces": [planar, curved]})()

    points, facets, normals = _tessellate_brep_faces(
        shape,
        0.1,
        curved_refinement=0.65,
    )

    assert len(points) == 6
    assert facets == [(0, 1, 2), (3, 4, 5)]
    assert len(normals) == 6
    assert planar.deflections == [0.1]
    assert curved.deflections == pytest.approx([0.065])


def test_round_boundary_refinement_is_bounded_and_preserves_occ_normals():
    import math

    class Circle:
        pass

    class Face(_TessellatedFace):
        Edges = [type("Edge", (), {"Curve": Circle()})()]

        def tessellate(self, deflection):
            self.deflections.append(deflection)
            sides = math.ceil(math.pi / math.acos(1 - deflection / 5))
            points = [_Vector(0., 0., 0.)] + [
                _Vector(5 * math.cos(i * 2 * math.pi / sides),
                        5 * math.sin(i * 2 * math.pi / sides), 0.)
                for i in range(sides)]
            facets = [(0, i + 1, (i + 1) % sides + 1)
                      for i in range(sides)]
            return points, facets

    original = Face(Plane())
    shape = type("Shape", (), {"Faces": [original]})()
    baseline = _tessellate_brep_faces(shape, .3, curved_refinement=.65)
    timings = {"tessellation_sec": 0., "occ_normals_sec": 0.}
    bounded = _tessellate_brep_faces(shape, .3, curved_refinement=.65,
                                    boundary_deflection=.02,
                                    max_triangles=len(baseline[1]),
                                    phase_timings=timings)
    assert bounded == baseline
    assert timings["boundary_unrefined_faces"] == 1
    refined = _tessellate_brep_faces(shape, .3, curved_refinement=.65,
                                    boundary_deflection=.02,
                                    max_triangles=1000)
    assert len(refined[1]) > len(baseline[1])
    assert all(normal == baseline[2][0] for normal in refined[2])
    assert refined[0][0] == baseline[0][0]


def test_cached_freecad_mesh_uses_cleaned_copy_for_real_contour_gain():
    import math

    class Circle:
        Radius = 5.
        Center = _Vector(0, 0, 0)
        Axis = _Vector(0, 0, 1)

    class CachedFace(_TessellatedFace):
        Edges = [type("Edge", (), {"Curve": Circle()})()]

        def __init__(self, *, cached=True):
            super().__init__(Plane())
            self.cached = cached
            self.clean_calls = 0

        def tessellate(self, deflection):
            self.deflections.append(deflection)
            sides = 8 if self.cached else max(8, math.ceil(math.pi / math.acos(
                1 - deflection / 5)))
            vertices = [_Vector(0, 0, 0)] + [
                _Vector(5 * math.cos(i * 2 * math.pi / sides),
                        5 * math.sin(i * 2 * math.pi / sides), 0)
                for i in range(sides)]
            return vertices, [(0, i + 1, (i + 1) % sides + 1)
                              for i in range(sides)]

        def cleaned(self):
            self.clean_calls += 1
            return CachedFace(cached=False)

    face = CachedFace()
    target = type("Shape", (), {"Faces": [face]})()
    original = _tessellate_brep_faces(target, .3, curved_refinement=.65)
    improved = _tessellate_brep_faces(target, .3, curved_refinement=.65,
                                     boundary_deflection=.02, max_triangles=100)
    assert len(improved[1]) > len(original[1])
    assert face.clean_calls == 1
    assert face.deflections == [.3, .3, .02]
    assert model_exporter._circle_boundary_sagittas(face, improved[0], improved[1])[0] < .05


def test_equal_triangle_count_can_contain_better_circle_geometry():
    import math

    class Circle:
        Radius = 5.
        Center = _Vector(0, 0, 0)
        Axis = _Vector(0, 0, 1)

    class Face(_TessellatedFace):
        Edges = [type("Edge", (), {"Curve": Circle()})()]

        def tessellate(self, deflection):
            if deflection > .02:
                angles = sorted([i * 2 * math.pi / 16 for i in range(16)] +
                                [i * .0001 for i in range(1, 17)])
            else:
                angles = [i * 2 * math.pi / 32 for i in range(32)]
            vertices = [_Vector(0, 0, 0)] + [
                _Vector(5 * math.cos(a), 5 * math.sin(a), 0) for a in angles
            ]
            return vertices, [(0, i + 1, (i + 1) % 32 + 1)
                              for i in range(32)]

    face = Face(Plane())
    shape = type("Shape", (), {"Faces": [face]})()
    coarse = _tessellate_brep_faces(shape, .3, curved_refinement=.65)
    finer = _tessellate_brep_faces(shape, .3, curved_refinement=.65,
                                  boundary_deflection=.02, max_triangles=32)
    assert len(coarse[1]) == len(finer[1]) == 32
    old_sag = model_exporter._circle_boundary_sagittas(face, coarse[0], coarse[1])[0]
    new_sag = model_exporter._circle_boundary_sagittas(face, finer[0], finer[1])[0]
    assert old_sag > .05 > new_sag
    assert all(normal == (0., 1., -0.) for normal in finer[2])


def test_viewer_progress_does_not_change_glb_geometry_normals_or_edges(tmp_path, monkeypatch):
    step = tmp_path / "part.step"
    step.write_text("fixture", encoding="utf-8")

    class Bounds:
        XLength = YLength = ZLength = 10

    class Shape:
        BoundBox = Bounds()
        Faces = [_TessellatedFace(Plane())]
        Edges = []

        def read(self, _path):
            pass

        def isNull(self):
            return False

    monkeypatch.setattr(model_exporter, "_configure_freecad_path", lambda: None)
    original_import = model_exporter.importlib.import_module
    monkeypatch.setattr(model_exporter.importlib, "import_module",
                        lambda name: (type("Part", (), {"Shape": Shape})
                                      if name == "Part" else None)
                        if name in {"Part", "FreeCAD"} else original_import(name))
    baseline = model_exporter.export_step_to_glb(str(step))
    phases = []
    observed = model_exporter.export_step_to_glb(
        str(step), progress=lambda phase, data: phases.append((phase, dict(data))),
    )
    assert baseline["available"] and baseline == observed
    assert {phase for phase, _ in phases} >= {
        "step_load", "tessellation", "occ_normals", "brep_edges",
        "glb_assembly", "base64_encoding", "export_completed",
    }
    assert phases[-1][1]["occ_normals_sec"] >= 0


def test_real_freecad_box_edges_come_from_brep_not_triangle_diagonals():
    try:
        import Part
    except ImportError:
        pytest.skip("FreeCAD Part is required for the B-Rep viewer regression.")

    shape = Part.makeBox(10.0, 20.0, 30.0)
    segments = _extract_brep_edge_segments(
        shape,
        deflection=0.02,
        diagonal=40.0,
    )

    assert len(shape.Edges) == 12
    assert len(segments) == 12


def test_viewer_pipeline_is_isolated_from_cad_measurement_engine():
    viewer_source = inspect.getsource(model_exporter) + inspect.getsource(model_service)

    assert "cad_analyzer" not in viewer_source
    assert "sheetmetal_unfolder" not in viewer_source
    assert "quote_engine" not in viewer_source


def test_frontend_uses_exported_lines_not_triangle_edges():
    frontend = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    assert "new THREE.EdgesGeometry" not in frontend
    assert "item.isLineSegments" in frontend
    assert "come from FreeCAD B-Rep edges" in frontend
