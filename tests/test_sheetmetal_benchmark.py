import json
import os
from pathlib import Path

import pytest

import app.pdf_report as pdf_report
from app.cad_analyzer import analyze_step_file, get_freecad_status
from app.pdf_report import _diagnostic_volume_area_error_pct
from app.quote_engine import quote_from_cad


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = PROJECT_ROOT / "tests" / "dataset" / "sheetmetal_benchmark"
BENCHMARK_DATA = json.loads(
    (PROJECT_ROOT / "tests" / "dataset" / "sheetmetal_benchmark_v1.json").read_text(
        encoding="utf-8"
    )
)
PIECES = {piece["id"]: piece for piece in BENCHMARK_DATA["pieces"]}
BLIND_ROOT = PROJECT_ROOT / "tests" / "dataset" / "sheetmetal_benchmark_v2"
BLIND_DATA = json.loads(
    (PROJECT_ROOT / "tests" / "dataset" / "sheetmetal_benchmark_v2_canonical.json").read_text(
        encoding="utf-8"
    )
)
BLIND_PIECES = {piece["id"]: piece for piece in BLIND_DATA["pieces"]}
PENDING_REFERENCE_CASES = set(BLIND_DATA.get("pending_reference_cases", []))


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


def test_blind_benchmark_uses_only_the_approved_canonical_sources():
    assert BLIND_DATA["version"] == "2.3-canonical"
    assert BLIND_DATA["canonical_sources"] == {
        "SM06": "v2",
        "SM07": "v2.1 patch",
        "SM08": "v2.2 patch",
        "SM09": "v2",
        "SM10": "v2.2 patch",
        "SM11": "v2.2 patch",
        "SM12": "v2.1 patch",
        "SM13": "v2.3 patch",
    }
    assert PENDING_REFERENCE_CASES == set()
    sm09_truth = BLIND_PIECES["SM09_2_pieghe_60_90_raggi_diversi"][
        "freecad_unfold_ground_truth"
    ]
    assert sm09_truth["blank_dimensions_local_2d_mm"] == {
        "length": 166.9956,
        "width": 68.0,
    }


def test_blind_v22_replacements_have_verified_final_geometry_truth():
    expected = {
        "SM08_3_pieghe_assi_misti": (3, 158.3673, 103.3982, 14717.3672, 523.5310),
        "SM10_irregolare_2_pieghe": (2, 133.3982, 94.9742, 10261.1076, 435.0707),
        "SM11_miter_relief": (2, 134.3982, 114.3982, 14163.4070, 497.5929),
    }
    for piece_id, (bends, length, width, area, perimeter) in expected.items():
        piece = BLIND_PIECES[piece_id]
        truth = piece["freecad_unfold_ground_truth"]
        assert piece["design_truth"]["bend_count"] == bends
        assert truth["blank_dimensions_mm"] == {"length": length, "width": width}
        assert truth["material_area_from_unfold_volume_mm2"] == area
        assert truth["largest_planar_outer_perimeter_mm"] == perimeter


def test_sm13_v23_reference_contains_propagated_openings():
    piece = BLIND_PIECES["SM13_4_pieghe_multi_feature"]
    truth = piece["freecad_unfold_ground_truth"]
    assert truth["blank_dimensions_local_2d_mm"] == {
        "length": 146.5973,
        "width": 109.3566,
    }
    assert truth["material_area_from_unfold_volume_mm2"] == 13688.5508
    assert truth["largest_planar_gross_area_mm2"] == 13938.8495
    assert truth["aperture_area_mm2"] == 250.2987
    assert truth["opening_wire_count"] == 3
    assert truth["largest_planar_outer_perimeter_mm"] == 511.9078


@pytest.mark.parametrize(
    ("case_dir", "piece_id"),
    tuple(
        (f"SM{number:02d}", next(key for key in BLIND_PIECES if key.startswith(f"SM{number:02d}_")))
        for number in range(6, 14)
    ),
)
def test_blind_sheetmetal_v2_canonical_benchmark(case_dir, piece_id):
    _require_freecad()
    expected = BLIND_PIECES[piece_id]
    truth = expected["freecad_unfold_ground_truth"]
    step_path = BLIND_ROOT / case_dir / "folded.step"
    actual = analyze_step_file(
        file_bytes=step_path.read_bytes(),
        source_file=step_path.name,
        material="acciaio",
        density_g_cm3=7.85,
    ).model_dump()
    flat = actual["flat_pattern"]
    dimensions = flat["blank_dimensions_mm"]
    expected_dimensions = truth.get(
        "blank_dimensions_local_2d_mm",
        truth["blank_dimensions_mm"],
    )

    assert actual["bends"]["count"] == expected["design_truth"]["bend_count"]
    assert flat["status"] in {"exact", "validated_estimate"}
    assert flat["usable_for_costing"] is True
    assert flat["validation"]["passed"] is True
    if case_dir in PENDING_REFERENCE_CASES:
        assert flat["blank_dimensions_mm"] is not None
        assert flat["net_developed_area_mm2"] is not None
        assert _within_target(
            flat["outer_perimeter_mm"],
            truth["largest_planar_outer_perimeter_mm"],
            1.0,
            0.0,
        )
        assert "benchmark_error_pct" not in flat
        return
    assert _within_target(dimensions["x"], expected_dimensions["length"], 0.5, 0.5)
    assert _within_target(dimensions["y"], expected_dimensions["width"], 0.5, 0.5)
    assert _within_target(flat["net_developed_area_mm2"], truth["material_area_from_unfold_volume_mm2"], 0.5, 0.0)
    assert _within_target(flat["outer_perimeter_mm"], truth["largest_planar_outer_perimeter_mm"], 1.0, 0.0)
    assert "benchmark_error_pct" not in flat


@pytest.mark.parametrize("case_dir", ["SM06", "SM07", "SM10", "SM11", "SM13"])
def test_blind_sheetmetal_reporting_uses_final_validated_values(case_dir, monkeypatch):
    _require_freecad()
    step_path = BLIND_ROOT / case_dir / "folded.step"
    analysis = analyze_step_file(
        file_bytes=step_path.read_bytes(),
        source_file=step_path.name,
        material="acciaio",
        density_g_cm3=7.85,
    ).model_dump()
    quote = quote_from_cad(analysis, material="acciaio")
    flat = analysis["flat_pattern"]
    expected_error = _diagnostic_volume_area_error_pct(flat)
    recorded_sections = {}
    original_section = pdf_report._section

    def recording_section(title, section_rows):
        recorded_sections[title] = dict(section_rows)
        return original_section(title, section_rows)

    monkeypatch.setattr(pdf_report, "_section", recording_section)
    pdf_report.generate_quote_pdf(analysis, quote)

    rows = recorded_sections["Sviluppo piano e grezzo"]
    assert flat["usable_for_costing"] is True
    assert flat["validation"]["passed"] is True
    assert flat["validation"]["all_openings_propagated"] is True
    assert rows["Peso grezzo"] != "-"
    assert rows["Differenza diagnostica volume/spessore"] == pdf_report._value(
        expected_error, "%"
    )
    assert "Validated estimate indica" in rows["Nota stato"]
    assert "Exact" not in rows["Nota stato"]
    verification_values = [
        str(value).casefold()
        for value in recorded_sections.get("Avvisi di verifica", {}).values()
    ]
    assert not any("flat non utilizzabile per il costing" in value for value in verification_values)
