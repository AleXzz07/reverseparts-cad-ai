import json
from pathlib import Path

from app.evaluator import evaluate_files, evaluate_staffa


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTUAL_FILE = PROJECT_ROOT / "tests" / "output" / "staffa_test_1_actual.json"
EXPECTED_FILE = PROJECT_ROOT / "tests" / "ground_truth" / "staffa_test_1_expected.json"


def test_evaluate_staffa_report_core_checks():
    actual = json.loads(ACTUAL_FILE.read_text(encoding="utf-8"))
    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))

    report = evaluate_staffa(actual, expected)

    assert report["part_name"] == "STAFFA TEST 1"
    assert 0 <= report["score_total"] <= 100
    assert report["status"] in {"pass", "warning", "fail"}
    assert report["checks"]["weight"]["status"] == "pass"
    assert report["checks"]["declared_thickness"]["status"] == "pass"
    assert report["checks"]["detected_thickness"]["status"] == "pass"
    assert report["checks"]["circular_holes"]["status"] == "pass"
    assert report["checks"]["elongated_holes"]["status"] == "pass"
    assert report["checks"]["polygonal_holes"]["status"] == "pass"
    assert report["checks"]["bends"]["status"] == "pass"


def test_evaluate_files_writes_report(tmp_path):
    output_path = tmp_path / "staffa_test_1_evaluation.json"

    report = evaluate_files(ACTUAL_FILE, EXPECTED_FILE, output_path)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert output_path.exists()
    assert written == report
    assert "checks" in written
