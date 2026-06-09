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
    assert quote["material"]["cost_eur_kg"] == 6.0
    assert quote["features_summary"] == {
        "circular_holes": 4,
        "elongated_holes": 2,
        "polygonal_holes": 2,
        "bends": 2,
    }
    assert quote["cost_drivers"]["complexity"] == "medium"
    assert quote["cost_drivers"]["setup_required"] is True
    assert quote["estimated_times_min"]["total"] > 0
    assert quote["estimated_internal_cost_eur"]["total"] == 25.09
    assert quote["estimated_internal_cost_eur"]["material"] == 0.3
    assert quote["commercial_guidance"]["minimum_order_value_eur"] == 40.0
    assert quote["commercial_guidance"]["minimum_order_applied"] is True
    assert quote["commercial_guidance"]["minimum_billable_price_eur"] == 40.0
    assert quote["commercial_guidance"]["margin_applied"] is False
    assert quote["commercial_guidance"]["note"] == "Il margine commerciale deve essere deciso dall'azienda."
    assert quote["config_used"]["pricing"]["minimum_order_value_eur"] == 40.0
    assert quote["config_used"]["pricing"]["margin_percent"] == 25.0
    assert quote["config_used"]["material"]["cost_eur_kg"] == 6.0
    assert quote["confidence"] in {"medium", "high"}
    assert quote["warnings"]


def test_quote_files_writes_report(tmp_path):
    output_path = tmp_path / "staffa_test_1_quote.json"

    quote = quote_files(ACTUAL_FILE, output_path, quantity=3)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert output_path.exists()
    assert written == quote
    assert written["quantity"] == 3
    assert written["estimated_internal_cost_eur"]["total"] > 0
    assert written["commercial_guidance"]["minimum_billable_price_eur"] > 0
