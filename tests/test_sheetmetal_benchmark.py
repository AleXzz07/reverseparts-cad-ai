import json
import os
from pathlib import Path

import pytest

from app.cad_analyzer import analyze_step_file, get_freecad_status


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = PROJECT_ROOT / "tests" / "dataset" / "sheetmetal_benchmark"
BENCHMARK_DATA = json.loads(
    (PROJECT_ROOT / "tests" / "dataset" / "sheetmetal_benchmark_v1.json").read_text(
        encoding="utf-8"
    )
)
PIECES = {piece["id"]: piece for piece in BENCHMARK_DATA["pieces"]}


def _require_freecad():
    status = get_freecad_status()
    if status.available:
        return
    if os.getenv("REVERSEPARTS_RUNNING_IN_DOCKER") == "1":
        pytest.fail(f"FreeCAD must be available inside Docker: {status.error}")
    pytest.skip(f"FreeCAD is required for sheet-metal benchmark: {status.error}")


def _within_target(actual, expected, percent, absolute):
    return abs(actual - expected) <= max(absolute, abs(expected) * percent / 100.0)


@pytest.mark.parametrize(
    ("case_dir", "piece_id"),
    (
        ("SM01", "SM01_1_piega_90"),
        ("SM02", "SM02_1_piega_60"),
        ("SM03", "SM03_U_2_pieghe_parallele"),
        ("SM05", "SM05_4_pieghe_parallele"),
    ),
)
def test_parallel_sheetmetal_unfold_benchmark(case_dir, piece_id):
    _require_freecad()
    step_path = BENCHMARK_ROOT / case_dir / "folded.step"
    expected = PIECES[piece_id]
    truth = expected["freecad_unfold_ground_truth"]
    actual = analyze_step_file(
        file_bytes=step_path.read_bytes(),
        source_file=step_path.name,
        material="acciaio",
        density_g_cm3=7.85,
    ).model_dump()

    flat = actual["flat_pattern"]
    dimensions = flat["blank_dimensions_mm"]
    expected_dimensions = truth["blank_dimensions_mm"]
    assert actual["bends"]["count"] == expected["design_truth"]["bend_count"]
    assert flat["status"] in {"exact", "validated_estimate"}
    assert flat["usable_for_costing"] is True
    assert flat["validation"]["passed"] is True
    assert _within_target(dimensions["x"], expected_dimensions["length"], 0.5, 0.5)
    assert _within_target(dimensions["y"], expected_dimensions["width"], 0.5, 0.5)
    assert _within_target(
        flat["net_developed_area_mm2"],
        truth["material_area_from_unfold_volume_mm2"],
        0.5,
        0.0,
    )
    assert _within_target(
        flat["outer_perimeter_mm"],
        truth["largest_planar_outer_perimeter_mm"],
        1.0,
        0.0,
    )


def test_sm02_canonical_guard_never_uses_obsolete_root_trial():
    sm02 = PIECES["SM02_1_piega_60"]["freecad_unfold_ground_truth"]
    assert sm02["root_face"] == "Face5"
    assert sm02["blank_dimensions_mm"]["length"] == 117.9322
    assert sm02["material_area_from_unfold_volume_mm2"] == 7075.9292


def test_sm04_perpendicular_automiter_unfold_benchmark():
    _require_freecad()
    step_path = BENCHMARK_ROOT / "SM04" / "folded.step"
    expected = PIECES["SM04_2_pieghe_perpendicolari"]
    truth = expected["freecad_unfold_ground_truth"]
    actual = analyze_step_file(
        file_bytes=step_path.read_bytes(),
        source_file=step_path.name,
        material="acciaio",
        density_g_cm3=7.85,
    ).model_dump()

    flat = actual["flat_pattern"]
    dimensions = flat["blank_dimensions_mm"]
    expected_dimensions = truth["blank_dimensions_mm"]
    assert actual["bends"]["count"] == 2
    assert flat["status"] in {"exact", "validated_estimate"}
    assert flat["usable_for_costing"] is True
    assert flat["validation"]["passed"] is True
    assert _within_target(dimensions["x"], expected_dimensions["length"], 0.5, 0.5)
    assert _within_target(dimensions["y"], expected_dimensions["width"], 0.5, 0.5)
    assert _within_target(flat["net_developed_area_mm2"], truth["material_area_from_unfold_volume_mm2"], 0.5, 0.0)
    assert _within_target(flat["outer_perimeter_mm"], truth["largest_planar_outer_perimeter_mm"], 1.0, 0.0)
