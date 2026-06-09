import json
from pathlib import Path

from app.quote_engine import quote_files, quote_from_cad


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTUAL_FILE = PROJECT_ROOT / "tests" / "output" / "staffa_test_1_actual.json"


def test_quote_from_cad_staffa_test_1():
    cad_data = json.loads(ACTUAL_FILE.read_text(encoding="utf-8"))

    quote = quote_from_cad(cad_data)

    assert quote["part_name"] == "STAFFA TEST 1"
    assert quote["quantity"] == 1
    assert quote["process_plan"] == ["laser 2D", "piegatura"]
    assert quote["material"]["name"] == "alluminio"
    assert quote["material"]["thickness_mm"] == 2.0
    assert quote["material"]["estimated_weight_kg"] == 0.05
    assert quote["features_summary"] == {
        "circular_holes": 4,
        "elongated_holes": 2,
        "polygonal_holes": 2,
        "bends": 2,
    }
    assert quote["cost_drivers"]["complexity"] == "medium"
    assert quote["cost_drivers"]["setup_required"] is True
    assert quote["estimated_times_min"]["total"] > 0
    assert quote["estimated_cost_eur"]["total_internal"] > 0
    assert quote["estimated_cost_eur"]["suggested_price"] > quote["estimated_cost_eur"]["total_internal"]
    assert quote["estimated_cost_eur"]["price_note"] == "indicativo"
    assert quote["pricing_parameters"]["source"] == "placeholder configurabili in app/quote_engine.py"
    assert quote["confidence"] in {"medium", "high"}
    assert quote["warnings"]


def test_quote_files_writes_report(tmp_path):
    output_path = tmp_path / "staffa_test_1_quote.json"

    quote = quote_files(ACTUAL_FILE, output_path, quantity=3)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert output_path.exists()
    assert written == quote
    assert written["quantity"] == 3
    assert written["estimated_cost_eur"]["suggested_price"] > 0
