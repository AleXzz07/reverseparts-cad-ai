from __future__ import annotations

import base64
import importlib
import math
import os
from io import BytesIO
from pathlib import Path
from typing import Any


PREVIEW_WIDTH_PX = 1200
PREVIEW_HEIGHT_PX = 900
RENDER_SCALE = 2


def _unavailable(message: str) -> dict[str, Any]:
    return {
        "image_png_base64": None,
        "available": False,
        "warnings": [f"Preview generation failed: {message}"],
    }


def _configure_freecad_path() -> None:
    import sys

    candidates = (
        "/usr/lib/freecad-python3/lib",
        "/usr/lib/freecad/lib",
        "/usr/lib/freecad-python3",
    )
    for candidate in candidates:
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)


def _vector(vertex: Any) -> tuple[float, float, float]:
    return (float(vertex.x), float(vertex.y), float(vertex.z))


def _project(point: tuple[float, float, float]) -> tuple[float, float]:
    # Orthographic isometric projection with the Z axis kept vertical.
    x, y, z = point
    return ((x - y) * math.sqrt(3) / 2, (x + y) * 0.5 - z)


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


def _shade(normal: tuple[float, float, float]) -> tuple[int, int, int]:
    light = (-0.35, -0.45, 0.82)
    intensity = abs(sum(left * right for left, right in zip(normal, light)))
    value = int(184 + 48 * intensity)
    return (value, value + 2, min(255, value + 5))


def _screen_transform(
    projected_points: list[tuple[float, float]],
) -> tuple[float, float, float]:
    min_x = min(point[0] for point in projected_points)
    max_x = max(point[0] for point in projected_points)
    min_y = min(point[1] for point in projected_points)
    max_y = max(point[1] for point in projected_points)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    margin = 70 * RENDER_SCALE
    width = PREVIEW_WIDTH_PX * RENDER_SCALE
    height = PREVIEW_HEIGHT_PX * RENDER_SCALE
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
    offset_x = (width - span_x * scale) / 2 - min_x * scale
    offset_y = (height - span_y * scale) / 2 + max_y * scale
    return scale, offset_x, offset_y


def _to_screen(
    point: tuple[float, float],
    transform: tuple[float, float, float],
) -> tuple[float, float]:
    scale, offset_x, offset_y = transform
    return (point[0] * scale + offset_x, offset_y - point[1] * scale)


def _render_with_freecad_tessellation(step_path: Path) -> bytes:
    _configure_freecad_path()
    importlib.import_module("FreeCAD")
    Part = importlib.import_module("Part")
    Image = importlib.import_module("PIL.Image")
    ImageDraw = importlib.import_module("PIL.ImageDraw")

    shape = Part.Shape()
    shape.read(str(step_path))
    if shape.isNull():
        raise ValueError("FreeCAD imported an empty shape.")

    bbox = shape.BoundBox
    diagonal = math.sqrt(
        float(bbox.XLength) ** 2
        + float(bbox.YLength) ** 2
        + float(bbox.ZLength) ** 2
    )
    deflection = max(diagonal / 450.0, 0.05)
    vertices, facets = shape.tessellate(deflection)
    points = [_vector(vertex) for vertex in vertices]
    if not points or not facets:
        raise ValueError("FreeCAD tessellation produced no visible geometry.")

    projected = [_project(point) for point in points]
    transform = _screen_transform(projected)
    canvas_size = (
        PREVIEW_WIDTH_PX * RENDER_SCALE,
        PREVIEW_HEIGHT_PX * RENDER_SCALE,
    )
    image = Image.new("RGB", canvas_size, (255, 255, 255))
    draw = ImageDraw.Draw(image)

    triangles = []
    for facet in facets:
        indices = tuple(int(index) for index in facet)
        if len(indices) != 3:
            continue
        triangle = tuple(points[index] for index in indices)
        depth = sum(sum(point) for point in triangle) / 3.0
        triangles.append((depth, triangle))

    for _, triangle in sorted(triangles, key=lambda item: item[0]):
        polygon = [_to_screen(_project(point), transform) for point in triangle]
        draw.polygon(polygon, fill=_shade(_normal(*triangle)))

    edge_deflection = max(diagonal / 250.0, 0.1)
    for edge in shape.Edges:
        edge_points = [_vector(point) for point in edge.discretize(Deflection=edge_deflection)]
        if len(edge_points) < 2:
            continue
        line = [_to_screen(_project(point), transform) for point in edge_points]
        draw.line(line, fill=(48, 55, 60), width=2 * RENDER_SCALE, joint="curve")

    resampling = getattr(Image, "Resampling", Image).LANCZOS
    image = image.resize(
        (PREVIEW_WIDTH_PX, PREVIEW_HEIGHT_PX),
        resample=resampling,
    )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def generate_step_preview(step_path: str) -> dict[str, Any]:
    source = Path(step_path)
    if not source.is_file():
        return _unavailable("STEP file does not exist.")

    try:
        image_bytes = _render_with_freecad_tessellation(source)
        if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return _unavailable("Renderer did not produce a valid PNG image.")
        return {
            "image_png_base64": base64.b64encode(image_bytes).decode("ascii"),
            "available": True,
            "warnings": [],
        }
    except Exception as exc:  # pragma: no cover - depends on host FreeCAD install
        return _unavailable(str(exc))
