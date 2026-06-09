from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DIMENSION_TOLERANCE_MM = 1.0
WEIGHT_TOLERANCE_PERCENT = 5.0
DIAMETER_TOLERANCE_MM = 0.2
LENGTH_TOLERANCE_MM = 0.25
POLYGON_TOLERANCE_MM = 0.25
THICKNESS_TOLERANCE_MM = 0.1


def _percent_error(actual: float, expected: float) -> float:
    if expected == 0:
        return 0.0 if actual == 0 else 100.0
    return abs(actual - expected) / abs(expected) * 100.0


def _check(status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "message": message, **details}


def _numeric_check(
    actual: float | None,
    expected: float | None,
    tolerance: float,
    unit: str,
) -> dict[str, Any]:
    if actual is None or expected is None:
        return _check(
            "warning",
            "Value not available for comparison.",
            actual=actual,
            expected=expected,
            tolerance=tolerance,
            unit=unit,
        )

    error = abs(actual - expected)
    return _check(
        "pass" if error <= tolerance else "fail",
        f"Error {error:.3f} {unit}; tolerance {tolerance:.3f} {unit}.",
        actual=actual,
        expected=expected,
        error=round(error, 3),
        tolerance=tolerance,
        unit=unit,
    )


def _dimensions_check(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    axis_checks = {}
    for axis in ("x", "y", "z"):
        axis_checks[axis] = _numeric_check(
            actual.get(axis),
            expected.get(axis),
            DIMENSION_TOLERANCE_MM,
            "mm",
        )

    status = "pass" if all(item["status"] == "pass" for item in axis_checks.values()) else "fail"
    return _check(
        status,
        "Bounding dimensions compared with ground truth.",
        axes=axis_checks,
    )


def _weight_check(actual: float | None, expected: float | None) -> dict[str, Any]:
    if actual is None or expected is None:
        return _check(
            "warning",
            "Weight not available for comparison.",
            actual=actual,
            expected=expected,
            tolerance_percent=WEIGHT_TOLERANCE_PERCENT,
        )

    error_percent = _percent_error(actual, expected)
    return _check(
        "pass" if error_percent <= WEIGHT_TOLERANCE_PERCENT else "fail",
        f"Weight error {error_percent:.2f}%; tolerance {WEIGHT_TOLERANCE_PERCENT:.2f}%.",
        actual=actual,
        expected=expected,
        error_percent=round(error_percent, 3),
        tolerance_percent=WEIGHT_TOLERANCE_PERCENT,
    )


def _count_near(items: list[dict[str, Any]], field: str, target: float, tolerance: float) -> int:
    return sum(
        1
        for item in items
        if item.get(field) is not None and abs(float(item[field]) - target) <= tolerance
    )


def _circular_holes_check(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    actual_items = actual.get("circular", [])
    expected_items = expected.get("circular", [])
    groups = []
    for expected_group in expected_items:
        diameter = float(expected_group["diameter_mm"])
        expected_count = int(expected_group["count"])
        actual_count = _count_near(
            actual_items,
            "diameter_mm",
            diameter,
            DIAMETER_TOLERANCE_MM,
        )
        groups.append(
            {
                "diameter_mm": diameter,
                "expected_count": expected_count,
                "actual_count": actual_count,
                "status": "pass" if actual_count >= expected_count else "fail",
            }
        )

    status = "pass" if groups and all(group["status"] == "pass" for group in groups) else "fail"
    return _check(status, "Circular holes grouped by diameter.", groups=groups)


def _elongated_holes_check(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    actual_items = actual.get("elongated", [])
    expected_items = expected.get("elongated", [])
    groups = []
    for expected_group in expected_items:
        length = float(expected_group["length_mm"])
        expected_count = int(expected_group["count"])
        actual_count = _count_near(actual_items, "length_mm", length, LENGTH_TOLERANCE_MM)
        groups.append(
            {
                "length_mm": length,
                "expected_count": expected_count,
                "actual_count": actual_count,
                "status": "pass" if actual_count >= expected_count else "fail",
            }
        )

    status = "pass" if groups and all(group["status"] == "pass" for group in groups) else "fail"
    return _check(status, "Elongated holes grouped by slot length.", groups=groups)


def _polygonal_holes_check(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    actual_items = actual.get("polygonal", [])
    expected_items = expected.get("polygonal", [])
    groups = []
    for expected_group in expected_items:
        max_dimension = float(expected_group["max_dimension_mm"])
        expected_count = int(expected_group["count"])
        actual_count = _count_near(
            actual_items,
            "max_dimension_mm",
            max_dimension,
            POLYGON_TOLERANCE_MM,
        )
        groups.append(
            {
                "max_dimension_mm": max_dimension,
                "expected_count": expected_count,
                "actual_count": actual_count,
                "status": "pass" if actual_count >= expected_count else "fail",
            }
        )

    status = "pass" if groups and all(group["status"] == "pass" for group in groups) else "fail"
    return _check(status, "Polygonal holes grouped by maximum dimension.", groups=groups)


def _bends_check(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    actual_count = actual.get("count")
    expected_count = expected.get("count")
    confidence = actual.get("confidence")
    items = actual.get("items", [])
    length_target = expected.get("length_mm")
    length_matches = (
        sum(
            1
            for item in items
            if item.get("length_mm") is not None
            and length_target is not None
            and abs(float(item["length_mm"]) - float(length_target)) <= DIMENSION_TOLERANCE_MM
        )
        if length_target is not None
        else 0
    )
    status = (
        "pass"
        if actual_count == expected_count
        and confidence in {"medium", "high"}
        and (length_target is None or length_matches >= expected_count)
        else "fail"
    )
    return _check(
        status,
        "Bends/flanges compared by count, confidence, and length.",
        actual_count=actual_count,
        expected_count=expected_count,
        confidence=confidence,
        length_matches=length_matches,
        expected_length_mm=length_target,
    )


def _score(checks: dict[str, dict[str, Any]]) -> int:
    scorable = [check for check in checks.values() if check["status"] != "warning"]
    if not scorable:
        return 0
    passed = sum(1 for check in scorable if check["status"] == "pass")
    return round(passed / len(scorable) * 100)


def _overall_status(score_total: int, checks: dict[str, dict[str, Any]]) -> str:
    if all(check["status"] in {"pass", "warning"} for check in checks.values()) and score_total >= 90:
        return "pass"
    if score_total >= 70:
        return "warning"
    return "fail"


def evaluate_staffa(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    actual_dimensions = actual.get("effective_dimensions_mm") or actual.get("raw_bounding_box_mm") or {}
    expected_dimensions = expected.get("dimensions_mm") or {}
    actual_holes = actual.get("holes", {})
    expected_holes = expected.get("holes", {})

    checks = {
        "dimensions": _dimensions_check(actual_dimensions, expected_dimensions),
        "volume": _numeric_check(actual.get("volume_cm3"), expected.get("volume_cm3"), 0.0, "cm3"),
        "area": _numeric_check(actual.get("surface_area_cm2"), expected.get("surface_area_cm2"), 0.0, "cm2"),
        "weight": _weight_check(actual.get("estimated_weight_kg"), expected.get("part_weight_kg")),
        "declared_thickness": _numeric_check(
            actual.get("declared_thickness_mm"),
            expected.get("declared_thickness_mm"),
            THICKNESS_TOLERANCE_MM,
            "mm",
        ),
        "detected_thickness": _numeric_check(
            actual.get("detected_thickness_mm"),
            expected.get("declared_thickness_mm"),
            THICKNESS_TOLERANCE_MM,
            "mm",
        ),
        "circular_holes": _circular_holes_check(actual_holes, expected_holes),
        "elongated_holes": _elongated_holes_check(actual_holes, expected_holes),
        "polygonal_holes": _polygonal_holes_check(actual_holes, expected_holes),
        "bends": _bends_check(actual.get("bends", {}), expected.get("bends", {})),
    }

    score_total = _score(checks)
    warnings = [
        f"{name}: {check['message']}"
        for name, check in checks.items()
        if check["status"] in {"warning", "fail"}
    ]
    next_improvements = []
    if checks["dimensions"]["status"] != "pass":
        next_improvements.append(
            "Calibrate effective_dimensions_mm against the AutoForm reference dimensions."
        )
    if checks["volume"]["status"] == "warning" or checks["area"]["status"] == "warning":
        next_improvements.append(
            "Add volume_cm3 and surface_area_cm2 to ground truth when validated values are available."
        )

    return {
        "part_name": actual.get("part_name") or expected.get("part_name", ""),
        "score_total": score_total,
        "status": _overall_status(score_total, checks),
        "checks": checks,
        "warnings": warnings,
        "next_improvements": next_improvements,
    }


def evaluate_files(actual_path: Path, expected_path: Path, output_path: Path) -> dict[str, Any]:
    actual = json.loads(actual_path.read_text(encoding="utf-8"))
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    report = evaluate_staffa(actual, expected)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate STAFFA TEST 1 analysis output.")
    parser.add_argument("--actual", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = evaluate_files(args.actual, args.expected, args.output)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
