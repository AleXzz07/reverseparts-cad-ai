from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRICING_CONFIG_PATH = PROJECT_ROOT / "config" / "pricing_default.json"
DEFAULT_MATERIALS_CONFIG_PATH = PROJECT_ROOT / "config" / "materials.json"
STANDARD_QUANTITY_BREAKS = (1, 5, 10, 25, 50, 100)
PRICING_OVERRIDE_FIELDS = {
    "laser_rate_eur_min",
    "bending_rate_eur_min",
    "cad_check_rate_eur_min",
    "handling_rate_eur_min",
    "laser_cut_speed_mm_min",
    "laser_pierce_time_sec",
    "laser_extra_handling_sec_per_piece",
    "bending_setup_time_min",
    "bending_time_sec_per_bend",
    "bending_extra_handling_sec_per_piece",
    "setup_cost_eur",
    "minimum_order_value_eur",
}
MATERIAL_OVERRIDE_FIELDS = {"density_g_cm3", "cost_eur_kg"}


@dataclass(frozen=True)
class QuoteParameters:
    laser_rate_eur_min: float
    bending_rate_eur_min: float
    cad_check_rate_eur_min: float
    handling_rate_eur_min: float
    laser_cut_speed_mm_min: float
    laser_pierce_time_sec: float
    laser_extra_handling_sec_per_piece: float
    bending_setup_time_min: float
    bending_time_sec_per_bend: float
    bending_extra_handling_sec_per_piece: float
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
        bending_setup_time_min=float(data["bending_setup_time_min"]),
        bending_time_sec_per_bend=float(data["bending_time_sec_per_bend"]),
        bending_extra_handling_sec_per_piece=float(data["bending_extra_handling_sec_per_piece"]),
        setup_cost_eur=float(data["setup_cost_eur"]),
        minimum_order_value_eur=float(data["minimum_order_value_eur"]),
    )


def load_materials_config(path: Path = DEFAULT_MATERIALS_CONFIG_PATH) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    materials = {}
    for material_name, values in data.items():
        material_config: dict[str, Any] = {
            "density_g_cm3": float(values["density_g_cm3"]),
            "cost_eur_kg": float(values["cost_eur_kg"]),
        }
        laser = values.get("laser")
        if isinstance(laser, dict):
            material_config["laser"] = {
                "cut_speed_mm_min": float(laser["cut_speed_mm_min"]),
                "pierce_time_sec": float(laser["pierce_time_sec"]),
            }
        materials[material_name] = material_config
    return materials


def _parameters_to_dict(parameters: QuoteParameters) -> dict[str, float]:
    return asdict(parameters)


def _validated_overrides(
    overrides: dict[str, float] | None,
    allowed_fields: set[str],
    label: str,
) -> dict[str, float]:
    if not overrides:
        return {}
    unknown = sorted(set(overrides) - allowed_fields)
    if unknown:
        raise ValueError(f"Override {label} non riconosciuti: {', '.join(unknown)}.")
    values = {key: float(value) for key, value in overrides.items()}
    invalid = sorted(key for key, value in values.items() if value < 0)
    if invalid:
        raise ValueError(f"Override {label} non validi: {', '.join(invalid)} devono essere >= 0.")
    return values


def _round_money(value: float) -> float:
    return round(value, 2)


def _material_error(material_name: str, materials: dict[str, dict[str, float]]) -> ValueError:
    available = ", ".join(sorted(materials)) or "none"
    return ValueError(f"Materiale non presente in config/materials.json: {material_name}. Materiali disponibili: {available}.")


def _feature_count(cad_data: dict[str, Any], group: str) -> int:
    return len(cad_data.get("holes", {}).get(group, []) or [])


def _bend_count(cad_data: dict[str, Any]) -> int:
    count = cad_data.get("bends", {}).get("count")
    if count is not None:
        return int(count)
    return len(cad_data.get("bends", {}).get("items", []) or [])


def _bend_count_is_declared(cad_data: dict[str, Any]) -> bool:
    return cad_data.get("bends", {}).get("count") is not None


def _pierce_count(total_holes: int) -> int:
    return 1 + total_holes


