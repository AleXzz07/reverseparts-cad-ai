import base64
import json
import os
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.main as api
from app.main import app
from app.pdf_report import _part_rows, _verification_rows
from app.schemas import CadAnalysisResponse


client = TestClient(app)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAFFA_TEST_FILE = PROJECT_ROOT / "tests" / "test_files" / "STAFFA TEST 1.stp"
STAFFA_EXPECTED_FILE = PROJECT_ROOT / "tests" / "ground_truth" / "staffa_test_1_expected.json"
STAFFA_ACTUAL_FILE = PROJECT_ROOT / "tests" / "output" / "staffa_test_1_actual.json"
STAFFA_QUOTE_FILE = PROJECT_ROOT / "tests" / "output" / "staffa_test_1_quote.json"


def _skip_without_freecad_for_real_fixture() -> None:
    health_response = client.get("/health")
    health_payload = health_response.json()
    if not health_payload["freecad_available"]:
        if os.getenv("REVERSEPARTS_RUNNING_IN_DOCKER") == "1":
            pytest.fail(f"FreeCAD must be available inside Docker: {health_payload['freecad_error']}")
        pytest.skip(f"FreeCAD is required for the real STEP fixture: {health_payload['freecad_error']}")


def _analyze_staffa_test_1() -> dict:
    expected = json.loads(STAFFA_EXPECTED_FILE.read_text(encoding="utf-8"))
    _skip_without_freecad_for_real_fixture()

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
    return response.json()


def test_health_returns_freecad_status():
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert isinstance(payload["freecad_available"], bool)
    assert "freecad_error" in payload


def test_frontend_returns_html():
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "REVERSEPARTS" in response.text
    assert "Analizza e genera preventivo" in response.text
    assert '<script src="/app-config.js"></script>' in response.text
    assert "window.REVERSEPARTS_API_BASE_URL" in response.text
    assert 'id="api-backend"' in response.text
    assert "API backend:" in response.text
    assert 'fetchApi("/health")' in response.text
    assert 'fetchApi("/config/defaults")' in response.text
    assert 'fetchApi("/analyze-and-quote"' in response.text
    assert 'fetchApi("/quote-pdf"' in response.text
    assert 'fetch(apiUrl("/health"))' not in response.text
    assert 'fetch(apiUrl("/config/defaults"))' not in response.text
    assert 'fetch(apiUrl("/analyze-and-quote")' not in response.text
    assert "Stato FreeCAD non verificato" in response.text
    assert "health check failed" in response.text
    assert "config loaded successfully" in response.text
    assert "Promise.all" not in response.text
    assert "Legenda parametri" in response.text
    assert 'class="info-tip"' in response.text
    assert "Densit&agrave; del materiale." in response.text
    assert "Anteprima pezzo" in response.text
    assert "Vista 3D interattiva" in response.text
    assert 'id="viewer-frame"' in response.text
    assert 'data-viewer-action="fit"' in response.text
    assert 'data-viewer-view="isometric"' in response.text
    assert '<script src="/vendor/three.min.js"></script>' in response.text
    assert "dataset.threeReady" in response.text
    assert "THREE.OrbitControls" in response.text
    assert 'id="part-preview"' in response.text
    assert 'id="preview-thumbnails"' in response.text
    assert "Isometrica" in response.text
    assert "Frontale" in response.text
    assert "Destra" in response.text
    assert "Alto" in response.text
    assert (
        "Anteprima non disponibile. Analisi e preventivo generati correttamente."
        in response.text
    )
    assert '["Asole",' in response.text
    assert '["Fori non riconosciuti",' in response.text


