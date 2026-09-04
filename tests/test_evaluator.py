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
    assert report["score_total"] >= 95
    assert report["status"] == "pass"
    assert report["checks"]["dimensions"]["status"] == "pass"
    assert report["checks"]["volume"]["status"] == "pass"
    assert report["checks"]["area"]["status"] == "pass"
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


def test_evaluator_rejects_extra_detected_holes():
    actual = {
        "holes": {
            "circular": [
                {"diameter_mm": 6.0},
                {"diameter_mm": 6.0},
            ]
        }
    }
    expected = {
        "part_name": "EXTRA HOLE REGRESSION",
        "holes": {"circular": [{"diameter_mm": 6.0, "count": 1}]},
    }

    report = evaluate_staffa(actual, expected)

    assert report["status"] == "fail"
    assert report["checks"]["circular_holes"]["actual_count"] == 2
    assert report["checks"]["circular_holes"]["expected_count"] == 1


def test_evaluator_accepts_explicitly_empty_hole_group():
    report = evaluate_staffa(
        {"holes": {"circular": []}},
        {"part_name": "NO CIRCULAR HOLES", "holes": {"circular": []}},
    )

    assert report["status"] == "pass"
    assert report["checks"]["circular_holes"]["actual_count"] == 0


def test_evaluator_supports_count_only_complex_ground_truth():
    actual = {
        "detected_thickness_mm": 2.0,
        "complexity_score": "high",
        "holes": {
            "circular_holes": 8,
            "elongated_holes": 0,
            "polygonal_holes": 3,
            "formed_holes": 1,
            "unknown_holes": 0,
            "total_holes": 12,
        },
        "bends": {"count": 17, "confidence": "high", "items": []},
    }
    expected = {
        "part_name": "COMPLEX COUNT REGRESSION",
        "detected_thickness_mm": 2.0,
        "complexity_score": "high",
        "holes": {
            "circular_holes": 8,
            "elongated_holes": 0,
            "polygonal_holes": 3,
            "formed_holes": 1,
            "unknown_holes": 0,
            "total_holes": 12,
        },
        "bends": {"count": 17},
    }

    report = evaluate_staffa(actual, expected)

    assert report["status"] == "pass"
    assert report["score_total"] == 100
    assert report["checks"]["formed_holes"]["status"] == "pass"
    assert report["checks"]["unknown_holes"]["status"] == "pass"
    assert report["checks"]["total_holes"]["status"] == "pass"
