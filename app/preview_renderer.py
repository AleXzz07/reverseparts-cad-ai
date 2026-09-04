from __future__ import annotations

import base64
import importlib
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
PREVIEW_WIDTH_PX = int(os.getenv("PREVIEW_WIDTH_PX", "1600"))
PREVIEW_HEIGHT_PX = int(os.getenv("PREVIEW_HEIGHT_PX", "1200"))
RENDER_SCALE = int(os.getenv("PREVIEW_RENDER_SCALE", "2"))
PREVIEW_RENDER_MODE = os.getenv(
    "PREVIEW_RENDER_MODE",
    "clean",
).strip().lower()
PREVIEW_TOTAL_TIMEOUT_SEC = float(os.getenv("PREVIEW_TOTAL_TIMEOUT_SEC", "30"))
PREVIEW_PER_VIEW_TIMEOUT_SEC = float(os.getenv("PREVIEW_PER_VIEW_TIMEOUT_SEC", "8"))
PRIMARY_VIEW_NAME = "isometric"
VIEW_ORDER = ("isometric", "top", "front", "right")
VIEW_LABELS = {
    "isometric": "Isometrica",
    "top": "Alto",
    "front": "Frontale",
    "right": "Destra",
}
VIEW_DIRECTIONS = {
    "isometric": (1.0, -1.0, 0.85),
    "top": (0.0, 0.0, 1.0),
    "front": (0.0, -1.0, 0.0),
    "right": (1.0, 0.0, 0.0),
}


