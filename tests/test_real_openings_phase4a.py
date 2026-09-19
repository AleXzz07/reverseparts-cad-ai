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
