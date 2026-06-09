from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRICING_CONFIG_PATH = PROJECT_ROOT / "config" / "pricing_default.json"
DEFAULT_MATERIALS_CONFIG_PATH = PROJECT_ROOT / "config" / "materials.json"
STANDARD_QUANTITY_BREAKS = (1, 5, 10, 25, 50, 100)


@dataclass(frozen=True)
class QuoteParameters:
    laser_rate_eur_min: float
    bending_rate_eur_min: float
    cad_check_rate_eur_min: float
    handling_rate_eur_min: float
    laser_cut_speed_mm_min: float
    laser_pierce_time_sec: float
    laser_extra_handling_sec_per_piece: float
    setup_cost_eur: float
    minimum_order_value_eur: float


def load_pricing_config(path: Path = DEFAULT_PRICING_CONFIG_PATH) -> QuoteParameters:
    data = json.loads(path.read_text(encoding="utf-8"))
    return QuoteParameters(
        laser_rate_eur_min=float(data["laser_rate_eur_min"]),
        bending_rate_eur_min=float(data["bending_rate_eur_min"]),
        cad_check_rate_eur_min=float(data["cad_check_rate_eur_min"]),
        handling_rate_eur_min=float(data["handling_rate_eur_min"]),
        laser_cut_speed_mm_min=float(data["laser_cut_speed_mm_min"]),
        laser_pierce_time_sec=float(data["laser_pierce_time_sec"]),
        laser_extra_handling_sec_per_piece=float(data["laser_extra_handling_sec_per_piece"]),
        setup_cost_eur=float(data["setup_cost_eur"]),
        minimum_order_value_eur=float(data["minimum_order_value_eur"]),
    )


def load_materials_config(path: Path = DEFAULT_MATERIALS_CONFIG_PATH) -> dict[str, dict[str, float]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        material_name: {
            "density_g_cm3": float(values["density_g_cm3"]),
            "cost_eur_kg": float(values["cost_eur_kg"]),
        }
        for material_name, values in data.items()
    }


def _parameters_to_dict(parameters: QuoteParameters) -> dict[str, float]:
    return {
        "laser_rate_eur_min": parameters.laser_rate_eur_min,
        "bending_rate_eur_min": parameters.bending_rate_eur_min,
        "cad_check_rate_eur_min": parameters.cad_check_rate_eur_min,
        "handling_rate_eur_min": parameters.handling_rate_eur_min,
        "laser_cut_speed_mm_min": parameters.laser_cut_speed_mm_min,
        "laser_pierce_time_sec": parameters.laser_pierce_time_sec,
        "laser_extra_handling_sec_per_piece": parameters.laser_extra_handling_sec_per_piece,
        "setup_cost_eur": parameters.setup_cost_eur,
        "minimum_order_value_eur": parameters.minimum_order_value_eur,
    }


def _round_money(value: float) -> float:
    return round(value, 2)


def _feature_count(cad_data: dict[str, Any], group: str) -> int:
    return len(cad_data.get("holes", {}).get(group, []) or [])


def _bend_count(cad_data: dict[str, Any]) -> int:
    count = cad_data.get("bends", {}).get("count")
    if count is not None:
        return int(count)
    return len(cad_data.get("bends", {}).get("items", []) or [])


def _pierce_count(circular_holes: int, elongated_holes: int, polygonal_holes: int) -> int:
    return 1 + circular_holes + elongated_holes + polygonal_holes


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


