from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from app.cad_analyzer import (
    _configure_freecad_path,
    analyze_step_file,
    get_freecad_status,
    load_analysis_config,
)
from app.sheetmetal_unfolder import build_sheet_topology_context


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "tests/test_files/real_sheet_topology_phase3b"
K_FACTOR = 0.4


def _require_freecad():
    status = get_freecad_status()
    if not status.available:
        if os.getenv("REVERSEPARTS_RUNNING_IN_DOCKER") == "1":
            pytest.fail(f"FreeCAD must be available inside Docker: {status.error}")
        pytest.skip(f"FreeCAD is required for real STEP topology tests: {status.error}")
    _configure_freecad_path()
    return importlib.import_module("Part")


def _load_shape(file_name: str):
    Part = _require_freecad()
    shape = Part.Shape()
    shape.read(str(FIXTURE_ROOT / file_name))
    return shape


def _face_index(shape, target) -> int:
    return next(
        index
        for index, face in enumerate(shape.Faces, start=1)
        if target.isSame(face)
    )


def _bend_face_pairs(shape, context) -> set[frozenset[int]]:
    return {
        frozenset(
            (
                _face_index(shape, bend.inner_face),
                _face_index(shape, bend.outer_face),
            )
        )
        for bend in context.bends
        if bend.outer_face is not None
    }


def _analyze(file_name: str):
    path = FIXTURE_ROOT / file_name
    return analyze_step_file(
        file_bytes=path.read_bytes(),
        source_file=file_name,
        material="acciaio",
        density_g_cm3=7.85,
    )


def test_moderate_sheetmetal_4_uses_local_cylinder_pairs_and_stays_partial():
    shape = _load_shape("SWPR-Moderate Sheetmetal 4.STEP")
    context = build_sheet_topology_context(
        shape,
        thickness_mm=2.66,
        k_factor=K_FACTOR,
        parameters=load_analysis_config(),
    )
    pairs = _bend_face_pairs(shape, context)

    wrong_pairs = (
        (108, 54),
        (49, 126),
        (39, 42),
        (40, 41),
        (110, 52),
        (51, 124),
        (35, 46),
        (36, 45),
    )
    local_pairs = (
        (108, 126),
        (49, 54),
        (39, 41),
        (40, 42),
        (110, 124),
        (51, 52),
        (35, 45),
        (36, 46),
    )
    assert all(frozenset(pair) not in pairs for pair in wrong_pairs)
    assert all(frozenset(pair) in pairs for pair in local_pairs)
    assert len(context.panels) == 33
    assert len(context.bends) == 33
    assert all(len(bend.panel_ids) == 2 for bend in context.bends)
    assert not any("trovate 3" in warning for warning in context.graph.warnings)
    assert context.graph.connected is False
    assert context.graph.direct is False

    result = _analyze("SWPR-Moderate Sheetmetal 4.STEP")
    assert result.flat_pattern.status == "partial"
    assert result.flat_pattern.usable_for_costing is False


def test_complex_part_6_rejects_cross_pairs_but_keeps_trimmed_panels_incomplete():
    shape = _load_shape("SWPR-Complex Sheet Metal Part 6.STEP")
    context = build_sheet_topology_context(
        shape,
        thickness_mm=1.29,
        k_factor=K_FACTOR,
        parameters=load_analysis_config(),
    )
    pairs = _bend_face_pairs(shape, context)

    wrong_pairs = (
        (284, 118),
        (119, 283),
        (298, 132),
        (307, 141),
        (133, 297),
        (142, 306),
    )
    local_pairs = (
        (284, 283),
        (119, 118),
        (298, 297),
        (307, 306),
        (133, 132),
        (142, 141),
    )
    assert all(frozenset(pair) not in pairs for pair in wrong_pairs)
    assert all(frozenset(pair) in pairs for pair in local_pairs)
    assert len(context.panels) == 17
    assert len(context.bends) == 15
    anomalous = [len(bend.panel_ids) for bend in context.bends if len(bend.panel_ids) != 2]
    assert anomalous == [1, 1]
    assert sum(len(bend.panel_ids) == 2 for bend in context.bends) == 13
    assert context.graph.connected is False
    assert context.graph.direct is False

    result = _analyze("SWPR-Complex Sheet Metal Part 6.STEP")
    assert result.flat_pattern.status == "partial"
    assert result.flat_pattern.usable_for_costing is False


@pytest.mark.parametrize(
    ("file_name", "thickness_mm", "panel_count", "bend_zone_count", "bend_count"),
    (
        ("SWPR-moderate sheetmetal part 1.STEP", 1.21, 17, 17, 15),
        ("SWPR-Moderate- Mounting Bracket.STEP", 1.88, 14, 13, 13),
    ),
)
def test_existing_real_topology_cases_remain_unchanged(
    file_name: str,
    thickness_mm: float,
    panel_count: int,
    bend_zone_count: int,
    bend_count: int,
):
    shape = _load_shape(file_name)
    context = build_sheet_topology_context(
        shape,
        thickness_mm=thickness_mm,
        k_factor=K_FACTOR,
        parameters=load_analysis_config(),
    )

    assert len(context.panels) == panel_count
    assert len(context.bends) == bend_zone_count
    assert all(len(bend.panel_ids) == 2 for bend in context.bends)
    assert context.graph.connected is True
    assert context.graph.direct is True

    result = _analyze(file_name)
    assert result.bends.count == bend_count
    assert result.flat_pattern.status == "partial"
    assert result.flat_pattern.usable_for_costing is False