def _unavailable(message: str) -> dict[str, Any]:
    return {
        "image_png_base64": None,
        "available": False,
        "mode": "failed",
        "partial": False,
        "views": [],
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


def _dot(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cross(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _normalize(
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    length = math.sqrt(_dot(vector, vector))
    if length == 0:
        raise ValueError("Camera vector cannot be zero.")
    return tuple(value / length for value in vector)


def _camera_basis(
    view_name: str,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    camera = _normalize(VIEW_DIRECTIONS[view_name])
    if view_name == "top":
        right = (1.0, 0.0, 0.0)
        up = (0.0, 1.0, 0.0)
    else:
        world_up = (0.0, 0.0, 1.0)
        right = _normalize(_cross(world_up, camera))
        up = _normalize(_cross(camera, right))
    return camera, right, up


def _project(
    point: tuple[float, float, float],
    basis: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ],
) -> tuple[float, float, float]:
    camera, right, up = basis
    return (_dot(point, right), _dot(point, up), _dot(point, camera))


def _normal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    ab = tuple(right - left for left, right in zip(a, b))
    ac = tuple(right - left for left, right in zip(a, c))
    cross = _cross(ab, ac)
    length = math.sqrt(_dot(cross, cross)) or 1.0
    return tuple(value / length for value in cross)


def _shade(normal: tuple[float, float, float]) -> tuple[int, int, int]:
    light = _normalize((-0.35, -0.45, 0.82))
    intensity = max(0.0, _dot(normal, light))
    ambient = 0.72
    value = int(186 + 48 * (ambient + (1 - ambient) * intensity))
    return (min(value, 238), min(value + 2, 242), min(value + 5, 246))


def _screen_transform(
    projected_points: list[tuple[float, float, float]],
) -> tuple[float, float, float]:
    min_x = min(point[0] for point in projected_points)
    max_x = max(point[0] for point in projected_points)
    min_y = min(point[1] for point in projected_points)
    max_y = max(point[1] for point in projected_points)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    margin = 86 * RENDER_SCALE
    width = PREVIEW_WIDTH_PX * RENDER_SCALE
    height = PREVIEW_HEIGHT_PX * RENDER_SCALE
    scale = min((width - 2 * margin) / span_x, (height - 2 * margin) / span_y)
    offset_x = (width - span_x * scale) / 2 - min_x * scale
    offset_y = (height - span_y * scale) / 2 + max_y * scale
    return scale, offset_x, offset_y


def _to_screen(
    point: tuple[float, float, float],
    transform: tuple[float, float, float],
) -> tuple[float, float]:
    scale, offset_x, offset_y = transform
    return (point[0] * scale + offset_x, offset_y - point[1] * scale)


def _discretize_edge(
    edge: Any,
    curved_edge_step: float,
) -> list[tuple[float, float, float]]:
    curve_name = type(edge.Curve).__name__.lower()
    point_count = 2
    if "line" not in curve_name:
        point_count = max(
            48,
            min(
                384,
                math.ceil(float(edge.Length) / curved_edge_step),
            ),
        )
    return [
        _vector(point)
        for point in edge.discretize(Number=point_count)
    ]


def _projection_candidates(
    x: float,
    y: float,
) -> tuple[tuple[float, float], ...]:
    return (
        (x, y),
        (x, -y),
        (-x, y),
        (-x, -y),
        (y, x),
        (y, -x),
        (-y, x),
        (-y, -x),
    )


def _bbox_center_2d(
    points: list[tuple[float, float]],
) -> tuple[float, float]:
    return (
        (min(point[0] for point in points) + max(point[0] for point in points))
        / 2.0,
        (min(point[1] for point in points) + max(point[1] for point in points))
        / 2.0,
    )


def _align_projected_lines(
    lines: list[list[tuple[float, float, float]]],
    target_points: list[tuple[float, float, float]],
) -> list[list[tuple[float, float, float]]]:
    source_points = [
        (point[0], point[1])
        for line in lines
        for point in line
    ]
    target_2d = [(point[0], point[1]) for point in target_points]
    if not source_points or not target_2d:
        return []

    source_center = _bbox_center_2d(source_points)
    target_center = _bbox_center_2d(target_2d)
    target_step = max(1, len(target_2d) // 700)
    sampled_targets = target_2d[::target_step]
    source_step = max(1, len(source_points) // 120)
    sampled_sources = source_points[::source_step]

    best_index = 0
    best_score = math.inf
    for candidate_index in range(8):
        score = 0.0
        for source_x, source_y in sampled_sources:
            transformed = _projection_candidates(
                source_x - source_center[0],
                source_y - source_center[1],
            )[candidate_index]
            mapped = (
                transformed[0] + target_center[0],
                transformed[1] + target_center[1],
            )
            score += min(
                (mapped[0] - target[0]) ** 2
                + (mapped[1] - target[1]) ** 2
                for target in sampled_targets
            )
        if score < best_score:
            best_score = score
            best_index = candidate_index

    aligned = []
    for line in lines:
        aligned_line = []
        for source_x, source_y, _ in line:
            transformed = _projection_candidates(
                source_x - source_center[0],
                source_y - source_center[1],
            )[best_index]
            aligned_line.append(
                (
                    transformed[0] + target_center[0],
                    transformed[1] + target_center[1],
                    0.0,
                )
            )
        aligned.append(aligned_line)
    return aligned


def _screen_line_length(
    line: list[tuple[float, float]],
) -> float:
    return sum(
        math.dist(left, right)
        for left, right in zip(line, line[1:])
    )


def _line_signature(
    line: list[tuple[float, float]],
    tolerance_px: float = 1.5,
) -> tuple[tuple[int, int], ...]:
    if len(line) <= 5:
        samples = line
    else:
        samples = [
            line[0],
            line[len(line) // 4],
            line[len(line) // 2],
            line[(len(line) * 3) // 4],
            line[-1],
        ]
    signature = tuple(
        (
            round(point[0] / tolerance_px),
            round(point[1] / tolerance_px),
        )
        for point in samples
    )
    reverse = tuple(reversed(signature))
    return min(signature, reverse)


def _deduplicate_screen_lines(
    lines: list[list[tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    unique = []
    signatures = set()
    for line in lines:
        if len(line) < 2 or _screen_line_length(line) < 3.0 * RENDER_SCALE:
            continue
        signature = _line_signature(line)
        if signature in signatures:
            continue
        signatures.add(signature)
        unique.append(line)
    return unique


def _convex_hull(
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    unique_points = sorted(set(points))
    if len(unique_points) <= 1:
        return unique_points

    def cross(
        origin: tuple[float, float],
        left: tuple[float, float],
        right: tuple[float, float],
    ) -> float:
        return (
            (left[0] - origin[0]) * (right[1] - origin[1])
            - (left[1] - origin[1]) * (right[0] - origin[0])
        )

    lower: list[tuple[float, float]] = []
    for point in unique_points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)

    upper: list[tuple[float, float]] = []
    for point in reversed(unique_points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)

    return lower[:-1] + upper[:-1]


def _crop_to_content(image: Any) -> Any:
    Image = importlib.import_module("PIL.Image")
    width, height = image.size
    pixels = image.load()
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    for y in range(height):
        for x in range(width):
            red, green, blue = pixels[x, y][:3]
            if (red, green, blue) != (255, 255, 255):
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
    if max_x < min_x or max_y < min_y:
        return image

    padding = max(16 * RENDER_SCALE, int(min(width, height) * 0.035))
    crop_box = (
        max(0, min_x - padding),
        max(0, min_y - padding),
        min(width, max_x + padding + 1),
        min(height, max_y + padding + 1),
    )
    cropped = image.crop(crop_box)
    target_ratio = width / height
    crop_ratio = cropped.width / max(cropped.height, 1)
    if crop_ratio > target_ratio:
        new_width = cropped.width
        new_height = max(1, round(new_width / target_ratio))
    else:
        new_height = cropped.height
        new_width = max(1, round(new_height * target_ratio))
    canvas = Image.new("RGB", (new_width, new_height), (255, 255, 255))
    canvas.paste(
        cropped,
        ((new_width - cropped.width) // 2, (new_height - cropped.height) // 2),
    )
    return canvas


def _visible_hlr_lines(
    shape: Any,
    direction: tuple[float, float, float],
    target_points: list[tuple[float, float, float]],
    curved_edge_step: float,
) -> list[list[tuple[float, float, float]]]:
    FreeCAD = importlib.import_module("FreeCAD")
    Drawing = importlib.import_module("Drawing")
    projection = Drawing.project(shape, FreeCAD.Vector(*direction))
    visible_shapes = projection[:2]
    raw_lines = [
        _discretize_edge(edge, curved_edge_step)
        for projected_shape in visible_shapes
        for edge in projected_shape.Edges
        if not edge.Degenerated
    ]
    return _align_projected_lines(raw_lines, target_points)


def _edge_is_secondary(shape: Any, edge: Any) -> bool:
    if edge.Degenerated:
        return True
    face_type = type(shape.Faces[0])
    adjacent_faces = shape.ancestorsOfType(edge, face_type)
    if any(edge.isSeam(face) for face in adjacent_faces):
        return True
    if len(adjacent_faces) != 2:
        return False

    midpoint = edge.valueAt(
        (float(edge.FirstParameter) + float(edge.LastParameter)) / 2.0
    )
    normals = []
    for face in adjacent_faces:
        try:
            u, v = face.Surface.parameter(midpoint)
            normal = face.normalAt(u, v)
            normals.append(_normalize(_vector(normal)))
        except Exception:
            return False
    return abs(_dot(normals[0], normals[1])) >= 0.995


def _fallback_topology_lines(
    shape: Any,
    basis: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ],
    curved_edge_step: float,
) -> list[list[tuple[float, float, float]]]:
    return [
        [_project(point, basis) for point in _discretize_edge(edge, curved_edge_step)]
        for edge in shape.Edges
        if not _edge_is_secondary(shape, edge)
    ]


def _load_geometry(
    step_path: Path,
) -> tuple[
    Any,
    list[tuple[float, float, float]],
    list[tuple[int, int, int]],
    float,
]:
    _configure_freecad_path()
    importlib.import_module("FreeCAD")
    Part = importlib.import_module("Part")

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
    deflection = max(diagonal / 1000.0, 0.02)
    vertices, raw_facets = shape.tessellate(deflection)
    points = [_vector(vertex) for vertex in vertices]
    facets = [
        tuple(int(index) for index in facet)
        for facet in raw_facets
        if len(facet) == 3
    ]
    if not points or not facets:
        raise ValueError("FreeCAD tessellation produced no visible geometry.")
    return shape, points, facets, diagonal


def render_named_view(
    shape: Any,
    points: list[tuple[float, float, float]],
    facets: list[tuple[int, int, int]],
    diagonal: float,
    view_name: str,
) -> str:
    if view_name not in VIEW_DIRECTIONS:
        raise ValueError(f"Unknown preview view: {view_name}")

    Image = importlib.import_module("PIL.Image")
    ImageDraw = importlib.import_module("PIL.ImageDraw")
    basis = _camera_basis(view_name)
    projected = [_project(point, basis) for point in points]
    transform = _screen_transform(projected)
    canvas_size = (
        PREVIEW_WIDTH_PX * RENDER_SCALE,
        PREVIEW_HEIGHT_PX * RENDER_SCALE,
    )
    image = Image.new("RGB", canvas_size, (255, 255, 255))
    draw = ImageDraw.Draw(image)

    triangles = []
    for indices in facets:
        triangle = tuple(points[index] for index in indices)
        projected_triangle = tuple(projected[index] for index in indices)
        depth = sum(point[2] for point in projected_triangle) / 3.0
        triangles.append((depth, triangle, projected_triangle))

    for _, triangle, projected_triangle in sorted(
        triangles,
        key=lambda item: item[0],
    ):
        polygon = [_to_screen(point, transform) for point in projected_triangle]
        draw.polygon(polygon, fill=_shade(_normal(*triangle)))

    if PREVIEW_RENDER_MODE != "ultra_light":
        curved_edge_step = (
            max(diagonal / 3000.0, 0.015)
            if PREVIEW_RENDER_MODE == "clean"
            else max(diagonal / 900.0, 0.08)
        )
        if PREVIEW_RENDER_MODE == "clean":
            try:
                projected_lines = _visible_hlr_lines(
                    shape,
                    VIEW_DIRECTIONS[view_name],
                    projected,
                    curved_edge_step,
                )
            except Exception:
                projected_lines = _fallback_topology_lines(
                    shape,
                    basis,
                    curved_edge_step,
                )
        else:
            projected_lines = _fallback_topology_lines(
                shape,
                basis,
                curved_edge_step,
            )

        screen_lines = _deduplicate_screen_lines(
            [
                [_to_screen(point, transform) for point in line]
                for line in projected_lines
            ]
        )
        for line in screen_lines:
            draw.line(
                line,
                fill=(48, 55, 60),
                width=2 * RENDER_SCALE,
                joint="curve",
            )

    image = _crop_to_content(image)
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    image = image.resize(
        (PREVIEW_WIDTH_PX, PREVIEW_HEIGHT_PX),
        resample=resampling,
    )
    output = BytesIO()
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f"reverseparts_preview_{view_name}_",
            suffix=".png",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
        image.save(tmp_path, format="PNG", optimize=True)
        logger.info("[preview] generated image: view=%s path=%s", view_name, tmp_path)
        output.write(tmp_path.read_bytes())
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return base64.b64encode(output.getvalue()).decode("ascii")


def generate_step_previews(
    step_path: str,
    *,
    max_views: int | None = None,
    view_names: list[str] | tuple[str, ...] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    return generate_static_previews(
        step_path,
        max_views=max_views,
        view_names=view_names,
        progress_callback=progress_callback,
    )


def generate_static_previews(
    step_path: str,
    views: list[str] | tuple[str, ...] | None = None,
    mode: str | None = None,
    *,
    max_views: int | None = None,
    view_names: list[str] | tuple[str, ...] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    source = Path(step_path)
    if not source.is_file():
        return _unavailable("STEP file does not exist.")

    total_started = time.perf_counter()
    try:
        import_started = time.perf_counter()
        shape, points, facets, diagonal = _load_geometry(source)
        import_elapsed = time.perf_counter() - import_started
        logger.info("[preview] import step: %.3f sec", import_elapsed)
    except Exception as exc:  # pragma: no cover - depends on host FreeCAD install
        return _unavailable(str(exc))
    setup_started = time.perf_counter()
    setup_elapsed = time.perf_counter() - setup_started
    logger.info("[preview] setup scene: %.3f sec", setup_elapsed)

    requested_views = tuple(view_names or views or VIEW_ORDER)
    rendered_views: list[dict[str, str]] = []
    warnings: list[str] = []
    if max_views is not None and not view_names and not views:
        requested_views = VIEW_ORDER[:max(1, min(int(max_views), len(VIEW_ORDER)))]
    logger.info("[preview] attempted views: %s", ", ".join(requested_views))
    for view_name in requested_views:
        elapsed_before_view = time.perf_counter() - total_started
        if elapsed_before_view >= PREVIEW_TOTAL_TIMEOUT_SEC:
            warnings.append(
                "Preview total timeout reached; returning generated views."
            )
            break
        try:
            render_started = time.perf_counter()
            encoded = render_named_view(
                shape,
                points,
                facets,
                diagonal,
                view_name,
            )
            render_elapsed = time.perf_counter() - render_started
            logger.info("[preview] render %s: %.3f sec", view_name, render_elapsed)
            if render_elapsed > PREVIEW_PER_VIEW_TIMEOUT_SEC:
                warnings.append(
                    f"Preview view '{view_name}' exceeded per-view timeout "
                    f"({render_elapsed:.1f}s > {PREVIEW_PER_VIEW_TIMEOUT_SEC:.1f}s)."
                )
            rendered_views.append(
                {
                    "key": view_name,
                    "name": view_name,
                    "label": VIEW_LABELS.get(view_name, view_name.title()),
                    "image_png_base64": encoded,
                }
            )
        except Exception as exc:
            warnings.append(
                f"Preview view '{view_name}' generation failed: {exc}"
            )
        if progress_callback is not None:
            try:
                progress_callback(
                    _preview_result(rendered_views, requested_views, warnings)
                )
            except Exception as exc:
                logger.warning("[preview] progress checkpoint failed: %s", exc)

    generated_names = [view["name"] for view in rendered_views]
    failed_names = [
        view_name
        for view_name in requested_views
        if view_name not in set(generated_names)
    ]
    logger.info(
        "[preview] generated ok: %s",
        ", ".join(generated_names) if generated_names else "none",
    )
    logger.info(
        "[preview] failed: %s",
        ", ".join(failed_names) if failed_names else "none",
    )
    logger.info(
        "[preview] returned views: %s",
        ", ".join(generated_names) if generated_names else "none",
    )
    logger.info("[preview] total: %.3f sec", time.perf_counter() - total_started)

    return _preview_result(rendered_views, requested_views, warnings)


def _preview_result(
    rendered_views: list[dict[str, str]],
    requested_views: tuple[str, ...],
    warnings: list[str],
) -> dict[str, Any]:
    view_snapshot = [dict(view) for view in rendered_views]
    primary = next(
        (
            view["image_png_base64"]
            for view in view_snapshot
            if view["name"] == PRIMARY_VIEW_NAME
        ),
        view_snapshot[0]["image_png_base64"] if view_snapshot else None,
    )
    if not view_snapshot:
        return {
            "image_png_base64": None,
            "available": False,
            "mode": "failed",
            "partial": False,
            "views": [],
            "warnings": warnings or ["Preview generation failed: no views generated."],
        }
    return {
        "image_png_base64": primary,
        "available": True,
        "partial": len(view_snapshot) < len(requested_views),
        "mode": (
            "ultra_light"
            if PREVIEW_RENDER_MODE == "ultra_light"
            else "light"
            if PREVIEW_RENDER_MODE == "light"
            else "full"
        ),
        "views": view_snapshot,
        "warnings": list(warnings),
    }


def generate_step_preview(step_path: str) -> dict[str, Any]:
    return generate_step_previews(step_path)
