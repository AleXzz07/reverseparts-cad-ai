from __future__ import annotations

import base64
import importlib
import json
import math
import os
import struct
from pathlib import Path
from typing import Any


GLB_MAGIC = 0x46546C67
GLB_VERSION = 2
JSON_CHUNK_TYPE = 0x4E4F534A
BIN_CHUNK_TYPE = 0x004E4942

Vector3 = tuple[float, float, float]
Triangle = tuple[int, int, int]
LineSegment = tuple[Vector3, Vector3]


def _unavailable(message: str) -> dict[str, Any]:
    return {
        "available": False,
        "model_base64": None,
        "format": None,
        "warnings": [f"3D model export failed: {message}"],
    }


def _configure_freecad_path() -> None:
    import sys

    for candidate in (
        "/usr/lib/freecad-python3/lib",
        "/usr/lib/freecad/lib",
        "/usr/lib/freecad-python3",
    ):
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)


def _vector(vertex: Any) -> Vector3:
    # glTF uses Y-up. Preserve the CAD Z-up posture by rotating around X.
    return (float(vertex.x), float(vertex.z), -float(vertex.y))


def _normalize(vector: Vector3) -> Vector3:
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return tuple(value / length for value in vector)


def _dot(left: Vector3, right: Vector3) -> float:
    return sum(a * b for a, b in zip(left, right))


