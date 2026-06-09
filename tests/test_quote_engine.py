import json
from pathlib import Path

from app.quote_engine import quote_files, quote_from_cad


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ACTUAL_FILE = PROJECT_ROOT / "tests" / "output" / "staffa_test_1_actual.json"


def _cad_data_without_cutting() -> dict:
    cad_data = json.loads(ACTUAL_FILE.read_text(encoding="utf-8"))
    cad_data.pop("cutting", None)
    return cad_data


def test_quote_from_cad_staffa_test_1():
    cad_data = _cad_data_without_cutting()

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
    assert quote["estimated_internal_cost_eur"]["unit_cost"] == 25.09
    assert quote["estimated_internal_cost_eur"]["material"] == 0.3
    assert quote["commercial_guidance"]["minimum_order_value_eur"] == 40.0
    assert quote["commercial_guidance"]["minimum_order_applied"] is True
    assert quote["commercial_guidance"]["minimum_billable_price_eur"] == 40.0
    assert quote["commercial_guidance"]["margin_applied"] is False
    assert quote["commercial_guidance"]["note"] == "Il margine commerciale deve essere deciso dall'azienda."
    assert quote["config_used"]["pricing"]["minimum_order_value_eur"] == 40.0
    assert "margin_percent" not in quote["config_used"]["pricing"]
    assert quote["config_used"]["pricing"]["laser_cut_speed_mm_min"] == 2500.0
    assert quote["config_used"]["pricing"]["laser_pierce_time_sec"] == 0.8
    assert quote["config_used"]["pricing"]["laser_extra_handling_sec_per_piece"] == 10.0
    assert quote["config_used"]["material"]["cost_eur_kg"] == 6.0
    assert quote["estimated_times_min"]["laser_time_source"] == "fallback_feature_based"
    assert quote["laser_details"]["cut_length_mm"] is None
    assert quote["laser_details"]["pierce_count"] is None
    assert [item["quantity"] for item in quote["quantity_breakdown"]] == [1, 5, 10, 25, 50, 100]
    assert quote["quantity_breakdown"][0]["estimated_internal_cost_eur"]["unit_cost"] == 25.09
    assert quote["quantity_breakdown"][-1]["estimated_internal_cost_eur"]["unit_cost"] < 10.0
    assert quote["confidence"] in {"medium", "high"}
    assert quote["warnings"]


def test_quote_from_cad_uses_requested_quantity():
    cad_data = _cad_data_without_cutting()

    quote = quote_from_cad(cad_data, quantity=37)

    assert quote["quantity"] == 37
    assert quote["estimated_internal_cost_eur"]["total"] == 269.38
    assert quote["estimated_internal_cost_eur"]["unit_cost"] == 7.28
    assert quote["commercial_guidance"]["minimum_order_applied"] is False
    assert quote["commercial_guidance"]["minimum_billable_price_eur"] == 269.38


def test_quote_uses_cut_length_when_available():
    cad_data = _cad_data_without_cutting()
    cad_data["cutting"] = {
        "outer_cut_length_mm": 300.0,
        "inner_cut_length_mm": 200.0,
        "total_cut_length_mm": 500.0,
        "confidence": "medium",
        "warnings": [],
    }

    quote = quote_from_cad(cad_data)

    assert quote["estimated_times_min"]["laser_time_source"] == "cut_length"
    assert quote["estimated_times_min"]["laser_cut_length_mm"] == 500.0
    assert quote["estimated_times_min"]["laser_cutting"] == 0.49
    assert quote["laser_details"] == {
        "cut_length_mm": 500.0,
        "cut_speed_mm_min": 2500.0,
        "pierce_count": 9,
        "pierce_time_sec": 0.8,
        "laser_time_min_per_piece": 0.4867,
    }
    assert quote["estimated_internal_cost_eur"]["laser"] == 0.59
    assert quote["estimated_internal_cost_eur"]["total"] == 21.14


def test_quote_files_writes_report(tmp_path):
    output_path = tmp_path / "staffa_test_1_quote.json"

    quote = quote_files(ACTUAL_FILE, output_path, quantity=3)
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert output_path.exists()
    assert written == quote
    assert written["quantity"] == 3
    assert written["estimated_internal_cost_eur"]["total"] > 0
    assert written["estimated_internal_cost_eur"]["unit_cost"] == round(
        written["estimated_internal_cost_eur"]["total"] / 3,
        2,
    )
    assert written["commercial_guidance"]["minimum_billable_price_eur"] > 0
