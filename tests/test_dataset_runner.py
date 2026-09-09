import json
import os
import shutil
from pathlib import Path

import pytest

from app.cad_analyzer import get_freecad_status
from app.dataset_runner import analyze_case, evaluate_dataset, iter_dataset_cases, quote_dataset
from app.evaluator import evaluate_staffa
from app.quote_engine import quote_from_cad


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_test_1"
LAMIERA_DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "lamiera_piana_test_1"
STAFFA_1_PIEGA_DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_1_piega_test_1"
STAFFA_U_DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_u_test_1"
STAFFA_16_PIEGHE_STRESS_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_16_pieghe_stress_test"
FREECAD_VALIDATION_CASE_NAMES = (
    "validation_01_staffa_piana",
    "validation_02_staffa_L_1_piega",
    "validation_03_staffa_U_2_pieghe",
    "validation_04_staffa_profilo_irregolare",
    "validation_05_staffa_2_pieghe_non_parallele",
)
FREECAD_VALIDATION_V2_CASE_NAMES = (
    "validation_06_staffa_piana_t1",
    "validation_07_staffa_L_t3",
    "validation_08_staffa_3_pieghe_parallele",
    "validation_09_staffa_aperture_miste",
    "validation_10_blocco_massivo_negativo",
)
FREECAD_VALIDATION_V3_CASE_NAMES = (
    "validation_13_piastra_foro_svasato",
    "validation_14_multisolid_due_piastre",
)
FREECAD_VALIDATION_V3_FINAL_CASE_NAMES = (
    "validation_11_staffa_1_piega_60deg",
    "validation_15_piastra_collarino_saldato",
)
DATASET_CASE_NAMES = (
    "lamiera_piana_test_1",
    "staffa_1_piega_test_1",
    "staffa_test_1",
    "staffa_u_test_1",
    "staffa_16_pieghe_stress_test",
    *FREECAD_VALIDATION_CASE_NAMES,
    *FREECAD_VALIDATION_V2_CASE_NAMES,
    *FREECAD_VALIDATION_V3_CASE_NAMES,
    *FREECAD_VALIDATION_V3_FINAL_CASE_NAMES,
)


def _require_freecad() -> None:
    status = get_freecad_status()
    if status.available:
        return
    if os.getenv("REVERSEPARTS_RUNNING_IN_DOCKER") == "1":
        pytest.fail(f"FreeCAD must be available inside Docker: {status.error}")
    pytest.skip(f"FreeCAD is required for dataset CAD regression: {status.error}")


def test_staffa_test_1_dataset_case_exists():
    assert (DATASET_CASE / "input.stp").exists()
    assert (DATASET_CASE / "expected.json").exists()
    assert (DATASET_CASE / "actual.json").exists()
    assert (DATASET_CASE / "evaluation.json").exists()
    assert (DATASET_CASE / "quote.json").exists()

    cases = iter_dataset_cases(PROJECT_ROOT / "tests" / "dataset")
    assert DATASET_CASE in cases


def test_freecad_validation_dataset_cases_exist():
    assert (PROJECT_ROOT / "tests" / "dataset" / "freecad_validation_ground_truth.json").exists()
    for case_name in FREECAD_VALIDATION_CASE_NAMES:
        case_dir = PROJECT_ROOT / "tests" / "dataset" / case_name
        assert (case_dir / "input.stp").exists()
        assert (case_dir / "expected.json").exists()
        expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
        assert expected["material_key"] == "acciaio"
        assert expected["density_g_cm3"] == 7.85


def test_freecad_validation_v2_dataset_cases_exist():
    assert (PROJECT_ROOT / "tests" / "dataset" / "freecad_validation_ground_truth_v2.json").exists()
    for case_name in FREECAD_VALIDATION_V2_CASE_NAMES:
        case_dir = PROJECT_ROOT / "tests" / "dataset" / case_name
        assert (case_dir / "input.stp").exists()
        assert (case_dir / "expected.json").exists()
        expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
        assert expected["material_key"] == "acciaio"
        assert expected["density_g_cm3"] == 7.85


def test_freecad_validation_v3_dataset_cases_exist():
    assert (PROJECT_ROOT / "tests" / "dataset" / "freecad_validation_ground_truth_v3.json").exists()
    for case_name in FREECAD_VALIDATION_V3_CASE_NAMES:
        case_dir = PROJECT_ROOT / "tests" / "dataset" / case_name
        assert (case_dir / "input.stp").exists()
        assert (case_dir / "expected.json").exists()
        expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
        assert expected["material_key"] == "acciaio"
        assert expected["density_g_cm3"] == 7.85