def _normal(a: Vector3, b: Vector3, c: Vector3) -> Vector3:
    ab = tuple(right - left for left, right in zip(a, b))
    ac = tuple(right - left for left, right in zip(a, c))
    return _normalize(
        (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
    )


def _vertex_normals(
    points: list[Vector3],
    facets: list[Triangle],
) -> list[Vector3]:
    """Fallback normals limited to one CAD face.

    Production tessellation calls this separately for each B-Rep face, so a
    fallback can never smooth across a real CAD edge. Keeping this helper
    backwards-compatible also supports the legacy comparison profiler.
    """

    accumulated = [[0.0, 0.0, 0.0] for _ in points]
    for first, second, third in facets:
        face_normal = _normal(points[first], points[second], points[third])
        for index in (first, second, third):
            for axis in range(3):
                accumulated[index][axis] += face_normal[axis]
    return [_normalize(tuple(values)) for values in accumulated]


def _analytic_face_normals(
    face: Any,
    source_vertices: list[Any],
    points: list[Vector3],
    facets: list[Triangle],
) -> list[Vector3]:
    """Return OCC surface normals without crossing B-Rep face boundaries."""

    fallback = _vertex_normals(points, facets)
    if _is_planar_face(face):
        # A mathematical plane has one constant analytic normal. Preserve the
        # face-local winding alignment and fallback for each mesh vertex.
        try:
            u, v = face.Surface.parameter(source_vertices[0])
            planar_normal = _normalize(_vector(face.normalAt(u, v)))
        except Exception:
            return fallback
        return [
            tuple(-value for value in planar_normal)
            if _dot(planar_normal, local) < 0.0 else planar_normal
            for local in fallback
        ]
    normals: list[Vector3] = []
    for index, vertex in enumerate(source_vertices):
        try:
            u, v = face.Surface.parameter(vertex)
            candidate = _normalize(_vector(face.normalAt(u, v)))
            # FreeCAD surface orientation can differ from tessellation winding.
            # Align locally with the face-only fallback without changing the
            # analytic direction along curved surfaces.
            if _dot(candidate, fallback[index]) < 0.0:
                candidate = tuple(-value for value in candidate)
            normals.append(candidate)
        except Exception:
            normals.append(fallback[index])
    return normals


def _is_planar_face(face: Any) -> bool:
    return type(face.Surface).__name__.lower() in {"plane", "geomplane"}


def _tessellate_brep_faces(
    shape: Any,
    deflection: float,
    *,
    curved_refinement: float,
) -> tuple[list[Vector3], list[Triangle], list[Vector3]]:
    """Tessellate per CAD face and retain its analytic normal field.

    Planar faces keep the established deflection. Only curved faces receive a
    bounded refinement, and the caller still enforces the global triangle cap.
    Vertices are intentionally not merged between B-Rep faces: this preserves
    sharp CAD edges and prevents cross-face normal averaging.
    """

    points: list[Vector3] = []
    facets: list[Triangle] = []
    normals: list[Vector3] = []
    for face in shape.Faces:
        face_deflection = deflection
        if not _is_planar_face(face):
            face_deflection *= curved_refinement
        source_vertices, raw_facets = face.tessellate(face_deflection)
        if not isinstance(source_vertices, list):
            source_vertices = list(source_vertices)
        face_points = [_vector(vertex) for vertex in source_vertices]
        face_facets = [
            tuple(int(index) for index in facet)
            for facet in raw_facets
            if len(facet) == 3
        ]
        if not face_points or not face_facets:
            continue
        offset = len(points)
        points.extend(face_points)
        normals.extend(
            _analytic_face_normals(
                face,
                source_vertices,
                face_points,
                face_facets,
            )
        )
        facets.extend(
            (first + offset, second + offset, third + offset)
            for first, second, third in face_facets
        )
    return points, facets, normals


def _face_normal_at_point(face: Any, point: Any) -> Vector3:
    u, v = face.Surface.parameter(point)
    return _normalize(_vector(face.normalAt(u, v)))


def _faces_are_tangent_along_edge(
    edge: Any,
    faces: list[Any],
    *,
    tangent_dot_threshold: float = 0.995,
) -> bool:
    if len(faces) != 2:
        return False
    first_parameter = float(edge.FirstParameter)
    last_parameter = float(edge.LastParameter)
    for fraction in (0.2, 0.5, 0.8):
        try:
            parameter = first_parameter + (last_parameter - first_parameter) * fraction
            point = edge.valueAt(parameter)
            left = _face_normal_at_point(faces[0], point)
            right = _face_normal_at_point(faces[1], point)
        except Exception:
            # If tangency cannot be proven, preserve the technical edge.
            return False
        if abs(_dot(left, right)) < tangent_dot_threshold:
            return False
    return True


def _discretize_brep_edge(edge: Any, deflection: float) -> list[Vector3]:
    curve_name = type(edge.Curve).__name__.lower()
    if "line" in curve_name:
        points = edge.discretize(Number=2)
    else:
        try:
            points = edge.discretize(Deflection=deflection)
        except Exception:
            point_count = max(
                12,
                min(256, math.ceil(float(edge.Length) / max(deflection, 1e-6))),
            )
            points = edge.discretize(Number=point_count)
    return [_vector(point) for point in points]


def _polyline_signature(
    points: list[Vector3],
    tolerance: float,
) -> tuple[tuple[int, int, int], ...]:
    quantized = tuple(
        tuple(round(coordinate / tolerance) for coordinate in point)
        for point in points
    )
    reverse = tuple(reversed(quantized))
    return min(quantized, reverse)


def _extract_brep_edge_segments(
    shape: Any,
    *,
    deflection: float,
    diagonal: float,
) -> list[LineSegment]:
    """Extract visible technical edges from B-Rep topology, never triangles."""

    if not shape.Faces:
        return []
    face_type = type(shape.Faces[0])
    signature_tolerance = max(diagonal * 1e-8, 1e-6)
    seen: set[tuple[tuple[int, int, int], ...]] = set()
    segments: list[LineSegment] = []
    for edge in shape.Edges:
        if bool(getattr(edge, "Degenerated", False)):
            continue
        try:
            adjacent_faces = list(shape.ancestorsOfType(edge, face_type))
        except Exception:
            adjacent_faces = []
        try:
            if any(edge.isSeam(face) for face in adjacent_faces):
                continue
        except Exception:
            # An uncertain seam remains visible rather than being discarded.
            pass
        if _faces_are_tangent_along_edge(edge, adjacent_faces):
            continue
        points = _discretize_brep_edge(edge, deflection)
        if len(points) < 2:
            continue
        signature = _polyline_signature(points, signature_tolerance)
        if signature in seen:
            continue
        seen.add(signature)
        segments.extend(zip(points, points[1:]))
    return segments


def _pad(data: bytes, padding: bytes) -> bytes:
    remainder = len(data) % 4
    if not remainder:
        return data
    return data + padding * (4 - remainder)


def _pack_vec3(points: list[Vector3]) -> bytes:
    """Pack the same little-endian floats into one allocated buffer."""
    data = bytearray(len(points) * 12)
    for offset, point in enumerate(points):
        struct.pack_into("<3f", data, offset * 12, *point)
    return bytes(data)


def _pack_facets(facets: list[Triangle]) -> bytes:
    data = bytearray(len(facets) * 12)
    for offset, facet in enumerate(facets):
        struct.pack_into("<3I", data, offset * 12, *facet)
    return bytes(data)


def _build_glb(
    points: list[Vector3],
    facets: list[Triangle],
    *,
    normals: list[Vector3] | None = None,
    edge_segments: list[LineSegment] | None = None,
) -> bytes:
    active_normals = normals or _vertex_normals(points, facets)
    if len(active_normals) != len(points):
        raise ValueError("GLB normals must match the position count.")
    positions = _pack_vec3(points)
    normal_data = _pack_vec3(active_normals)
    indices = _pack_facets(facets)
    edge_points = [point for segment in (edge_segments or []) for point in segment]
    edge_data = _pack_vec3(edge_points)

    chunks = (positions, normal_data, indices, edge_data)
    offsets: list[int] = []
    binary_parts: list[bytes] = []
    cursor = 0
    for chunk in chunks:
        offsets.append(cursor)
        padded = _pad(chunk, b"\x00")
        binary_parts.append(padded)
        cursor += len(padded)
    binary = b"".join(binary_parts)
    minimum = [min(point[axis] for point in points) for axis in range(3)]
    maximum = [max(point[axis] for point in points) for axis in range(3)]

    nodes: list[dict[str, Any]] = [{"mesh": 0, "name": "STEP surfaces"}]
    materials: list[dict[str, Any]] = [
        {
            "name": "Light gray technical material",
            "pbrMetallicRoughness": {
                "baseColorFactor": [0.72, 0.75, 0.78, 1.0],
                "metallicFactor": 0.12,
                "roughnessFactor": 0.68,
            },
            "doubleSided": True,
        }
    ]
    meshes: list[dict[str, Any]] = [
        {
            "name": "STEP surfaces",
            "primitives": [
                {
                    "attributes": {"POSITION": 0, "NORMAL": 1},
                    "indices": 2,
                    "material": 0,
                }
            ],
        }
    ]
    buffer_views: list[dict[str, Any]] = [
        {
            "buffer": 0,
            "byteOffset": offsets[0],
            "byteLength": len(positions),
            "target": 34962,
        },
        {
            "buffer": 0,
            "byteOffset": offsets[1],
            "byteLength": len(normal_data),
            "target": 34962,
        },
        {
            "buffer": 0,
            "byteOffset": offsets[2],
            "byteLength": len(indices),
            "target": 34963,
        },
    ]
    accessors: list[dict[str, Any]] = [
        {
            "bufferView": 0,
            "componentType": 5126,
            "count": len(points),
            "type": "VEC3",
            "min": minimum,
            "max": maximum,
        },
        {
            "bufferView": 1,
            "componentType": 5126,
            "count": len(active_normals),
            "type": "VEC3",
        },
        {
            "bufferView": 2,
            "componentType": 5125,
            "count": len(facets) * 3,
            "type": "SCALAR",
        },
    ]
    if edge_points:
        materials.append(
            {
                "name": "CAD edge material",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.2, 0.28, 0.36, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
            }
        )
        edge_view = len(buffer_views)
        edge_accessor = len(accessors)
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": offsets[3],
                "byteLength": len(edge_data),
                "target": 34962,
            }
        )
        accessors.append(
            {
                "bufferView": edge_view,
                "componentType": 5126,
                "count": len(edge_points),
                "type": "VEC3",
                "min": [min(point[axis] for point in edge_points) for axis in range(3)],
                "max": [max(point[axis] for point in edge_points) for axis in range(3)],
            }
        )
        nodes.append({"mesh": 1, "name": "CAD B-Rep edges"})
        meshes.append(
            {
                "name": "CAD B-Rep edges",
                "primitives": [
                    {
                        "attributes": {"POSITION": edge_accessor},
                        "material": 1,
                        "mode": 1,
                    }
                ],
            }
        )

    document = {
        "asset": {"version": "2.0", "generator": "REVERSEPARTS FreeCAD exporter"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": buffer_views,
        "accessors": accessors,
    }
    json_chunk = _pad(
        json.dumps(document, separators=(",", ":")).encode("utf-8"),
        b" ",
    )
    total_length = 12 + 8 + len(json_chunk) + 8 + len(binary)
    return b"".join(
        (
            struct.pack("<III", GLB_MAGIC, GLB_VERSION, total_length),
            struct.pack("<II", len(json_chunk), JSON_CHUNK_TYPE),
            json_chunk,
            struct.pack("<II", len(binary), BIN_CHUNK_TYPE),
            binary,
        )
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def export_step_to_glb(step_path: str) -> dict[str, Any]:
    source = Path(step_path)
    if not source.is_file():
        return _unavailable("STEP file does not exist.")

    try:
        _configure_freecad_path()
        importlib.import_module("FreeCAD")
        Part = importlib.import_module("Part")
        shape = Part.Shape()
        shape.read(str(source))
        if shape.isNull():
            raise ValueError("FreeCAD imported an empty shape.")

        bbox = shape.BoundBox
        diagonal = math.sqrt(
            float(bbox.XLength) ** 2
            + float(bbox.YLength) ** 2
            + float(bbox.ZLength) ** 2
        )
        max_triangles = max(
            1000,
            int(os.getenv("VIEWER_MODEL_MAX_TRIANGLES", "120000")),
        )
        tessellation_ratio = max(
            1.0,
            _env_float("VIEWER_MODEL_TESSELLATION_RATIO", 650.0),
        )
        curved_refinement = min(
            1.0,
            max(
                0.4,
                _env_float("VIEWER_MODEL_CURVED_FACE_REFINEMENT", 0.65),
            ),
        )
        deflection = max(diagonal / tessellation_ratio, 0.04)
        points: list[Vector3] = []
        facets: list[Triangle] = []
        normals: list[Vector3] = []
        for _ in range(5):
            points, facets, normals = _tessellate_brep_faces(
                shape,
                deflection,
                curved_refinement=curved_refinement,
            )
            if len(facets) <= max_triangles:
                break
            deflection *= 1.7

        if not points or not facets:
            raise ValueError("FreeCAD tessellation produced no mesh.")
        if len(facets) > max_triangles:
            raise ValueError(
                f"mesh exceeds configured triangle limit ({len(facets)} > "
                f"{max_triangles})."
            )

        edge_deflection = max(deflection * 0.5, 0.02)
        edge_segments = _extract_brep_edge_segments(
            shape,
            deflection=edge_deflection,
            diagonal=diagonal,
        )
        glb = _build_glb(
            points,
            facets,
            normals=normals,
            edge_segments=edge_segments,
        )
        return {
            "available": True,
            "model_base64": base64.b64encode(glb).decode("ascii"),
            "format": "glb",
            "warnings": [],
        }
    except Exception as exc:  # pragma: no cover - depends on FreeCAD host
        return _unavailable(str(exc))