def _laser_profile(
    material_config: dict[str, Any] | None,
    parameters: QuoteParameters,
) -> tuple[float, float, bool]:
    laser = material_config.get("laser") if material_config else None
    if isinstance(laser, dict) and "cut_speed_mm_min" in laser and "pierce_time_sec" in laser:
        return float(laser["cut_speed_mm_min"]), float(laser["pierce_time_sec"]), True
    return parameters.laser_cut_speed_mm_min, parameters.laser_pierce_time_sec, False


def _process_plan(bends: int) -> list[str]:
    plan = ["laser 2D"]
    if bends > 0:
        plan.append("piegatura")
    return plan


def _complexity(
    circular_holes: int,
    elongated_holes: int,
    polygonal_holes: int,
    formed_holes: int,
    unknown_holes: int,
    bends: int,
) -> str:
    feature_score = (
        circular_holes
        + elongated_holes * 2
        + polygonal_holes * 2
        + formed_holes * 3
        + unknown_holes * 2
        + bends * 2
    )
    if feature_score >= 25:
        return "high"
    if feature_score >= 8:
        return "medium"
    return "low"


def _confidence(cad_data: dict[str, Any]) -> str:
    if cad_data.get("complexity_score") == "high":
        return "low"
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
    formed_holes: int,
    unknown_holes: int,
    total_holes: int,
    bends: int,
    bends_count_available: bool,
    estimated_weight_kg: float | None,
    material_config: dict[str, Any] | None,
    parameters: QuoteParameters,
    total_cut_length_mm: float | None,
) -> dict[str, Any]:
    laser_cut_speed_mm_min, laser_pierce_time_sec, material_laser_profile_used = _laser_profile(
        material_config,
        parameters,
    )
    if total_cut_length_mm is not None and total_cut_length_mm > 0:
        pierce_count = _pierce_count(total_holes)
        laser_time_min_per_piece = (
            total_cut_length_mm / laser_cut_speed_mm_min
            + pierce_count * laser_pierce_time_sec / 60
            + parameters.laser_extra_handling_sec_per_piece / 60
        )
        laser_cutting = round(laser_time_min_per_piece * quantity, 2)
        laser_time_source = "cut_length"
        laser_details = {
            "cut_length_mm": total_cut_length_mm,
            "material_laser_profile_used": material_laser_profile_used,
            "cut_speed_mm_min": laser_cut_speed_mm_min,
            "pierce_count": pierce_count,
            "pierce_time_sec": laser_pierce_time_sec,
            "laser_time_min_per_piece": round(laser_time_min_per_piece, 4),
        }
    else:
        laser_feature_factor = (
            circular_holes * 0.12
            + elongated_holes * 0.35
            + polygonal_holes * 0.3
            + formed_holes * 0.4
            + unknown_holes * 0.4
        )
        laser_cutting = round((2.0 + laser_feature_factor) * quantity, 2)
        laser_time_source = "fallback_feature_based"
        laser_details = {
            "cut_length_mm": None,
            "material_laser_profile_used": material_laser_profile_used,
            "cut_speed_mm_min": laser_cut_speed_mm_min,
            "pierce_count": None,
            "pierce_time_sec": laser_pierce_time_sec,
            "laser_time_min_per_piece": None,
        }

    cad_check = 3.0
    if bends_count_available and bends > 0:
        bending_time_min_per_piece = (
            bends * parameters.bending_time_sec_per_bend
            + parameters.bending_extra_handling_sec_per_piece
        ) / 60
        bending = round(parameters.bending_setup_time_min + bending_time_min_per_piece * quantity, 2)
        bending_details = {
            "bends_count": bends,
            "bending_setup_time_min": parameters.bending_setup_time_min,
            "bending_time_sec_per_bend": parameters.bending_time_sec_per_bend,
            "bending_extra_handling_sec_per_piece": parameters.bending_extra_handling_sec_per_piece,
            "bending_time_min_per_piece": round(bending_time_min_per_piece, 4),
            "bending_time_total_min": bending,
        }
    elif bends_count_available:
        bending = 0.0
        bending_details = {
            "bends_count": 0,
            "bending_setup_time_min": 0.0,
            "bending_time_sec_per_bend": parameters.bending_time_sec_per_bend,
            "bending_extra_handling_sec_per_piece": 0.0,
            "bending_time_min_per_piece": 0.0,
            "bending_time_total_min": 0.0,
        }
    else:
        bending = round((0.8 + bends * 0.55) * quantity if bends else 0.0, 2)
        bending_details = {
            "bends_count": None,
            "bending_setup_time_min": None,
            "bending_time_sec_per_bend": None,
            "bending_extra_handling_sec_per_piece": None,
            "bending_time_min_per_piece": None,
            "bending_time_total_min": bending,
        }
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
        "bending_details": bending_details,
    }