def test_freecad_validation_v3_final_dataset_cases_exist():
    assert (PROJECT_ROOT / "tests" / "dataset" / "ground_truth_v3_final.json").exists()
    for case_name in FREECAD_VALIDATION_V3_FINAL_CASE_NAMES:
        case_dir = PROJECT_ROOT / "tests" / "dataset" / case_name
        assert (case_dir / "input.stp").exists()
        assert (case_dir / "expected.json").exists()
        expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
        assert expected["material_key"] == "acciaio"
        assert expected["density_g_cm3"] == 7.85


def test_staffa_test_1_dataset_evaluate_and_quote(tmp_path):
    dataset_dir = tmp_path / "dataset"
    case_dir = dataset_dir / "staffa_test_1"
    case_dir.mkdir(parents=True)
    for file_name in ("expected.json", "actual.json"):
        shutil.copyfile(DATASET_CASE / file_name, case_dir / file_name)

    evaluations = evaluate_dataset(dataset_dir)
    quotes = quote_dataset(dataset_dir)

    evaluation = json.loads((case_dir / "evaluation.json").read_text(encoding="utf-8"))
    quote = json.loads((case_dir / "quote.json").read_text(encoding="utf-8"))

    assert evaluations[0]["case"] == "staffa_test_1"
    assert quotes[0]["case"] == "staffa_test_1"
    assert evaluation["status"] == "pass"
    assert quote["part_name"] == "STAFFA TEST 1"
    assert quote["features_summary"]["bends"] == 2
    assert quote["estimated_internal_cost_eur"]["total"] is None
    assert quote["estimated_internal_cost_eur"]["laser"] is None
    assert quote["material"]["blank_weight_kg"] is None
    assert quote["laser_applicability"]["status"] == "not_available"
    assert any(
        "nessun fallback euristico" in warning
        for warning in quote["warnings"]
    )
    assert quote["commercial_guidance"]["margin_applied"] is False


def test_dataset_lamiera_piana():
    assert (LAMIERA_DATASET_CASE / "input.stp").exists()
    assert (LAMIERA_DATASET_CASE / "expected.json").exists()
    assert (LAMIERA_DATASET_CASE / "actual.json").exists()
    assert (LAMIERA_DATASET_CASE / "evaluation.json").exists()
    assert (LAMIERA_DATASET_CASE / "quote.json").exists()

    actual = json.loads((LAMIERA_DATASET_CASE / "actual.json").read_text(encoding="utf-8"))
    quote = json.loads((LAMIERA_DATASET_CASE / "quote.json").read_text(encoding="utf-8"))

    assert actual["bends"]["count"] == 0
    assert len(actual["holes"]["circular"]) >= 4
    assert quote["process_plan"] == ["laser 2D"]
    assert "piegatura" not in quote["process_plan"]
    assert quote["features_summary"]["bends"] == 0
    assert quote["estimated_times_min"]["bending"] == 0.0
    assert quote["estimated_internal_cost_eur"]["bending"] == 0.0


def test_dataset_staffa_1_piega():
    assert (STAFFA_1_PIEGA_DATASET_CASE / "input.stp").exists()
    assert (STAFFA_1_PIEGA_DATASET_CASE / "expected.json").exists()
    assert (STAFFA_1_PIEGA_DATASET_CASE / "actual.json").exists()
    assert (STAFFA_1_PIEGA_DATASET_CASE / "evaluation.json").exists()
    assert (STAFFA_1_PIEGA_DATASET_CASE / "quote.json").exists()

    actual = json.loads((STAFFA_1_PIEGA_DATASET_CASE / "actual.json").read_text(encoding="utf-8"))
    quote = json.loads((STAFFA_1_PIEGA_DATASET_CASE / "quote.json").read_text(encoding="utf-8"))

    assert actual["bends"]["count"] == 1
    assert len(actual["bends"]["items"]) == 1
    assert len(actual["holes"]["circular"]) == 2
    assert actual["holes"]["elongated"] == []
    assert actual["holes"]["polygonal"] == []
    assert quote["process_plan"] == ["laser 2D", "piegatura"]
    assert quote["features_summary"]["bends"] == 1
    assert quote["features_summary"]["circular_holes"] == 2
    assert quote["features_summary"]["elongated_holes"] == 0
    assert quote["features_summary"]["polygonal_holes"] == 0
    assert quote["bending_details"]["bends_count"] == 1


