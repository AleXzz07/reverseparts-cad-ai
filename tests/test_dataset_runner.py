import json
import shutil
from pathlib import Path

from app.dataset_runner import evaluate_dataset, iter_dataset_cases, quote_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_test_1"
LAMIERA_DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "lamiera_piana_test_1"
STAFFA_1_PIEGA_DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_1_piega_test_1"
STAFFA_U_DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_u_test_1"
STAFFA_16_PIEGHE_STRESS_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_16_pieghe_stress_test"


def test_staffa_test_1_dataset_case_exists():
    assert (DATASET_CASE / "input.stp").exists()
    assert (DATASET_CASE / "expected.json").exists()
    assert (DATASET_CASE / "actual.json").exists()
    assert (DATASET_CASE / "evaluation.json").exists()
    assert (DATASET_CASE / "quote.json").exists()

    cases = iter_dataset_cases(PROJECT_ROOT / "tests" / "dataset")
    assert DATASET_CASE in cases


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
    assert quote["estimated_internal_cost_eur"]["total"] > 0
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
    assert (STAFFA_16_PIEGHE_STRESS_CASE / "actual.json").exists()
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