def quote_from_cad(
    cad_data: dict[str, Any],
    *,
    quantity: int = 1,
    material: str | None = None,
    parameters: QuoteParameters | None = None,
    materials: dict[str, dict[str, Any]] | None = None,
    pricing_overrides: dict[str, float] | None = None,
    material_overrides: dict[str, float] | None = None,
    pricing_config_path: Path = DEFAULT_PRICING_CONFIG_PATH,
    materials_config_path: Path = DEFAULT_MATERIALS_CONFIG_PATH,
) -> dict[str, Any]:
    parameters = parameters or load_pricing_config(pricing_config_path)
    materials = materials or load_materials_config(materials_config_path)
    effective_pricing_overrides = _validated_overrides(
        pricing_overrides,
        PRICING_OVERRIDE_FIELDS,
        "pricing",
    )
    effective_material_overrides = _validated_overrides(
        material_overrides,
        MATERIAL_OVERRIDE_FIELDS,
        "materiale",
    )
    if effective_pricing_overrides:
        parameters = replace(parameters, **effective_pricing_overrides)
    quantity = max(int(quantity), 1)
    circular_holes = _feature_count(cad_data, "circular")
    elongated_holes = _feature_count(cad_data, "elongated")
    polygonal_holes = _feature_count(cad_data, "polygonal")
    formed_holes = _feature_count(cad_data, "formed")
    unknown_holes = _feature_count(cad_data, "unknown")
    total_holes = (
        circular_holes
        + elongated_holes
        + polygonal_holes
        + formed_holes
        + unknown_holes
    )
    bends = _bend_count(cad_data)
    bends_count_available = _bend_count_is_declared(cad_data)
    total_cut_length_mm = cad_data.get("cutting", {}).get("total_cut_length_mm")

    material_name = material or cad_data.get("declared_material")
    material_key = str(material_name).lower() if material_name else None
    base_material_config = materials.get(material_key or "") if material_key else None
    material_config = (
        {
            **base_material_config,
            "laser": dict(base_material_config.get("laser", {})),
        }
        if base_material_config
        else None
    )
    if material is not None and material_config is None:
        raise _material_error(str(material), materials)
    if material_config is not None:
        material_config.update(effective_material_overrides)
        if "laser_cut_speed_mm_min" in effective_pricing_overrides:
            material_config["laser"]["cut_speed_mm_min"] = parameters.laser_cut_speed_mm_min
        if "laser_pierce_time_sec" in effective_pricing_overrides:
            material_config["laser"]["pierce_time_sec"] = parameters.laser_pierce_time_sec

    volume_cm3 = cad_data.get("volume_cm3")
    if material_config is not None and volume_cm3 is not None:
        estimated_weight_kg = round(float(volume_cm3) * material_config["density_g_cm3"] / 1000, 3)
        weight_source = "recalculated_from_volume"
    else:
        estimated_weight_kg = cad_data.get("estimated_weight_kg")
        weight_source = "cad_estimate" if estimated_weight_kg is not None else None
    thickness_mm = cad_data.get("detected_thickness_mm") or cad_data.get("declared_thickness_mm")
    warnings = [
        "Preventivo preliminare: parametri economici caricati da config e da validare con dati aziendali reali.",
        "Il motore non applica margine e non decide il prezzo finale commerciale.",
    ]
    if material_config is None:
        warnings.append("Materiale non presente in config/materials.json: costo materiale non calcolabile in modo affidabile.")
    if estimated_weight_kg is None:
        warnings.append("Peso stimato non disponibile: costo materiale non calcolabile in modo affidabile.")
    if material_config is not None and volume_cm3 is None:
        warnings.append("Volume CAD non disponibile: peso materiale mantenuto dalla stima CAD originale.")
    if thickness_mm is None:
        warnings.append("Spessore non disponibile: complessita processo meno affidabile.")
    if not bends_count_available:
        warnings.append("Conteggio pieghe non disponibile: tempo piegatura calcolato con fallback euristico.")
    if cad_data.get("complexity_score") == "high":
        warnings.append(
            "Parte CAD complessa: tempi e feature rilevate richiedono verifica tecnica."
        )
    if unknown_holes > 0:
        warnings.append(
            "Some openings were detected but their shape could not be "
            "classified with confidence."
        )

    complexity = _complexity(
        circular_holes,
        elongated_holes,
        polygonal_holes,
        formed_holes,
        unknown_holes,
        bends,
    )
    amounts = _estimate_amounts(
        quantity=quantity,
        circular_holes=circular_holes,
        elongated_holes=elongated_holes,
        polygonal_holes=polygonal_holes,
        formed_holes=formed_holes,
        unknown_holes=unknown_holes,
        total_holes=total_holes,
        bends=bends,
        bends_count_available=bends_count_available,
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
            formed_holes=formed_holes,
            unknown_holes=unknown_holes,
            total_holes=total_holes,
            bends=bends,
            bends_count_available=bends_count_available,
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
                "bending_details": break_amounts["bending_details"],
            }
        )

    return {
        "part_name": cad_data.get("part_name", ""),
        "quantity": quantity,
        "process_plan": _process_plan(bends),
        "material": {
            "name": material_name,
            "density_g_cm3": material_config["density_g_cm3"] if material_config else None,
            "cost_eur_kg": material_config["cost_eur_kg"] if material_config else None,
            "estimated_weight_kg": estimated_weight_kg,
            "weight_source": weight_source,
            "thickness_mm": thickness_mm,
        },
        "features_summary": {
            "circular_holes": circular_holes,
            "elongated_holes": elongated_holes,
            "polygonal_holes": polygonal_holes,
            "formed_holes": formed_holes,
            "unknown_holes": unknown_holes,
            "total_holes": total_holes,
            "bends": bends,
        },
        "cost_drivers": {
            "complexity": (
                "high"
                if cad_data.get("complexity_score") == "high"
                else complexity
            ),
            "laser_cutting_complexity": (
                "medium: profilo lamiera con fori circolari, asole e fori poligonali"
                if (
                    circular_holes
                    + elongated_holes
                    + polygonal_holes
                    + unknown_holes
                    >= 6
                )
                else "low: geometria semplice"
            ),
            "bending_complexity": (
                "high: molte pieghe rilevate, verifica tecnica richiesta"
                if bends >= 8
                else (
                    f"medium: {bends} flange semplici da piegare"
                    if bends > 0
                    else "low: nessuna piega rilevata"
                )
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
        "overrides_used": bool(
            effective_pricing_overrides or effective_material_overrides
        ),
        "confidence": _confidence(cad_data),
        "warnings": warnings,
    }


def quote_files(
    actual_path: Path,
    output_path: Path,
    quantity: int = 1,
    material: str | None = None,
    pricing_config_path: Path = DEFAULT_PRICING_CONFIG_PATH,
    materials_config_path: Path = DEFAULT_MATERIALS_CONFIG_PATH,
) -> dict[str, Any]:
    cad_data = json.loads(actual_path.read_text(encoding="utf-8"))
    quote = quote_from_cad(
        cad_data,
        quantity=quantity,
        material=material,
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
    parser.add_argument("--material", type=str, default=None)
    parser.add_argument("--pricing-config", type=Path, default=DEFAULT_PRICING_CONFIG_PATH)
    parser.add_argument("--materials-config", type=Path, default=DEFAULT_MATERIALS_CONFIG_PATH)
    args = parser.parse_args()

    try:
        quote = quote_files(
            args.actual,
            args.output,
            quantity=args.quantity,
            material=args.material,
            pricing_config_path=args.pricing_config,
            materials_config_path=args.materials_config,
        )
    except ValueError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(quote, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