def test_dataset_staffa_u():
    assert (STAFFA_U_DATASET_CASE / "input.stp").exists()
    assert (STAFFA_U_DATASET_CASE / "expected.json").exists()
    assert (STAFFA_U_DATASET_CASE / "actual.json").exists()
    assert (STAFFA_U_DATASET_CASE / "evaluation.json").exists()
    assert (STAFFA_U_DATASET_CASE / "quote.json").exists()

    actual = json.loads((STAFFA_U_DATASET_CASE / "actual.json").read_text(encoding="utf-8"))
    quote = json.loads((STAFFA_U_DATASET_CASE / "quote.json").read_text(encoding="utf-8"))

    assert actual["bends"]["count"] == 2
    assert len(actual["bends"]["items"]) == 2
    assert len(actual["holes"]["circular"]) == 4
    assert actual["holes"]["elongated"] == []
    assert actual["holes"]["polygonal"] == []
    assert quote["process_plan"] == ["laser 2D", "piegatura"]
    assert quote["features_summary"]["bends"] == 2
    assert quote["features_summary"]["circular_holes"] == 4
    assert quote["features_summary"]["elongated_holes"] == 0
    assert quote["features_summary"]["polygonal_holes"] == 0
    assert quote["bending_details"]["bends_count"] == 2


def test_dataset_staffa_16_pieghe_stress():
    assert (STAFFA_16_PIEGHE_STRESS_CASE / "input.stp").exists()
    assert (STAFFA_16_PIEGHE_STRESS_CASE / "expected.json").exists()
    assert (STAFFA_16_PIEGHE_STRESS_CASE / "actual.json").exists()
    assert (STAFFA_16_PIEGHE_STRESS_CASE / "evaluation.json").exists()
    assert (STAFFA_16_PIEGHE_STRESS_CASE / "quote.json").exists()

    actual = json.loads(
        (STAFFA_16_PIEGHE_STRESS_CASE / "actual.json").read_text(encoding="utf-8")
    )
    quote = json.loads(
        (STAFFA_16_PIEGHE_STRESS_CASE / "quote.json").read_text(encoding="utf-8")
    )

    assert actual["raw_bounding_box_mm"]["x"] is not None
    assert actual["detected_thickness_mm"] == 2.0
    assert actual["complexity_score"] == "high"
    assert actual["holes"]["circular_holes"] == 8
    assert actual["holes"]["polygonal_holes"] == 3
    assert actual["holes"]["formed_holes"] == 1
    assert actual["holes"]["total_holes"] == 12
    assert actual["bends"]["count"] == 17
    assert actual["warnings"]
    assert quote["process_plan"] == ["laser 2D", "piegatura"]
    assert quote["bending_details"]["bends_count"] == actual["bends"]["count"]
    assert quote["features_summary"]["circular_holes"] == 8
    assert quote["features_summary"]["polygonal_holes"] == 3
    assert quote["features_summary"]["formed_holes"] == 1
    assert quote["features_summary"]["total_holes"] == 12
    assert quote["laser_details"]["pierce_count"] == 13
    assert quote["confidence"] == "low"
    assert quote["warnings"]


