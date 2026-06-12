from pathlib import Path

import app.preview_renderer as preview_renderer


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
    assert result["warnings"]
    assert result["warnings"][0].startswith("Preview generation failed:")


def test_generate_step_preview_fails_safely_for_missing_file(tmp_path):
    result = preview_renderer.generate_step_preview(
        str(Path(tmp_path) / "missing.step")
    )

    assert result["available"] is False
    assert result["image_png_base64"] is None
    assert result["warnings"] == [
        "Preview generation failed: STEP file does not exist."
    ]