def _estimate_amounts(
    *,
    quantity: int,
    circular_holes: int,
    elongated_holes: int,
    polygonal_holes: int,
    bends: int,
    estimated_weight_kg: float | None,
    material_config: dict[str, float] | None,
    parameters: QuoteParameters,
    total_cut_length_mm: float | None,
) -> dict[str, Any]:
    if total_cut_length_mm is not None and total_cut_length_mm > 0:
        pierce_count = _pierce_count(circular_holes, elongated_holes, polygonal_holes)
        laser_time_min_per_piece = (
            total_cut_length_mm / parameters.laser_cut_speed_mm_min
            + pierce_count * parameters.laser_pierce_time_sec / 60
            + parameters.laser_extra_handling_sec_per_piece / 60
        )
        laser_cutting = round(laser_time_min_per_piece * quantity, 2)
        laser_time_source = "cut_length"
        laser_details = {
            "cut_length_mm": total_cut_length_mm,
            "cut_speed_mm_min": parameters.laser_cut_speed_mm_min,
            "pierce_count": pierce_count,
            "pierce_time_sec": parameters.laser_pierce_time_sec,
            "laser_time_min_per_piece": round(laser_time_min_per_piece, 4),
        }
    else:
        laser_feature_factor = (
            circular_holes * 0.12
            + elongated_holes * 0.35
            + polygonal_holes * 0.3
        )
        laser_cutting = round((2.0 + laser_feature_factor) * quantity, 2)
        laser_time_source = "fallback_feature_based"
        laser_details = {
            "cut_length_mm": None,
            "cut_speed_mm_min": parameters.laser_cut_speed_mm_min,
            "pierce_count": None,
            "pierce_time_sec": parameters.laser_pierce_time_sec,
            "laser_time_min_per_piece": None,
        }

    cad_check = 3.0
    bending = round((0.8 + bends * 0.55) * quantity if bends else 0.0, 2)
    handling = round(1.5 + 0.4 * quantity, 2)
    total_time = round(cad_check + laser_cutting + bending + handling, 2)

    material_cost = (
        _round_money(float(estimated_weight_kg) * material_config["cost_eur_kg"] * quantity)
        if estimated_weight_kg is not None and material_config is not None
        else None
    )
    cad_check_cost = _round_money(cad_check * parameters.cad_check_rate_eur_min)
    laser_cost = _round_money(laser_cutting * parameters.laser_rate_eur_min)
    bending_cost = _round_money(bending * parameters.bending_rate_eur_min)
    handling_cost = _round_money(handling * parameters.handling_rate_eur_min)
    setup_cost = _round_money(parameters.setup_cost_eur)
    total_internal = _round_money(
        (material_cost or 0.0)
        + cad_check_cost
        + laser_cost
        + bending_cost
        + handling_cost
        + setup_cost
    )
    unit_cost = _round_money(total_internal / quantity)
    minimum_order_applied = total_internal < parameters.minimum_order_value_eur
    minimum_billable_price = (
        _round_money(parameters.minimum_order_value_eur)
        if minimum_order_applied
        else total_internal
    )

    return {
        "estimated_times_min": {
            "cad_check": cad_check,
            "laser_cutting": laser_cutting,
            "bending": bending,
            "handling": handling,
            "total": total_time,
            "laser_time_source": laser_time_source,
            "laser_cut_length_mm": total_cut_length_mm,
        },
        "estimated_internal_cost_eur": {
            "material": material_cost,
            "laser": laser_cost,
            "bending": bending_cost,
            "cad_check": cad_check_cost,
            "handling": handling_cost,
            "setup": setup_cost,
            "total": total_internal,
            "unit_cost": unit_cost,
        },
        "commercial_guidance": {
            "minimum_order_value_eur": parameters.minimum_order_value_eur,
            "minimum_order_applied": minimum_order_applied,
            "minimum_billable_price_eur": minimum_billable_price,
            "margin_applied": False,
            "note": "Il margine commerciale deve essere deciso dall'azienda.",
        },
        "laser_details": laser_details,
    }


