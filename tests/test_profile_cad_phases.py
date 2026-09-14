import json

from app.schemas import Bends, CadAnalysisResponse
from scripts.profile_cad_phases import (
    REFERENCE_NAME,
    _discover_profile_cases,
    _result_details,
    _write_outputs,
)


def test_profiler_reads_pydantic_bend_count_without_len():
    response = CadAnalysisResponse(part_name="test", source_file="test.step")
    response.bends = Bends(count=3)

    details = _result_details(response)

    assert details["bend_zone_count"] == 3


def test_profiler_discovers_all_step_files_and_adds_reference_once(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    names = [
        "SWPR-Moderate Sheetmetal 4.STEP",
        "SWPR-Complex Sheet Metal Part 6.STEP",
        "SWPR-complex sheetmetal part 2.STP",
        "SWPR-Moderate-Correct-and-Flatten-Imported-Sheet-Metal.STEP",
    ]
    for index, name in enumerate(names):
        (input_dir / name).write_text(f"STEP-{index}", encoding="utf-8")
    (input_dir / "ignore.txt").write_text("not CAD", encoding="utf-8")
    (input_dir / "zz-duplicate.STEP").write_text("STEP-0", encoding="utf-8")

    reference = tmp_path / "reference" / "input.stp"
    reference.parent.mkdir()
    reference.write_text("REFERENCE", encoding="utf-8")

    cases = _discover_profile_cases(input_dir, reference)

    assert len(cases) == 5
    assert {name for name, _, _, reference_case in cases if not reference_case} == set(names)
    assert cases[-1] == (REFERENCE_NAME, reference.resolve(), False, True)


def test_profiler_does_not_duplicate_reference_content_from_input(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "external.STEP").write_text("EXTERNAL", encoding="utf-8")
    (input_dir / "copied-reference.STP").write_text("REFERENCE", encoding="utf-8")
    reference = tmp_path / "input.stp"
    reference.write_text("REFERENCE", encoding="utf-8")

    cases = _discover_profile_cases(input_dir, reference)

    assert [name for name, _, _, _ in cases] == ["external.STEP", REFERENCE_NAME]


def test_profiler_output_totals_are_dynamic(tmp_path):
    settings = {"expected_total": 5}
    summaries = [{"file_name": "one.STEP"}]

    _write_outputs(tmp_path, summaries, [], settings)

    payload = json.loads((tmp_path / "cad_phase_profile.json").read_text(encoding="utf-8"))
    assert payload["expected_total"] == 5
    assert payload["tested_total"] == 1
