from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class QuoteParameters:
    material_cost_eur_kg: float = 6.0
    laser_rate_eur_min: float = 1.2
    bending_rate_eur_min: float = 0.9
    setup_cost_eur: float = 15.0
    margin_percent: float = 25.0


DEFAULT_QUOTE_PARAMETERS = QuoteParameters()


def _round_money(value: float) -> float:
    return round(value, 2)


def _feature_count(cad_data: dict[str, Any], group: str) -> int:
    return len(cad_data.get("holes", {}).get(group, []) or [])


def _bend_count(cad_data: dict[str, Any]) -> int:
    count = cad_data.get("bends", {}).get("count")
    if count is not None:
        return int(count)
    return len(cad_data.get("bends", {}).get("items", []) or [])


def _process_plan(bends: int) -> list[str]:
    plan = ["laser 2D"]
    if bends > 0:
        plan.append("piegatura")
    return plan


def _complexity(
    circular_holes: int,
    elongated_holes: int,
    polygonal_holes: int,
    bends: int,
) -> str:
    feature_score = circular_holes + elongated_holes * 2 + polygonal_holes * 2 + bends * 2
    if feature_score >= 12:
        return "medium"
    if feature_score >= 4:
        return "low"
    return "low"


def _confidence(cad_data: dict[str, Any]) -> str:
    holes_confidence = cad_data.get("holes", {}).get("confidence", "low")
    bends_confidence = cad_data.get("bends", {}).get("confidence", "low")
    thickness_confidence = cad_data.get("thickness_confidence", "low")
    if holes_confidence in {"medium", "high"} and bends_confidence in {"medium", "high"} and thickness_confidence in {"medium", "high"}:
        return "medium"
    return "low"


def quote_from_cad(
    cad_data: dict[str, Any],
    *,
    quantity: int = 1,
    parameters: QuoteParameters = DEFAULT_QUOTE_PARAMETERS,
) -> dict[str, Any]:
    quantity = max(int(quantity), 1)
    circular_holes = _feature_count(cad_data, "circular")
    elongated_holes = _feature_count(cad_data, "elongated")
    polygonal_holes = _feature_count(cad_data, "polygonal")
    bends = _bend_count(cad_data)

    estimated_weight_kg = cad_data.get("estimated_weight_kg")
    thickness_mm = cad_data.get("detected_thickness_mm") or cad_data.get("declared_thickness_mm")
    warnings = [
        "Preventivo preliminare: parametri economici placeholder da validare con dati aziendali reali.",
        "Prezzo finale indicativo, non usare come offerta definitiva senza revisione tecnica/commerciale.",
    ]
    if estimated_weight_kg is None:
        warnings.append("Peso stimato non disponibile: costo materiale non calcolabile in modo affidabile.")
    if thickness_mm is None:
        warnings.append("Spessore non disponibile: complessita processo meno affidabile.")

    complexity = _complexity(circular_holes, elongated_holes, polygonal_holes, bends)
    laser_feature_factor = (
        circular_holes * 0.12
        + elongated_holes * 0.35
        + polygonal_holes * 0.3
    )
    laser_cutting = round((2.0 + laser_feature_factor) * quantity, 2)
    bending = round((0.8 + bends * 0.55) * quantity if bends else 0.0, 2)
    cad_check = 3.0
    handling = round((1.5 + 0.4 * quantity), 2)
    total_time = round(cad_check + laser_cutting + bending + handling, 2)

    material_cost = (
        _round_money(float(estimated_weight_kg) * parameters.material_cost_eur_kg * quantity)
        if estimated_weight_kg is not None
        else None
    )
    laser_cost = _round_money(laser_cutting * parameters.laser_rate_eur_min)
    bending_cost = _round_money(bending * parameters.bending_rate_eur_min)
    setup_cost = _round_money(parameters.setup_cost_eur)
    subtotal = (material_cost or 0.0) + laser_cost + bending_cost + setup_cost
    total_internal = _round_money(subtotal)
    suggested_price = _round_money(total_internal * (1.0 + parameters.margin_percent / 100.0))

    return {
        "part_name": cad_data.get("part_name", ""),
        "quantity": quantity,
        "process_plan": _process_plan(bends),
        "material": {
            "name": cad_data.get("declared_material"),
            "thickness_mm": thickness_mm,
            "estimated_weight_kg": estimated_weight_kg,
        },
        "features_summary": {
            "circular_holes": circular_holes,
            "elongated_holes": elongated_holes,
            "polygonal_holes": polygonal_holes,
            "bends": bends,
        },
        "cost_drivers": {
            "complexity": complexity,
            "laser_cutting_complexity": (
                "medium: profilo lamiera con fori circolari, asole e fori poligonali"
                if circular_holes + elongated_holes + polygonal_holes >= 6
                else "low: geometria semplice"
            ),
            "bending_complexity": (
                "medium: 2 flange semplici da piegare"
                if bends == 2
                else "low: nessuna o poche pieghe rilevate"
            ),
            "setup_required": True,
        },
        "estimated_times_min": {
            "cad_check": cad_check,
            "laser_cutting": laser_cutting,
            "bending": bending,
            "handling": handling,
            "total": total_time,
        },
        "estimated_cost_eur": {
            "material": material_cost,
            "laser": laser_cost,
            "bending": bending_cost,
            "setup": setup_cost,
            "total_internal": total_internal,
            "suggested_price": suggested_price,
            "price_note": "indicativo",
        },
        "pricing_parameters": {
            **asdict(parameters),
            "source": "placeholder configurabili in app/quote_engine.py",
        },
        "confidence": _confidence(cad_data),
        "warnings": warnings,
    }


def quote_files(actual_path: Path, output_path: Path, quantity: int = 1) -> dict[str, Any]:
    cad_data = json.loads(actual_path.read_text(encoding="utf-8"))
    quote = quote_from_cad(cad_data, quantity=quantity)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(quote, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return quote


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a preliminary quote for STAFFA TEST 1.")
    parser.add_argument("--actual", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quantity", type=int, default=1)
    args = parser.parse_args()

    quote = quote_files(args.actual, args.output, quantity=args.quantity)
    print(json.dumps(quote, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
