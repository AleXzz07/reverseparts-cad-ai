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


def test_evaluator_compares_slot_overall_length_and_width():
    report = evaluate_staffa(
        {
            "holes": {
                "elongated": [
                    {
                        "length_mm": 71.42,
                        "overall_length_mm": 30.0,
                        "width_mm": 10.0,
                    }
                ]
            }
        },
        {
            "holes": {
                "elongated": [
                    {"overall_length_mm": 30.0, "width_mm": 10.0, "count": 1}
                ]
            }
        },
    )

    assert report["status"] == "pass"
    assert report["checks"]["elongated_holes"]["status"] == "pass"


def test_evaluator_compares_rounded_rectangle_length_width_and_radius():
    report = evaluate_staffa(
        {
            "holes": {
                "rounded_rectangular": [
                    {
                        "overall_length_mm": 26.0,
                        "width_mm": 16.0,
                        "corner_radius_mm": 4.0,
                    }
                ]
            }
        },
        {
            "holes": {
                "rounded_rectangular": [
                    {
                        "overall_length_mm": 26.0,
                        "width_mm": 16.0,
                        "corner_radius_mm": 4.0,
                        "count": 1,
                    }
                ]
            }
        },
    )

    assert report["status"] == "pass"
    assert report["checks"]["rounded_rectangular_geometry"]["status"] == "pass"


def test_evaluator_compares_individual_bend_geometry():
    report = evaluate_staffa(
        {
            "bends": {
                "count": 2,
                "confidence": "high",
                "items": [
                    {"radius_mm": 2.0, "angle_deg": 90.0, "length_mm": 46.0},
                    {"radius_mm": 2.0, "angle_deg": 90.0, "length_mm": 54.0},
                ],
            }
        },
        {
            "bends": {
                "count": 2,
                "items": [
                    {"radius_mm": 2.0, "angle_deg": 90.0, "length_mm": 46.0},
                    {"radius_mm": 2.0, "angle_deg": 90.0, "length_mm": 54.0},
                ],
            }
        },
    )

    assert report["status"] == "pass"
    assert report["checks"]["bends"]["status"] == "pass"


def test_evaluator_does_not_compare_weight_when_density_differs():
    report = evaluate_staffa(
        {"estimated_weight_kg": 0.05, "density_g_cm3": 2.7},
        {"estimated_weight_kg": 0.143, "density_g_cm3": 7.85},
    )

    assert report["checks"]["weight"]["status"] == "warning"


def test_evaluator_compares_part_classification():
    report = evaluate_staffa(
        {"part_classification": {"category": "non_sheet_metal"}},
        {"part_classification": {"category": "non_sheet_metal"}},
    )

    assert report["status"] == "pass"
    assert report["checks"]["part_classification"]["status"] == "pass"


def test_evaluator_applies_sheetmetal_flat_benchmark_tolerances():
    report = evaluate_staffa(
        {
            "flat_pattern": {
                "status": "validated_estimate",
                "usable_for_costing": True,
                "blank_dimensions_mm": {"x": 117.7, "y": 60.1},
                "net_developed_area_mm2": 7070.0,
                "outer_perimeter_mm": 356.2,
                "validation": {"passed": True},
            }
        },
        {
            "flat_pattern": {
                "status": "validated_estimate",
                "usable_for_costing": True,
                "blank_dimensions_mm": {"length": 117.9322, "width": 60.0},
                "net_developed_area_mm2": 7075.9292,
                "outer_perimeter_mm": 355.8644,
                "validation": {"passed": True},
            }
        },
    )

    assert report["status"] == "pass"
    assert report["checks"]["flat_pattern_dimension_x"]["status"] == "pass"
    assert report["checks"]["flat_pattern_dimension_y"]["status"] == "pass"
    assert report["checks"]["flat_pattern_net_developed_area_mm2"]["status"] == "pass"
    assert report["checks"]["flat_pattern_outer_perimeter_mm"]["status"] == "pass"
    assert report["checks"]["flat_pattern_validation"]["status"] == "pass"


def test_evaluator_compares_countersink_geometry_and_physical_count():
    actual = {
        "holes": {
            "circular": [
                {
                    "type": "countersunk",
                    "diameter_mm": 6.0,
                    "through_diameter_mm": 6.0,
                    "countersink_major_diameter_mm": 12.0,
                    "countersink_depth_mm": 2.0,
                }
            ],
            "physical_openings_total": 1,
        }
    }
    expected = {
        "holes": {
            "countersunk": [
                {
                    "through_diameter_mm": 6.0,
                    "countersink_major_diameter_mm": 12.0,
                    "countersink_depth_mm": 2.0,
                    "count": 1,
                }
            ],
            "physical_openings_total": 1,
        }
    }

    report = evaluate_staffa(actual, expected)

    assert report["checks"]["countersunk_geometry"]["status"] == "pass"
    assert report["checks"]["physical_openings_total"]["status"] == "pass"


def test_evaluator_compares_assembly_passages_and_weld_candidate_geometry():
    report = evaluate_staffa(
        {
            "assembly": {
                "component_count": 2,
                "component_opening_features_total": 4,
                "physical_passages_total": 3,
                "weld_candidates": [
                    {
                        "state": "weld_candidate",
                        "geometry": "circular",
                        "reference_diameter_mm": 18.0,
                        "nominal_length_mm": 56.55,
                    }
                ],
            }
        },
        {
            "assembly": {
                "component_count": 2,
                "component_opening_features_total": 4,
                "physical_passages_total": 3,
                "weld_candidates": [
                    {
                        "state": "weld_candidate",
                        "geometry": "circular",
                        "reference_diameter_mm": 18.0,
                        "nominal_length_mm": 56.5487,
                    }
                ],
            }
        },
    )

    assert report["status"] == "pass"
    assert report["checks"]["assembly_component_count"]["status"] == "pass"
    assert report["checks"]["assembly_physical_passages_total"]["status"] == "pass"
    assert report["checks"]["assembly_weld_candidates"]["status"] == "pass"