def quote_from_cad(
    cad_data: dict[str, Any],
    *,
    quantity: int = 1,
    parameters: QuoteParameters | None = None,
    materials: dict[str, dict[str, float]] | None = None,
    pricing_config_path: Path = DEFAULT_PRICING_CONFIG_PATH,
    materials_config_path: Path = DEFAULT_MATERIALS_CONFIG_PATH,
) -> dict[str, Any]:
    parameters = parameters or load_pricing_config(pricing_config_path)
    materials = materials or load_materials_config(materials_config_path)
    quantity = max(int(quantity), 1)
    circular_holes = _feature_count(cad_data, "circular")
    elongated_holes = _feature_count(cad_data, "elongated")
    polygonal_holes = _feature_count(cad_data, "polygonal")
    bends = _bend_count(cad_data)
    total_cut_length_mm = cad_data.get("cutting", {}).get("total_cut_length_mm")

    material_name = cad_data.get("declared_material")
    material_config = materials.get(str(material_name).lower()) if material_name else None
    estimated_weight_kg = cad_data.get("estimated_weight_kg")
    thickness_mm = cad_data.get("detected_thickness_mm") or cad_data.get("declared_thickness_mm")
    warnings = [
        "Preventivo preliminare: parametri economici caricati da config e da validare con dati aziendali reali.",
        "Il motore non applica margine e non decide il prezzo finale commerciale.",
    ]
    if material_config is None:
        warnings.append("Materiale non presente in config/materials.json: costo materiale non calcolabile in modo affidabile.")
    if estimated_weight_kg is None:
        warnings.append("Peso stimato non disponibile: costo materiale non calcolabile in modo affidabile.")
    if thickness_mm is None:
        warnings.append("Spessore non disponibile: complessita processo meno affidabile.")

    complexity = _complexity(circular_holes, elongated_holes, polygonal_holes, bends)
    amounts = _estimate_amounts(
        quantity=quantity,
        circular_holes=circular_holes,
        elongated_holes=elongated_holes,
        polygonal_holes=polygonal_holes,
        bends=bends,
        estimated_weight_kg=estimated_weight_kg,
        material_config=material_config,
        parameters=parameters,
        total_cut_length_mm=total_cut_length_mm,
    )
    quantity_breakdown = []
    for break_quantity in STANDARD_QUANTITY_BREAKS:
        break_amounts = _estimate_amounts(
            quantity=break_quantity,
            circular_holes=circular_holes,
            elongated_holes=elongated_holes,
            polygonal_holes=polygonal_holes,
            bends=bends,
            estimated_weight_kg=estimated_weight_kg,
            material_config=material_config,
            parameters=parameters,
            total_cut_length_mm=total_cut_length_mm,
        )
        quantity_breakdown.append(
            {
                "quantity": break_quantity,
                "estimated_internal_cost_eur": {
                    "total": break_amounts["estimated_internal_cost_eur"]["total"],
                    "unit_cost": break_amounts["estimated_internal_cost_eur"]["unit_cost"],
                },
                "commercial_guidance": {
                    "minimum_order_applied": break_amounts["commercial_guidance"]["minimum_order_applied"],
                    "minimum_billable_price_eur": break_amounts["commercial_guidance"]["minimum_billable_price_eur"],
                },
                "laser_details": break_amounts["laser_details"],
            }
        )

    return {
        "part_name": cad_data.get("part_name", ""),
        "quantity": quantity,
        "process_plan": _process_plan(bends),
        "material": {
            "name": material_name,
            "thickness_mm": thickness_mm,
            "estimated_weight_kg": estimated_weight_kg,
            "density_g_cm3": material_config["density_g_cm3"] if material_config else None,
            "cost_eur_kg": material_config["cost_eur_kg"] if material_config else None,
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
            "laser_time_source": amounts["estimated_times_min"]["laser_time_source"],
        },
        **amounts,
        "quantity_breakdown": quantity_breakdown,
        "config_used": {
            "pricing_config": str(pricing_config_path),
            "materials_config": str(materials_config_path),
            "pricing": _parameters_to_dict(parameters),
            "material": material_config,
        },
        "confidence": _confidence(cad_data),
        "warnings": warnings,
    }


def quote_files(
    actual_path: Path,
    output_path: Path,
    quantity: int = 1,
    pricing_config_path: Path = DEFAULT_PRICING_CONFIG_PATH,
    materials_config_path: Path = DEFAULT_MATERIALS_CONFIG_PATH,
) -> dict[str, Any]:
    cad_data = json.loads(actual_path.read_text(encoding="utf-8"))
    quote = quote_from_cad(
        cad_data,
        quantity=quantity,
        pricing_config_path=pricing_config_path,
        materials_config_path=materials_config_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(quote, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return quote


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a preliminary quote for STAFFA TEST 1.")
    parser.add_argument("--actual", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--pricing-config", type=Path, default=DEFAULT_PRICING_CONFIG_PATH)
    parser.add_argument("--materials-config", type=Path, default=DEFAULT_MATERIALS_CONFIG_PATH)
    args = parser.parse_args()

    quote = quote_files(
        args.actual,
        args.output,
        quantity=args.quantity,
        pricing_config_path=args.pricing_config,
        materials_config_path=args.materials_config,
    )
    print(json.dumps(quote, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
