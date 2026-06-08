from fastapi.testclient import TestClient

import app.main as api
from app.main import app
from app.schemas import CadAnalysisResponse


client = TestClient(app)


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