def test_frontend_serves_bundled_three_assets():
    response = client.get("/vendor/three.min.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert len(response.content) > 500_000


def test_app_config_uses_api_base_url(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "https://reverseparts-cad-api.onrender.com/")

    response = client.get("/app-config.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "no-store"
    assert (
        'window.REVERSEPARTS_API_BASE_URL = '
        '"https://reverseparts-cad-api.onrender.com";'
    ) in response.text


def test_app_config_defaults_to_relative_requests(monkeypatch):
    monkeypatch.delenv("API_BASE_URL", raising=False)

    response = client.get("/app-config.js")

    assert response.status_code == 200
    assert 'window.REVERSEPARTS_API_BASE_URL = "";' in response.text


def test_cors_allows_vercel_frontend():
    response = client.get(
        "/health",
        headers={"Origin": "https://reverseparts-cad-ai.vercel.app"},
    )

    assert response.status_code == 200
    assert (
        response.headers["access-control-allow-origin"]
        == "https://reverseparts-cad-ai.vercel.app"
    )


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


def test_quote_endpoint_generates_quote_from_analysis():
    analysis = json.loads(STAFFA_ACTUAL_FILE.read_text(encoding="utf-8"))

    response = client.post(
        "/quote",
        json={
            "analysis": analysis,
            "quantity": 37,
            "material": "acciaio",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["quantity"] == 37
    assert payload["material"]["name"] == "acciaio"
    assert payload["material"]["density_g_cm3"] == 7.85
    assert payload["material"]["weight_source"] == "recalculated_from_volume"
    assert payload["laser_details"]["material_laser_profile_used"] is True
    assert payload["laser_details"]["cut_speed_mm_min"] == 3500.0
    assert payload["overrides_used"] is False


def test_quote_endpoint_pricing_overrides_change_cost():
    analysis = json.loads(STAFFA_ACTUAL_FILE.read_text(encoding="utf-8"))
    base_response = client.post(
        "/quote",
        json={
            "analysis": analysis,
            "quantity": 10,
            "material": "alluminio",
        },
    )
    override_response = client.post(
        "/quote",
        json={
            "analysis": analysis,
            "quantity": 10,
            "material": "alluminio",
            "pricing_overrides": {
                "laser_rate_eur_min": 9.0,
                "setup_cost_eur": 50.0,
            },
            "material_overrides": {
                "cost_eur_kg": 12.0,
            },
        },
    )

    assert base_response.status_code == 200
    assert override_response.status_code == 200
    base = base_response.json()
    overridden = override_response.json()
    assert base["overrides_used"] is False
    assert overridden["overrides_used"] is True
    assert overridden["config_used"]["pricing"]["laser_rate_eur_min"] == 9.0
    assert overridden["config_used"]["pricing"]["setup_cost_eur"] == 50.0
    assert overridden["config_used"]["material"]["cost_eur_kg"] == 12.0
    assert (
        overridden["estimated_internal_cost_eur"]["total"]
        > base["estimated_internal_cost_eur"]["total"]
    )


def test_quote_endpoint_rejects_unknown_material():
    analysis = json.loads(STAFFA_ACTUAL_FILE.read_text(encoding="utf-8"))

    response = client.post(
        "/quote",
        json={
            "analysis": analysis,
            "quantity": 1,
            "material": "titanio",
        },
    )

    assert response.status_code == 400
    assert "Unknown material 'titanio'" in response.json()["detail"]


def test_quote_endpoint_validates_quantity():
    analysis = json.loads(STAFFA_ACTUAL_FILE.read_text(encoding="utf-8"))

    response = client.post(
        "/quote",
        json={
            "analysis": analysis,
            "quantity": 0,
            "material": "alluminio",
        },
    )

    assert response.status_code == 422


def test_quote_pdf_endpoint_returns_pdf():
    analysis = json.loads(STAFFA_ACTUAL_FILE.read_text(encoding="utf-8"))
    quote = json.loads(STAFFA_QUOTE_FILE.read_text(encoding="utf-8"))

    response = client.post(
        "/quote-pdf",
        json={
            "analysis": analysis,
            "quote": quote,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert len(response.content) > 1000
    assert len(response.content) < 20000
    assert response.content.startswith(b"%PDF")


def test_quote_pdf_part_rows_include_all_hole_categories():
    analysis = json.loads(STAFFA_ACTUAL_FILE.read_text(encoding="utf-8"))
    quote = json.loads(STAFFA_QUOTE_FILE.read_text(encoding="utf-8"))
    quote["features_summary"]["unknown_holes"] = 0

    rows = dict(_part_rows(analysis, quote))

    assert rows["Fori circolari"] == 4
    assert rows["Asole"] == 2
    assert rows["Fori poligonali"] == 2
    assert rows["Fori sagomati/imbutiti"] == 0
    assert rows["Fori non riconosciuti"] == 0
    assert rows["Fori totali"] == 8


def test_quote_pdf_includes_unknown_hole_verification_warning():
    analysis = json.loads(STAFFA_ACTUAL_FILE.read_text(encoding="utf-8"))
    quote = json.loads(STAFFA_QUOTE_FILE.read_text(encoding="utf-8"))
    quote["features_summary"]["unknown_holes"] = 1

    rows = dict(_verification_rows(analysis, quote))

    assert rows["Aperture non riconosciute"] == (
        "Some openings were detected but their shape could not be "
        "classified with confidence."
    )


def test_quote_pdf_endpoint_accepts_preview_png():
    analysis = json.loads(STAFFA_ACTUAL_FILE.read_text(encoding="utf-8"))
    quote = json.loads(STAFFA_QUOTE_FILE.read_text(encoding="utf-8"))
    image_buffer = BytesIO()
    Image.new("RGB", (4, 3), (220, 224, 228)).save(image_buffer, format="PNG")
    preview_png = base64.b64encode(image_buffer.getvalue()).decode("ascii")

    response = client.post(
        "/quote-pdf",
        json={
            "analysis": analysis,
            "quote": quote,
            "preview": {
                "image_png_base64": preview_png,
                "available": True,
                "views": [
                    {
                        "name": name,
                        "image_png_base64": preview_png,
                    }
                    for name in ("isometric", "front", "right", "top")
                ],
                "warnings": [],
            },
            "viewer_model": {
                "available": True,
                "model_base64": "Z2xURg==",
                "format": "glb",
                "warnings": [],
            },
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 1000


def test_analyze_cad_staffa_test_1_real_step_file():
    expected = json.loads(STAFFA_EXPECTED_FILE.read_text(encoding="utf-8"))
    payload = _analyze_staffa_test_1()
    assert payload["source_file"] == "STAFFA TEST 1.stp"
    assert payload["volume_cm3"] is not None
    assert payload["surface_area_cm2"] is not None
    assert payload["estimated_weight_kg"] is not None
    assert abs(payload["estimated_weight_kg"] - 0.05) <= 0.005
    assert payload["declared_material"] == "alluminio"
    assert payload["declared_thickness_mm"] == expected["declared_thickness_mm"]
    assert payload["density_g_cm3"] == 2.70

    raw_bounding_box = payload["raw_bounding_box_mm"]
    assert raw_bounding_box["x"] is not None
    assert raw_bounding_box["y"] is not None
    assert raw_bounding_box["z"] is not None

    assert payload["holes"]["confidence"] in {"medium", "high"}
    assert payload["holes"]["elongated_holes"] == 2
    assert payload["holes"]["unknown_holes"] == 0
    assert payload["holes"]["total_holes"] == 8
    assert payload["bends"]["confidence"] in {"medium", "high"}
    assert isinstance(payload["warnings"], list)


def test_analyze_and_quote_staffa_test_1_real_step_file():
    expected = json.loads(STAFFA_EXPECTED_FILE.read_text(encoding="utf-8"))
    _skip_without_freecad_for_real_fixture()

    with STAFFA_TEST_FILE.open("rb") as step_file:
        response = client.post(
            "/analyze-and-quote",
            data={
                "material": "alluminio",
                "declared_thickness_mm": str(expected["declared_thickness_mm"]),
                "quantity": "37",
            },
            files={"file": ("STAFFA TEST 1.stp", step_file, "application/step")},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["analysis"]["source_file"] == "STAFFA TEST 1.stp"
    assert payload["analysis"]["declared_material"] == "alluminio"
    assert payload["analysis"]["density_g_cm3"] == 2.7
    assert abs(payload["analysis"]["estimated_weight_kg"] - 0.05) <= 0.005
    assert payload["quote"]["quantity"] == 37
    assert payload["quote"]["material"]["name"] == "alluminio"
    assert abs(payload["quote"]["material"]["estimated_weight_kg"] - 0.05) <= 0.005
    assert payload["quote"]["laser_details"]["cut_speed_mm_min"] == 2500.0
    assert payload["quote"]["features_summary"]["bends"] == 2
    assert payload["quote"]["features_summary"]["elongated_holes"] == 2
    assert payload["quote"]["features_summary"]["unknown_holes"] == 0
    assert payload["quote"]["features_summary"]["total_holes"] == 8
    assert payload["quote"]["laser_details"]["pierce_count"] == 9
    assert "preview" in payload
    assert isinstance(payload["preview"]["available"], bool)
    assert "image_png_base64" in payload["preview"]
    assert isinstance(payload["preview"]["views"], list)
    assert isinstance(payload["preview"]["warnings"], list)
    assert "viewer_model" in payload
    assert isinstance(payload["viewer_model"]["available"], bool)
    assert isinstance(payload["viewer_model"]["warnings"], list)
    if payload["viewer_model"]["available"]:
        assert payload["viewer_model"]["format"] == "glb"
        model_bytes = base64.b64decode(payload["viewer_model"]["model_base64"])
        assert model_bytes.startswith(b"glTF")
    if os.getenv("REVERSEPARTS_RUNNING_IN_DOCKER") == "1":
        assert payload["preview"]["available"] is True
        assert len(payload["preview"]["views"]) >= 4
        assert {
            view["name"] for view in payload["preview"]["views"]
        } >= {"isometric", "front", "right", "top"}
        preview_bytes = base64.b64decode(payload["preview"]["image_png_base64"])
        assert preview_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_analyze_and_quote_rejects_unknown_material_before_analysis():
    response = client.post(
        "/analyze-and-quote",
        data={
            "material": "titanio",
            "quantity": "1",
        },
        files={"file": ("part.step", b"", "application/step")},
    )

    assert response.status_code == 400
    assert "Unknown material 'titanio'" in response.json()["detail"]


def test_analyze_and_quote_survives_preview_failure(monkeypatch):
    events = []

    async def fake_analysis(**kwargs):
        events.append("analysis")
        return CadAnalysisResponse(
            part_name="safe-preview-test",
            source_file="safe-preview-test.step",
            volume_cm3=10.0,
            complexity_score="high",
        )

    def fake_quote(*args, **kwargs):
        events.append("quote")
        return {
            "part_name": "safe-preview-test",
            "quantity": 1,
            "process_plan": ["laser 2D"],
        }

    def failed_preview(*args, **kwargs):
        events.append("preview")
        raise RuntimeError("renderer process crashed")

    def unavailable_model(*args, **kwargs):
        events.append("viewer")
        return {
            "available": False,
            "model_base64": None,
            "format": None,
            "warnings": [],
        }

    monkeypatch.setattr(api, "_analyze_uploaded_cad", fake_analysis)
    monkeypatch.setattr(api, "quote_from_cad", fake_quote)
    monkeypatch.setattr(api, "generate_safe_viewer_model", unavailable_model)
    monkeypatch.setattr(api, "generate_safe_step_preview", failed_preview)

    response = client.post(
        "/analyze-and-quote",
        data={"material": "alluminio", "quantity": "1"},
        files={"file": ("safe-preview-test.step", b"STEP", "application/step")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert events == ["analysis", "quote", "viewer", "preview"]
    assert payload["analysis"]["part_name"] == "safe-preview-test"
    assert payload["quote"]["part_name"] == "safe-preview-test"
    assert payload["preview"]["available"] is False
    assert payload["preview"]["views"] == []
    assert "renderer process crashed" in payload["preview"]["warnings"][0]

    health_response = client.get("/health")
    assert health_response.status_code == 200
    assert health_response.json()["status"] == "ok"


def test_analyze_and_quote_survives_viewer_model_failure(monkeypatch):
    async def fake_analysis(**kwargs):
        return CadAnalysisResponse(
            part_name="viewer-fallback-test",
            source_file="viewer-fallback-test.step",
            volume_cm3=10.0,
        )

    monkeypatch.setattr(api, "_analyze_uploaded_cad", fake_analysis)
    monkeypatch.setattr(
        api,
        "quote_from_cad",
        lambda *args, **kwargs: {"part_name": "viewer-fallback-test"},
    )
    monkeypatch.setattr(
        api,
        "generate_safe_viewer_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("GLB worker crashed")
        ),
    )
    monkeypatch.setattr(
        api,
        "generate_safe_step_preview",
        lambda *args, **kwargs: {
            "available": False,
            "image_png_base64": None,
            "views": [],
            "warnings": [],
        },
    )

    response = client.post(
        "/analyze-and-quote",
        data={"material": "alluminio", "quantity": "1"},
        files={"file": ("viewer-fallback-test.step", b"STEP", "application/step")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["analysis"]["part_name"] == "viewer-fallback-test"
    assert payload["quote"]["part_name"] == "viewer-fallback-test"
    assert payload["viewer_model"]["available"] is False
    assert payload["viewer_model"]["model_base64"] is None
    assert "GLB worker crashed" in payload["viewer_model"]["warnings"][0]


def test_detect_circular_holes_staffa_test_1():
    payload = _analyze_staffa_test_1()

    circular_holes = payload["holes"]["circular"]
    assert len(circular_holes) >= 4

    seven_mm_holes = [
        hole
        for hole in circular_holes
        if hole["diameter_mm"] is not None and abs(hole["diameter_mm"] - 6.99) <= 0.15
    ]
    five_mm_holes = [
        hole
        for hole in circular_holes
        if hole["diameter_mm"] is not None and abs(hole["diameter_mm"] - 4.99) <= 0.15
    ]

    assert len(seven_mm_holes) >= 2
    assert len(five_mm_holes) >= 2
    assert payload["holes"]["confidence"] in {"medium", "high"}


def test_detect_elongated_holes_staffa_test_1():
    payload = _analyze_staffa_test_1()

    elongated_holes = payload["holes"]["elongated"]
    assert len(elongated_holes) >= 2

    expected_length_holes = [
        hole
        for hole in elongated_holes
        if hole["length_mm"] is not None and abs(hole["length_mm"] - 51.37) <= 0.25
    ]

    assert len(expected_length_holes) >= 2


def test_detect_polygonal_holes_staffa_test_1():
    payload = _analyze_staffa_test_1()

    polygonal_holes = payload["holes"]["polygonal"]
    assert len(polygonal_holes) >= 2

    expected_dimension_holes = [
        hole
        for hole in polygonal_holes
        if hole["max_dimension_mm"] is not None
        and abs(hole["max_dimension_mm"] - 27.71) <= 0.25
    ]

    assert len(expected_dimension_holes) >= 2


def test_detect_sheet_thickness_staffa_test_1():
    payload = _analyze_staffa_test_1()

    assert payload["detected_thickness_mm"] is not None
    assert abs(payload["detected_thickness_mm"] - 2.0) <= 0.1
    assert payload["thickness_confidence"] in {"medium", "high"}


def test_detect_bends_staffa_test_1():
    payload = _analyze_staffa_test_1()

    bends = payload["bends"]
    assert bends["count"] == 2
    assert bends["confidence"] in {"medium", "high"}
    assert len(bends["items"]) == 2
    assert all(item["type"] == "simple flange" for item in bends["items"])
    assert all(
        item["length_mm"] is not None and abs(item["length_mm"] - 50.0) <= 1.0
        for item in bends["items"]
    )


def test_detect_cutting_lengths_staffa_test_1():
    payload = _analyze_staffa_test_1()

    cutting = payload["cutting"]
    assert cutting["outer_cut_length_mm"] is not None
    assert cutting["inner_cut_length_mm"] is not None
    assert cutting["total_cut_length_mm"] is not None
    assert cutting["inner_cut_length_mm"] > 200.0
    assert cutting["total_cut_length_mm"] == round(
        cutting["outer_cut_length_mm"] + cutting["inner_cut_length_mm"],
        2,
    )
    assert cutting["confidence"] in {"medium", "high"}
    assert isinstance(cutting["warnings"], list)
