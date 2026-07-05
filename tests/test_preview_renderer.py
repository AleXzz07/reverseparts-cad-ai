import inspect
import time
from pathlib import Path

import pytest

import app.preview_renderer as preview_renderer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRESS_TEST_FILE = (
    PROJECT_ROOT
    / "tests"
    / "dataset"
    / "staffa_16_pieghe_stress_test"
    / "input.stp"
)


def test_generate_step_preview_fails_safely_without_renderer_dependencies(
    tmp_path,
    monkeypatch,
):
    step_path = tmp_path / "part.step"
    step_path.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="ascii")

    def fail_import(name: str):
        raise ImportError(f"{name} unavailable")

    monkeypatch.setattr(preview_renderer.importlib, "import_module", fail_import)

    result = preview_renderer.generate_step_preview(str(step_path))

    assert result["available"] is False
    assert result["image_png_base64"] is None
    assert result["views"] == []
    assert result["warnings"]
    assert result["warnings"][0].startswith("Preview generation failed:")


def test_generate_step_preview_fails_safely_for_missing_file(tmp_path):
    result = preview_renderer.generate_step_preview(
        str(Path(tmp_path) / "missing.step")
    )

    assert result["available"] is False
    assert result["image_png_base64"] is None
    assert result["views"] == []
    assert result["warnings"] == [
        "Preview generation failed: STEP file does not exist."
    ]


def test_generate_step_previews_keeps_successful_views(tmp_path, monkeypatch):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    monkeypatch.setattr(
        preview_renderer,
        "_load_geometry",
        lambda source: (object(), [], [], 1.0),
    )

    def fake_render(shape, points, facets, diagonal, view_name):
        if view_name == "right":
            raise RuntimeError("right view unavailable")
        return f"encoded-{view_name}"

    monkeypatch.setattr(preview_renderer, "render_named_view", fake_render)

    result = preview_renderer.generate_step_previews(str(step_path))

    assert result["available"] is True
    assert result["image_png_base64"] == "encoded-isometric"
    assert result["partial"] is True
    assert [view["name"] for view in result["views"]] == [
        "isometric",
        "top",
        "front",
    ]
    assert [view["key"] for view in result["views"]] == [
        "isometric",
        "top",
        "front",
    ]
    assert [view["label"] for view in result["views"]] == [
        "Isometrica",
        "Alto",
        "Frontale",
    ]
    assert result["warnings"] == [
        "Preview view 'right' generation failed: right view unavailable"
    ]


def test_generate_step_previews_returns_four_views_when_all_succeed(
    tmp_path,
    monkeypatch,
):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    rendered = []
    imports = []

    def fake_load(source):
        imports.append(source)
        return object(), [], [], 1.0

    monkeypatch.setattr(
        preview_renderer,
        "_load_geometry",
        fake_load,
    )

    def fake_render(shape, points, facets, diagonal, view_name):
        rendered.append(view_name)
        return f"encoded-{view_name}"

    monkeypatch.setattr(preview_renderer, "render_named_view", fake_render)

    result = preview_renderer.generate_step_previews(str(step_path))

    assert result["available"] is True
    assert imports == [step_path]
    assert result["partial"] is False
    assert rendered == ["isometric", "top", "front", "right"]
    assert [view["name"] for view in result["views"]] == rendered
    assert [view["key"] for view in result["views"]] == rendered
    assert [view["label"] for view in result["views"]] == [
        "Isometrica",
        "Alto",
        "Frontale",
        "Destra",
    ]


def test_generate_step_previews_returns_generated_views_after_total_timeout(
    tmp_path,
    monkeypatch,
):
    step_path = tmp_path / "part.step"
    step_path.write_text("STEP", encoding="ascii")
    rendered = []

    monkeypatch.setattr(
        preview_renderer,
        "_load_geometry",
        lambda source: (object(), [], [], 1.0),
    )
    monkeypatch.setattr(preview_renderer, "PREVIEW_TOTAL_TIMEOUT_SEC", 0.01)

    def fake_render(shape, points, facets, diagonal, view_name):
        rendered.append(view_name)
        time.sleep(0.02)
        return f"encoded-{view_name}"

    monkeypatch.setattr(preview_renderer, "render_named_view", fake_render)

    result = preview_renderer.generate_step_previews(str(step_path))

    assert result["available"] is True
    assert result["partial"] is True
    assert rendered == ["isometric"]
    assert [view["name"] for view in result["views"]] == ["isometric"]
    assert "Preview total timeout reached; returning generated views." in result["warnings"]


def test_ultra_light_render_does_not_draw_convex_hull_outline():
    source = inspect.getsource(preview_renderer.render_named_view)

    assert "_convex_hull" not in source


def test_deduplicate_screen_lines_removes_duplicates_and_micro_edges():
    line = [(0.0, 0.0), (20.0, 0.0), (40.0, 0.0)]
    duplicate_reversed = list(reversed(line))
    micro_edge = [(0.0, 0.0), (1.0, 0.0)]

    result = preview_renderer._deduplicate_screen_lines(
        [line, duplicate_reversed, micro_edge]
    )

    assert result == [line]


def test_clean_hlr_reduces_stress_test_edge_noise():
    try:
        shape, points, facets, diagonal = preview_renderer._load_geometry(
            STRESS_TEST_FILE
        )
    except Exception as exc:
        pytest.skip(f"FreeCAD HLR is not available: {exc}")

    basis = preview_renderer._camera_basis("isometric")
    projected = [
        preview_renderer._project(point, basis)
        for point in points
    ]
    transform = preview_renderer._screen_transform(projected)
    lines = preview_renderer._visible_hlr_lines(
        shape,
        preview_renderer.VIEW_DIRECTIONS["isometric"],
        projected,
        max(diagonal / 3000.0, 0.015),
    )
    screen_lines = preview_renderer._deduplicate_screen_lines(
        [
            [
                preview_renderer._to_screen(point, transform)
                for point in line
            ]
            for line in lines
        ]
    )

    signatures = {
        preview_renderer._line_signature(line)
        for line in screen_lines
    }
    assert len(screen_lines) < len(shape.Edges) * 0.75
    assert len(signatures) == len(screen_lines)
    assert all(
        preview_renderer._screen_line_length(line)
        >= 3.0 * preview_renderer.RENDER_SCALE
        for line in screen_lines
    )