@pytest.mark.parametrize("case_name", DATASET_CASE_NAMES)
def test_real_cad_analysis_matches_dataset_ground_truth(case_name, tmp_path):
    _require_freecad()
    source_case = PROJECT_ROOT / "tests" / "dataset" / case_name
    case_dir = tmp_path / case_name
    case_dir.mkdir()
    for file_name in ("input.stp", "expected.json"):
        shutil.copyfile(source_case / file_name, case_dir / file_name)

    actual = analyze_case(case_dir)
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
    report = evaluate_staffa(actual, expected)

    failed_checks = {
        name: check
        for name, check in report["checks"].items()
        if check["status"] == "fail"
    }
    assert report["status"] == "pass", failed_checks

    holes = actual["holes"]
    if case_name == "validation_01_staffa_piana":
        assert holes["circular_holes"] == 4
        assert holes["elongated_holes"] == 1
        assert holes["unknown_holes"] == 0
        assert holes["total_holes"] == 5
        slot = holes["elongated"][0]
        assert slot["overall_length_mm"] == pytest.approx(30.0, abs=0.25)
        assert slot["width_mm"] == pytest.approx(10.0, abs=0.25)
        assert slot["perimeter_mm"] == pytest.approx(71.42, abs=0.25)
        assert slot["axis"] == pytest.approx([0.0, 0.0, 1.0], abs=0.01)
        assert [abs(value) for value in slot["orientation_axis"]] == pytest.approx(
            [1.0, 0.0, 0.0], abs=0.01
        )
        manufacturability = actual["manufacturability"]
        assert manufacturability["measured_holes"] == 5
        assert manufacturability["measured_hole_pairs"] == 10
        assert manufacturability["hole_to_edge_confidence"] == "high"
        assert manufacturability["hole_to_hole_confidence"] == "high"
        assert manufacturability["min_hole_to_hole_mm"] == pytest.approx(
            34.012, abs=0.05
        )
        assert slot["edge_distance_mm"] is not None
        assert slot["nearest_hole_distance_mm"] is not None
        assert not any(
            "Hole-to-edge distance is available only" in warning
            for warning in manufacturability["warnings"]
        )

    if case_name == "validation_04_staffa_profilo_irregolare":
        assert actual["detected_thickness_mm"] == pytest.approx(2.0, abs=0.1)
        assert holes["elongated_holes"] == 1
        assert holes["rounded_rectangular_holes"] == 1
        assert holes["polygonal_holes"] == 0
        assert holes["unknown_holes"] == 0
        slot = holes["elongated"][0]
        assert slot["overall_length_mm"] == pytest.approx(28.0, abs=0.25)
        assert slot["width_mm"] == pytest.approx(8.0, abs=0.25)
        assert slot["axis"] == pytest.approx([0.0, 0.0, 1.0], abs=0.01)
        assert [abs(value) for value in slot["orientation_axis"]] == pytest.approx(
            [1.0, 0.0, 0.0], abs=0.01
        )
        rounded = holes["rounded_rectangular"][0]
        assert rounded["overall_length_mm"] == pytest.approx(26.0, abs=0.25)
        assert rounded["width_mm"] == pytest.approx(16.0, abs=0.25)
        assert rounded["corner_radius_mm"] == pytest.approx(4.0, abs=0.25)
        assert rounded["perimeter_mm"] == pytest.approx(77.13, abs=0.25)
        manufacturability = actual["manufacturability"]
        assert manufacturability["measured_holes"] == 4
        assert manufacturability["measured_hole_pairs"] == 6
        assert manufacturability["hole_to_edge_confidence"] == "high"
        assert manufacturability["hole_to_hole_confidence"] == "high"
        assert manufacturability["min_hole_to_hole_mm"] == pytest.approx(
            16.0, abs=0.05
        )
        assert slot["edge_distance_mm"] is not None
        assert slot["nearest_hole_distance_mm"] is not None
        assert not any(
            "Hole-to-edge distance is available only" in warning
            for warning in manufacturability["warnings"]
        )
        assert actual["flat_pattern"]["net_developed_area_mm2"] == pytest.approx(
            8237.13,
            abs=0.1,
        )

    if case_name == "validation_07_staffa_L_t3":
        assert actual["detected_thickness_mm"] == pytest.approx(3.0, abs=0.1)
        assert holes["circular_holes"] == 4
        assert sorted(hole["diameter_mm"] for hole in holes["circular"]) == pytest.approx(
            [6.0, 8.0, 10.0, 12.0], abs=0.1
        )
        assert holes["total_holes"] == 4
        assert actual["bends"]["count"] == 1

    if case_name == "validation_10_blocco_massivo_negativo":
        assert actual["detected_thickness_mm"] is None
        assert actual["part_classification"]["category"] == "non_sheet_metal"
        assert actual["part_classification"]["confidence"] in {"medium", "high"}
        assert holes["circular_holes"] == 2
        assert holes["total_holes"] == 2
        assert actual["bends"]["count"] == 0
        assert actual["flat_pattern"]["status"] == "unavailable"
        assert actual["cutting"]["total_cut_length_mm"] is None
        quote = quote_from_cad(actual, material="acciaio")
        assert quote["quote_applicability"]["status"] == "not_applicable"
        assert quote["process_plan"] == []
        assert quote["estimated_internal_cost_eur"]["total"] is None
        assert quote["estimated_internal_cost_eur"]["bending"] == 0.0

    if case_name == "validation_13_piastra_foro_svasato":
        assert actual["detected_thickness_mm"] == pytest.approx(4.0, abs=0.1)
        assert actual["part_classification"]["category"] == "sheet_metal"
        assert holes["circular_holes"] == 2
        assert holes["countersunk_holes"] == 1
        assert holes["physical_openings_total"] == 2
        countersunk = next(
            feature
            for feature in holes["circular"]
            if feature["type"] == "countersunk"
        )
        assert countersunk["diameter_mm"] == pytest.approx(6.0, abs=0.1)
        assert countersunk["through_diameter_mm"] == pytest.approx(6.0, abs=0.1)
        assert countersunk["countersink_major_diameter_mm"] == pytest.approx(12.0, abs=0.1)
        assert countersunk["countersink_depth_mm"] == pytest.approx(2.0, abs=0.1)
        assert countersunk["edge_distance_mm"] == pytest.approx(29.0, abs=0.1)
        assert actual["manufacturability"]["min_hole_to_edge_mm"] != pytest.approx(
            3.0, abs=0.1
        )
        assert actual["flat_pattern"]["net_developed_area_mm2"] == pytest.approx(
            7621.46, abs=0.1
        )
        assert actual["flat_pattern"]["gross_blank_area_mm2"] == pytest.approx(
            7700.0, abs=0.1
        )
        assert actual["cutting"]["inner_cut_length_mm"] == pytest.approx(
            43.98, abs=0.1
        )

    if case_name == "validation_14_multisolid_due_piastre":
        assert actual["geometry"]["solid_count"] == 2
        assert actual["part_classification"]["category"] == "multi_solid"
        assert actual["part_classification"]["confidence"] == "high"
        assert holes["circular_holes"] == 2
        assert holes["physical_openings_total"] == 2
        assert actual["bends"]["count"] == 0
        assert actual["flat_pattern"]["status"] == "unavailable"
        assert actual["cutting"]["total_cut_length_mm"] is None
        assert any("piu solidi" in warning for warning in actual["warnings"])
        quote = quote_from_cad(actual, material="acciaio")
        assert quote["quote_applicability"]["status"] == "not_applicable"
        assert quote["process_plan"] == []
        assert quote["estimated_internal_cost_eur"]["total"] is None
        assert quote["estimated_internal_cost_eur"]["bending"] == 0.0

    if case_name == "validation_15_piastra_collarino_saldato":
        assert actual["geometry"]["solid_count"] == 2
        assert actual["part_classification"]["category"] == "multi_solid"
        assert holes["circular_holes"] == 4
        assert sorted(hole["diameter_mm"] for hole in holes["circular"]) == pytest.approx(
            [6.0, 6.0, 10.0, 10.0], abs=0.1
        )
        assert holes["physical_openings_total"] == 3
        assembly = actual["assembly"]
        assert assembly["component_count"] == 2
        assert assembly["component_opening_features_total"] == 4
        assert assembly["physical_passages_total"] == 3
        assert len(assembly["weld_candidates"]) == 1
        candidate = assembly["weld_candidates"][0]
        assert candidate["state"] == "weld_candidate"
        assert candidate["review_status"] == "pending"
        assert candidate["geometry"] == "circular"
        assert candidate["reference_diameter_mm"] == pytest.approx(18.0, abs=0.1)
        assert candidate["nominal_length_mm"] == pytest.approx(56.5487, abs=0.1)
        assert "process" not in candidate
        assert actual["flat_pattern"]["status"] == "unavailable"
        quote = quote_from_cad(actual, material="acciaio")
        assert quote["quote_applicability"]["status"] == "not_applicable"
        assert quote["process_plan"] == []
        assert quote["welding_quote"]["status"] == "requires_configuration"
        assert quote["welding_quote"]["scope"] == "welding_only"
        assert quote["welding_quote"]["total_cost_eur"] is None

    if case_name == "validation_05_staffa_2_pieghe_non_parallele":
        assert holes["circular_holes"] == 4
        assert holes["polygonal"] == []
        assert holes["unknown"] == []
        assert holes["total_holes"] == 4
        assert actual["flat_pattern"]["status"] in {"partial", "unavailable"}
        assert actual["flat_pattern"]["usable_for_costing"] is False
        assert actual["cutting"]["outer_cut_length_mm"] is None
        assert actual["cutting"]["inner_cut_length_mm"] is None
        assert actual["cutting"]["total_cut_length_mm"] is None
        assert actual["cutting"]["source"] == "unavailable"
        assert any(
            "sviluppo piano non ha superato" in warning
            for warning in actual["cutting"]["warnings"]
        )
