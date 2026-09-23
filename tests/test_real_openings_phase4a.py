from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.cad_analyzer import analyze_step_file, get_freecad_status


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    PROJECT_ROOT
    / "tests/test_files/real_openings_phase4a/SWPR-Simple Sheet Metal Part 9.STEP"
)
MODERATE_PART_1_FIXTURE = (
    PROJECT_ROOT
    / "tests/test_files/real_sheet_topology_phase3b/SWPR-moderate sheetmetal part 1.STEP"
)
PART1_451_FIXTURE = (
    PROJECT_ROOT
    / "tests/test_files/real_openings_phase4a/User Library-Part1-451.STEP"
)
CORRECT_AND_FLATTEN_FIXTURE = (
    PROJECT_ROOT
    / "tests/test_files/real_openings_phase4a/"
    "SWPR-Moderate-Correct-and-Flatten-Imported-Sheet-Metal.STEP"
)


def _require_freecad() -> None:
    status = get_freecad_status()
    if status.available:
        return
    if os.getenv("REVERSEPARTS_RUNNING_IN_DOCKER") == "1":
        pytest.fail(f"FreeCAD must be available inside Docker: {status.error}")
    pytest.skip(f"FreeCAD is required for real STEP opening tests: {status.error}")


def test_simple_sheet_metal_part_9_has_nine_physical_openings():
    _require_freecad()
    result = analyze_step_file(
        file_bytes=FIXTURE.read_bytes(),
        source_file=FIXTURE.name,
        material="acciaio",
        density_g_cm3=7.85,
    )

    assert result.holes.total_holes == 9
    assert result.holes.physical_openings_total == 9
    # Phase 4A must not change the still-unverified bend result.
    assert result.bends.count == 9


def test_moderate_part_1_includes_the_slot_crossing_the_bend():
    _require_freecad()
    result = analyze_step_file(
        file_bytes=MODERATE_PART_1_FIXTURE.read_bytes(),
        source_file=MODERATE_PART_1_FIXTURE.name,
        material="acciaio",
        density_g_cm3=7.85,
    )

    assert result.holes.total_holes == 10
    assert result.holes.physical_openings_total == 10
    bend_crossing_slots = [
        feature
        for feature in result.holes.elongated
        if feature.overall_length_mm is not None
        and feature.width_mm is not None
        and abs(feature.overall_length_mm - 12.7) <= 0.05
        and abs(feature.width_mm - 6.35) <= 0.05
    ]
    assert len(bend_crossing_slots) == 1
    assert bend_crossing_slots[0].axis is None
    assert bend_crossing_slots[0].confidence == "high"


def test_part1_451_separates_closed_draws_from_physical_openings():
    _require_freecad()
    result = analyze_step_file(
        file_bytes=PART1_451_FIXTURE.read_bytes(),
        source_file=PART1_451_FIXTURE.name,
        material="acciaio",
        density_g_cm3=7.85,
    )

    assert result.holes.total_holes == 8
    assert result.holes.physical_openings_total == 8
    assert len(result.forming_features) == 2
    assert all(
        feature.type == "closed circular draw"
        and feature.diameter_mm is not None
        and abs(feature.diameter_mm - 58.83) <= 0.02
        and feature.depth_mm is not None
        and abs(feature.depth_mm - 5.0) <= 0.02
        for feature in result.forming_features
    )


def test_correct_and_flatten_recovers_submillimetre_sheet_holes_and_lances():
    _require_freecad()
    result = analyze_step_file(
        file_bytes=CORRECT_AND_FLATTEN_FIXTURE.read_bytes(),
        source_file=CORRECT_AND_FLATTEN_FIXTURE.name,
        material="acciaio",
        density_g_cm3=7.85,
    )

    assert result.detected_thickness_mm == pytest.approx(0.76, abs=0.01)
    assert result.holes.total_holes == 2
    assert result.holes.physical_openings_total == 2
    assert len(result.holes.circular) == 2
    assert all(
        feature.diameter_mm == pytest.approx(3.6576, abs=0.02)
        for feature in result.holes.circular
    )

    lances = [
        feature
        for feature in result.forming_features
        if feature.type == "lance/tab formed"
    ]
    assert len(lances) == 2
    assert all(
        sorted((feature.length_mm, feature.width_mm))
        == pytest.approx([21.59, 26.67], abs=0.05)
        and feature.cut_length_mm == pytest.approx(78.49, abs=0.05)
        and feature.connected_edge_length_mm == pytest.approx(19.05, abs=0.05)
        and feature.angle_deg == pytest.approx(90.0, abs=0.2)
        for feature in lances
    )

    # The existing bend detector intentionally remains unchanged in Phase 4A
    # v5. It recovers the nine bends with supported outer radii; the two
    # sub-millimetre-radius hems remain for the future PhysicalBendIdentity work.
    assert result.bends.count == 9
    assert result.flat_pattern.status == "partial"
    assert result.flat_pattern.usable_for_costing is False
    assert result.flat_pattern.blank_weight_kg is None
    assert result.cutting.source == "unavailable"
    assert result.cutting.total_cut_length_mm is None
    # The production topology graph currently contains nine zones and all nine
    # are covered. The independent audit found two additional physical hems,
    # but they are not graph nodes until the future PhysicalBendIdentity phase.
    # Do not invent an "11 topology zones" warning in production output.
    assert not any(
        "zone topologiche di piega" in warning
        and "non sono coperte" in warning
        for warning in result.flat_pattern.warnings
    )
    assert any(
        "Sviluppo piano completo non determinabile" in warning
        for warning in result.flat_pattern.warnings
    )
