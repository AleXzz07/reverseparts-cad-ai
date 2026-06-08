import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.main import app
from app.schemas import CadAnalysisResponse


client = TestClient(app)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAFFA_TEST_FILE = PROJECT_ROOT / "tests" / "test_files" / "STAFFA TEST 1.stp"
STAFFA_EXPECTED_FILE = PROJECT_ROOT / "tests" / "ground_truth" / "staffa_test_1_expected.json"


def test_health_returns_freecad_status():
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert isinstance(payload["freecad_available"], bool)
    assert "freecad_error" in payload


def test_analyze_cad_requires_file():
    response = client.post("/analyze-cad")

    assert response.status_code == 422


def test_analyze_cad_rejects_invalid_extension():
    response = client.post(
        "/analyze-cad",
        files={"file": ("invalid.txt", b"not a step file", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only .stp and .step files are accepted."


def test_analyze_cad_rejects_empty_step_file():
    response = client.post(
        "/analyze-cad",
        files={"file": ("empty.step", b"", "application/step")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded CAD file is empty."


def test_analyze_cad_rejects_unparseable_step_file(monkeypatch):
    class AvailableFreeCad:
        available = True
        error = None

    def fake_analyze_step_file(**kwargs):
        return CadAnalysisResponse(
            part_name="broken",
            source_file=kwargs["source_file"],
            warnings=["FreeCAD failed to parse the STEP file: invalid STEP data"],
        )

    monkeypatch.setattr(api, "get_freecad_status", lambda: AvailableFreeCad())
    monkeypatch.setattr(api, "analyze_step_file", fake_analyze_step_file)

    response = client.post(
        "/analyze-cad",
        files={"file": ("broken.step", b"not valid step data", "application/step")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == ["FreeCAD failed to parse the STEP file: invalid STEP data"]


def test_analyze_cad_staffa_test_1_real_step_file():
    expected = json.loads(STAFFA_EXPECTED_FILE.read_text(encoding="utf-8"))
    health_response = client.get("/health")
    health_payload = health_response.json()
    if not health_payload["freecad_available"]:
        if os.getenv("REVERSEPARTS_RUNNING_IN_DOCKER") == "1":
            pytest.fail(f"FreeCAD must be available inside Docker: {health_payload['freecad_error']}")
        pytest.skip(f"FreeCAD is required for the real STEP fixture: {health_payload['freecad_error']}")

    with STAFFA_TEST_FILE.open("rb") as step_file:
        response = client.post(
            "/analyze-cad",
            data={
                "material": "alluminio",
                "density_g_cm3": "2.70",
                "declared_thickness_mm": str(expected["declared_thickness_mm"]),
                "quantity": "1",
            },
            files={"file": ("STAFFA TEST 1.stp", step_file, "application/step")},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_file"] == "STAFFA TEST 1.stp"
    assert payload["volume_cm3"] is not None
    assert payload["surface_area_cm2"] is not None
    assert payload["estimated_weight_kg"] is not None
    assert payload["declared_material"] == "alluminio"
    assert payload["declared_thickness_mm"] == expected["declared_thickness_mm"]
    assert payload["density_g_cm3"] == 2.70

    raw_bounding_box = payload["raw_bounding_box_mm"]
    assert raw_bounding_box["x"] is not None
    assert raw_bounding_box["y"] is not None
    assert raw_bounding_box["z"] is not None

    assert payload["holes"]["confidence"] == "low"
    assert payload["bends"]["confidence"] == "low"
    assert payload["warnings"]
