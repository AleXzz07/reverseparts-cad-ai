import json
import shutil
from pathlib import Path

from app.dataset_runner import evaluate_dataset, iter_dataset_cases, quote_dataset


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "staffa_test_1"
LAMIERA_DATASET_CASE = PROJECT_ROOT / "tests" / "dataset" / "lamiera_piana_test_1"


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
