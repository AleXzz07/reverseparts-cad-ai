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


def _vector(vertex: Any) -> tuple[float, float, float]:
    # glTF uses Y-up. Preserve the CAD Z-up posture by rotating around X.
    return (float(vertex.x), float(vertex.z), -float(vertex.y))


def _normal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    ab = tuple(right - left for left, right in zip(a, b))
    ac = tuple(right - left for left, right in zip(a, c))
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    length = math.sqrt(sum(value * value for value in cross)) or 1.0
    return tuple(value / length for value in cross)


def _vertex_normals(
    points: list[tuple[float, float, float]],
    facets: list[tuple[int, int, int]],
) -> list[tuple[float, float, float]]:
    accumulated = [[0.0, 0.0, 0.0] for _ in points]
    for first, second, third in facets:
        face_normal = _normal(points[first], points[second], points[third])
        for index in (first, second, third):
            for axis in range(3):
                accumulated[index][axis] += face_normal[axis]

    normals = []
    for values in accumulated:
        length = math.sqrt(sum(value * value for value in values)) or 1.0
        normals.append(tuple(value / length for value in values))
    return normals


def _pad(data: bytes, padding: bytes) -> bytes:
    remainder = len(data) % 4
    if not remainder:
        return data
    return data + padding * (4 - remainder)


def _build_glb(
    points: list[tuple[float, float, float]],
    facets: list[tuple[int, int, int]],
) -> bytes:
    normals = _vertex_normals(points, facets)
    positions = b"".join(struct.pack("<3f", *point) for point in points)
    normal_data = b"".join(struct.pack("<3f", *normal) for normal in normals)
    indices = b"".join(
        struct.pack("<I", index)
        for facet in facets
        for index in facet
    )

    position_offset = 0
    normal_offset = len(positions)
    index_offset = normal_offset + len(normal_data)
    binary = _pad(positions + normal_data + indices, b"\x00")
    minimum = [min(point[axis] for point in points) for axis in range(3)]
    maximum = [max(point[axis] for point in points) for axis in range(3)]

    document = {
        "asset": {"version": "2.0", "generator": "REVERSEPARTS FreeCAD exporter"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "STEP part"}],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "NORMAL": 1},
                        "indices": 2,
                        "material": 0,
                    }
                ]
            }
        ],
        "materials": [
            {
                "name": "Light gray technical material",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.72, 0.75, 0.78, 1.0],
                    "metallicFactor": 0.12,
                    "roughnessFactor": 0.68,
                },
                "doubleSided": True,
            }
        ],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {
                "buffer": 0,
                "byteOffset": position_offset,
                "byteLength": len(positions),
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": normal_offset,
                "byteLength": len(normal_data),
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": index_offset,
                "byteLength": len(indices),
                "target": 34963,
            },
        ],
        "accessors": [
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
                "count": len(normals),
                "type": "VEC3",
            },
            {
                "bufferView": 2,
                "componentType": 5125,
                "count": len(facets) * 3,
                "type": "SCALAR",
            },
        ],
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
        deflection = max(
            diagonal / float(os.getenv("VIEWER_MODEL_TESSELLATION_RATIO", "650")),
            0.04,
        )
        vertices: list[Any] = []
        raw_facets: list[Any] = []
        for _ in range(5):
            vertices, raw_facets = shape.tessellate(deflection)
            if len(raw_facets) <= max_triangles:
                break
            deflection *= 1.7

        points = [_vector(vertex) for vertex in vertices]
        facets = [
            tuple(int(index) for index in facet)
            for facet in raw_facets
            if len(facet) == 3
        ]
        if not points or not facets:
            raise ValueError("FreeCAD tessellation produced no mesh.")
        if len(facets) > max_triangles:
            raise ValueError(
                f"mesh exceeds configured triangle limit ({len(facets)} > "
                f"{max_triangles})."
            )

        glb = _build_glb(points, facets)
        return {
            "available": True,
            "model_base64": base64.b64encode(glb).decode("ascii"),
            "format": "glb",
            "warnings": [],
        }
    except Exception as exc:  # pragma: no cover - depends on FreeCAD host
        return _unavailable(str(exc))
