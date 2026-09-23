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
