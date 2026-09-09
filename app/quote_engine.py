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
STRICTLY_POSITIVE_OVERRIDE_FIELDS = {"laser_cut_speed_mm_min", "density_g_cm3"}


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
    zero_not_allowed = sorted(
        key
        for key, value in values.items()
        if key in STRICTLY_POSITIVE_OVERRIDE_FIELDS and value == 0
    )
    if zero_not_allowed:
        raise ValueError(
            f"Override {label} non validi: {', '.join(zero_not_allowed)} "
            "devono essere maggiori di 0."
        )
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
    laser_costing_available: bool,
) -> dict[str, Any]:
    laser_cut_speed_mm_min, laser_pierce_time_sec, material_laser_profile_used = _laser_profile(
        material_config,
        parameters,
    )
    if not laser_costing_available:
        laser_cutting = None
        laser_time_source = "unavailable_flat_pattern"
        laser_details = {
            "cut_length_mm": None,
            "material_laser_profile_used": material_laser_profile_used,
            "cut_speed_mm_min": laser_cut_speed_mm_min,
            "pierce_count": None,
            "pierce_time_sec": laser_pierce_time_sec,
            "laser_time_min_per_piece": None,
        }
    elif total_cut_length_mm is not None and total_cut_length_mm > 0:
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
        laser_cutting = None
        laser_time_source = "unavailable_flat_cut_length"
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
    total_time = (
        round(cad_check + laser_cutting + bending + handling, 2)
        if laser_cutting is not None
        else None
    )

    material_cost = (
        _round_money(float(estimated_weight_kg) * material_config["cost_eur_kg"] * quantity)
        if estimated_weight_kg is not None and material_config is not None
        else None
    )
    cad_check_cost = _round_money(cad_check * parameters.cad_check_rate_eur_min)
    laser_cost = (
        _round_money(laser_cutting * parameters.laser_rate_eur_min)
        if laser_cutting is not None
        else None
    )
    bending_cost = _round_money(bending * parameters.bending_rate_eur_min)
    handling_cost = _round_money(handling * parameters.handling_rate_eur_min)
    setup_cost = _round_money(parameters.setup_cost_eur)
    total_internal = (
        _round_money(
            (material_cost or 0.0)
            + cad_check_cost
            + laser_cost
            + bending_cost
            + handling_cost
            + setup_cost
        )
        if laser_cost is not None and material_cost is not None
        else None
    )
    unit_cost = _round_money(total_internal / quantity) if total_internal is not None else None
    minimum_order_applied = (
        total_internal < parameters.minimum_order_value_eur
        if total_internal is not None
        else False
    )
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


def _not_applicable_amounts(reason: str) -> dict[str, Any]:
    return {
        "estimated_times_min": {
            "cad_check": None,
            "laser_cutting": None,
            "bending": 0.0,
            "handling": None,
            "total": None,
            "laser_time_source": "not_applicable",
            "laser_cut_length_mm": None,
        },
        "estimated_internal_cost_eur": {
            "material": None,
            "laser": None,
            "bending": 0.0,
            "cad_check": None,
            "handling": None,
            "setup": None,
            "total": None,
            "unit_cost": None,
        },
        "commercial_guidance": {
            "minimum_order_value_eur": None,
            "minimum_order_applied": False,
            "minimum_billable_price_eur": None,
            "margin_applied": False,
            "note": reason,
        },
        "laser_details": {
            "cut_length_mm": None,
            "material_laser_profile_used": False,
            "cut_speed_mm_min": None,
            "pierce_count": None,
            "pierce_time_sec": None,
            "laser_time_min_per_piece": None,
        },
        "bending_details": {
            "bends_count": 0,
            "bending_setup_time_min": 0.0,
            "bending_time_sec_per_bend": None,
            "bending_extra_handling_sec_per_piece": 0.0,
            "bending_time_min_per_piece": 0.0,
            "bending_time_total_min": 0.0,
        },
    }


def _intermittent_effective_length(configuration: dict[str, Any]) -> tuple[float | None, int | None, str | None]:
    length = configuration.get("weld_length_mm")
    segment = configuration.get("segment_length_mm")
    pitch = configuration.get("pitch_mm")
    gap = configuration.get("gap_mm")
    requested_count = configuration.get("segment_count")
    if length is None or segment is None:
        return None, None, "Intermittente: lunghezza giunzione e lunghezza segmento sono obbligatorie."
    length = float(length)
    segment = float(segment)
    if (pitch is None) == (gap is None):
        return None, None, "Intermittente: indicare esattamente uno tra pitch_mm e gap_mm."
    if pitch is not None:
        pitch = float(pitch)
        if pitch < segment:
            return None, None, "Intermittente: pitch_mm deve essere almeno pari a segment_length_mm."
        count = int(requested_count) if requested_count is not None else int((length - segment) // pitch) + 1
        occupied_span = (count - 1) * pitch + segment
        method = "segment_count x segment_length; pitch centro-centro"
    else:
        gap = float(gap)
        count = int(requested_count) if requested_count is not None else int((length + gap) // (segment + gap))
        occupied_span = count * segment + max(0, count - 1) * gap
        method = "segment_count x segment_length; gap libero tra segmenti"
    if count < 1:
        return None, None, "Intermittente: nessun segmento completo entra nella lunghezza configurata."
    if occupied_span > length + 1e-6:
        return None, None, "Intermittente: i segmenti configurati superano la lunghezza disponibile."
    return round(count * segment, 3), count, method


def _calculate_welding_quote(
    cad_data: dict[str, Any],
    welds: list[dict[str, Any]] | None,
    *,
    quantity: int,
) -> dict[str, Any]:
    classification = cad_data.get("part_classification", {}) or {}
    if classification.get("category") != "multi_solid":
        return {
            "status": "not_requested",
            "scope": "welding_only",
            "items": [],
            "setup_groups": [],
            "total_time_min": None,
            "total_cost_eur": None,
            "warnings": [],
        }

    assembly = cad_data.get("assembly", {}) or {}
    candidates = assembly.get("weld_candidates", []) or []
    candidate_by_id = {str(item.get("id")): item for item in candidates}
    configurations = welds or []
    configuration_ids = [str(item.get("weld_id", "")) for item in configurations]
    duplicate_ids = sorted({item_id for item_id in configuration_ids if configuration_ids.count(item_id) > 1})
    global_errors = [
        f"Configurazione duplicata per {item_id}." for item_id in duplicate_ids
    ]
    configured_candidate_ids = {
        str(item.get("weld_id"))
        for item in configurations
        if item.get("source_state") != "manual_weld"
    }
    pending_candidate_ids = sorted(set(candidate_by_id) - configured_candidate_ids)

    item_results: list[dict[str, Any]] = []
    confirmed_for_setup: list[tuple[dict[str, Any], dict[str, Any]]] = []
    productive_time_total = 0.0
    for configuration in configurations:
        weld_id = str(configuration.get("weld_id", ""))
        source_state = configuration.get("source_state")
        review_status = configuration.get("review_status", "confirmed")
        errors: list[str] = []
        if source_state in {"weld_candidate", "weld_detected"}:
            source = candidate_by_id.get(weld_id)
            if source is None:
                errors.append("La saldatura CAD indicata non esiste nell'analisi corrente.")
            elif source.get("state") != source_state:
                errors.append("Lo stato origine non coincide con l'evidenza CAD.")
        elif source_state == "manual_weld":
            source = None
        else:
            source = None
            errors.append("Origine saldatura non valida.")

        base_result = {
            "weld_id": weld_id,
            "source_state": source_state,
            "review_status": review_status,
            "process": configuration.get("process"),
            "continuity": configuration.get("continuity"),
            "joint_type": configuration.get("joint_type"),
            "side": configuration.get("side"),
            "weld_length_mm": configuration.get("weld_length_mm"),
            "segment_length_mm": configuration.get("segment_length_mm"),
            "pitch_mm": configuration.get("pitch_mm"),
            "gap_mm": configuration.get("gap_mm"),
            "segment_count": configuration.get("segment_count"),
            "effective_weld_length_mm": None,
            "productive_length_mm": None,
            "passes": configuration.get("passes"),
            "size_basis": configuration.get("size_basis"),
            "size_mm": configuration.get("size_mm"),
            "setup_scope": configuration.get("setup_scope", "per_process"),
            "setup_time_min": configuration.get("setup_time_min", 0.0),
            "welding_time_min_per_piece": None,
            "preparation_time_min_per_piece": configuration.get("preparation_time_min_per_piece", 0.0),
            "finishing_time_min_per_piece": configuration.get("finishing_time_min_per_piece", 0.0),
            "productive_time_total_min": None,
            "allocated_setup_time_min": 0.0,
            "total_time_min": None,
            "hourly_rate_eur": configuration.get("hourly_rate_eur"),
            "cost_eur": None,
            "calculation_method": None,
            "errors": errors,
        }
        if review_status == "rejected":
            base_result["calculation_method"] = "Candidata esclusa dall'utente; nessun costo applicato."
            item_results.append(base_result)
            continue
        required = {
            "process": configuration.get("process"),
            "continuity": configuration.get("continuity"),
            "joint_type": configuration.get("joint_type"),
            "side": configuration.get("side"),
            "weld_length_mm": configuration.get("weld_length_mm"),
            "size_basis": configuration.get("size_basis"),
            "size_mm": configuration.get("size_mm"),
            "passes": configuration.get("passes"),
            "hourly_rate_eur": configuration.get("hourly_rate_eur"),
        }
        missing = [name for name, value in required.items() if value in {None, ""}]
        if missing:
            errors.append(f"Parametri obbligatori mancanti: {', '.join(missing)}.")
        speed = configuration.get("speed_mm_min")
        time_per_mm = configuration.get("time_sec_per_mm")
        if (speed is None) == (time_per_mm is None):
            errors.append("Indicare esattamente uno tra speed_mm_min e time_sec_per_mm.")

        continuity = configuration.get("continuity")
        effective_length = None
        segment_count = None
        intermittent_method = None
        if continuity == "continuous" and configuration.get("weld_length_mm") is not None:
            if any(configuration.get(field) is not None for field in ("segment_length_mm", "pitch_mm", "gap_mm", "segment_count")):
                errors.append("I parametri dei segmenti non sono ammessi per una saldatura continua.")
            effective_length = round(float(configuration["weld_length_mm"]), 3)
        elif continuity == "intermittent":
            effective_length, segment_count, intermittent_detail = _intermittent_effective_length(configuration)
            if effective_length is None:
                errors.append(str(intermittent_detail))
            else:
                intermittent_method = "Lunghezza intermittente: " + str(intermittent_detail)

        if configuration.get("finishing_grinding") and float(configuration.get("finishing_time_min_per_piece", 0.0)) <= 0:
            errors.append("La finitura/molatura attiva richiede finishing_time_min_per_piece > 0.")
        if errors or effective_length is None:
            base_result["segment_count"] = segment_count or configuration.get("segment_count")
            item_results.append(base_result)
            continue

        side_multiplier = 2 if configuration.get("side") == "both" else 1
        productive_length = effective_length * side_multiplier * int(configuration["passes"])
        if speed is not None:
            welding_time = productive_length / float(speed)
            travel_method = "lunghezza produttiva / velocita"
        else:
            welding_time = productive_length * float(time_per_mm) / 60.0
            travel_method = "lunghezza produttiva x tempo/mm"
        preparation = float(configuration.get("preparation_time_min_per_piece", 0.0))
        finishing = (
            float(configuration.get("finishing_time_min_per_piece", 0.0))
            if configuration.get("finishing_grinding")
            else 0.0
        )
        productive_per_piece = preparation + welding_time + finishing
        productive_total = productive_per_piece * quantity
        productive_time_total += productive_total
        base_result.update(
            {
                "segment_count": segment_count or configuration.get("segment_count"),
                "effective_weld_length_mm": round(effective_length, 3),
                "productive_length_mm": round(productive_length, 3),
                "welding_time_min_per_piece": round(welding_time, 4),
                "productive_time_total_min": round(productive_total, 4),
                "calculation_method": "; ".join(
                    item for item in (intermittent_method, travel_method) if item
                ),
            }
        )
        item_results.append(base_result)
        confirmed_for_setup.append((configuration, base_result))

    setup_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for configuration, result in confirmed_for_setup:
        scope = configuration.get("setup_scope", "per_process")
        if scope == "per_weld":
            key = ("per_weld", str(configuration.get("weld_id")))
        elif scope == "per_lot":
            key = ("per_lot", "lot")
        else:
            key = ("per_process", str(configuration.get("process")))
        group = setup_groups.setdefault(
            key,
            {
                "scope": scope,
                "key": key[1],
                "weld_ids": [],
                "setup_time_min": 0.0,
                "hourly_rate_eur": 0.0,
                "setup_cost_eur": 0.0,
                "method": "Setup massimo dichiarato nel gruppo; non sommato tra saldature dello stesso gruppo.",
            },
        )
        group["weld_ids"].append(str(configuration.get("weld_id")))
        group["setup_time_min"] = max(
            float(group["setup_time_min"]),
            float(configuration.get("setup_time_min", 0.0)),
        )
        group["hourly_rate_eur"] = max(
            float(group["hourly_rate_eur"]),
            float(configuration.get("hourly_rate_eur", 0.0)),
        )

    total_setup = 0.0
    setup_cost_total = 0.0
    for group in setup_groups.values():
        total_setup += float(group["setup_time_min"])
        group["setup_cost_eur"] = _round_money(
            float(group["setup_time_min"]) / 60.0 * float(group["hourly_rate_eur"])
        )
        setup_cost_total += float(group["setup_cost_eur"])

    total_cost = 0.0
    for configuration, result in confirmed_for_setup:
        item_time = float(result["productive_time_total_min"])
        result["total_time_min"] = round(item_time, 4)
        result["cost_eur"] = _round_money(item_time / 60.0 * float(configuration["hourly_rate_eur"]))
        total_cost += float(result["cost_eur"])
    total_cost += setup_cost_total

    has_errors = bool(global_errors or pending_candidate_ids or any(item["errors"] for item in item_results))
    if not configurations and not candidates:
        status = "not_configured"
    else:
        status = "requires_configuration" if has_errors or not configurations else "calculated"
    warnings = [
        "Stima limitata alle saldature: non costituisce un preventivo completo dell'assemblato."
    ]
    if pending_candidate_ids:
        warnings.append(
            "Candidate ancora da confermare o rifiutare: " + ", ".join(pending_candidate_ids) + "."
        )
    warnings.extend(global_errors)
    complete = status == "calculated"
    return {
        "status": status,
        "scope": "welding_only",
        "quantity": quantity,
        "items": item_results,
        "setup_groups": list(setup_groups.values()),
        "productive_time_total_min": round(productive_time_total, 4) if complete and confirmed_for_setup else None,
        "setup_time_total_min": round(total_setup, 4) if complete and confirmed_for_setup else None,
        "total_time_min": round(productive_time_total + total_setup, 4) if complete and confirmed_for_setup else None,
        "total_cost_eur": _round_money(total_cost) if complete and confirmed_for_setup else None,
        "warnings": warnings,
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
    welds: list[dict[str, Any]] | None = None,
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
    classification = cad_data.get("part_classification", {}) or {}
    part_category = classification.get("category", "unknown")
    classification_reason = classification.get("reason") or "Classificazione geometrica non disponibile."
    quote_not_applicable = part_category in {"non_sheet_metal", "multi_solid"}
    quote_applicability = {
        "status": "not_applicable" if quote_not_applicable else (
            "applicable" if part_category == "sheet_metal" else "requires_review"
        ),
        "reason": (
            "Preventivo lamiera non applicabile: lo STEP contiene piu solidi/componenti; analizzarli singolarmente."
            if part_category == "multi_solid"
            else (
                "Preventivo lamiera non applicabile: il CAD e classificato come pezzo massivo/non lamiera."
                if quote_not_applicable
                else classification_reason
            )
        ),
    }
    quantity = max(int(quantity), 1)
    welding_quote = _calculate_welding_quote(
        cad_data,
        welds,
        quantity=quantity,
    )
    circular_holes = _feature_count(cad_data, "circular")
    elongated_holes = _feature_count(cad_data, "elongated")
    rounded_rectangular_holes = _feature_count(cad_data, "rounded_rectangular")
    polygonal_holes = _feature_count(cad_data, "polygonal")
    formed_holes = _feature_count(cad_data, "formed")
    unknown_holes = _feature_count(cad_data, "unknown")
    countersunk_holes = int(
        (cad_data.get("holes", {}) or {}).get(
            "countersunk_holes",
            sum(
                1
                for feature in (cad_data.get("holes", {}) or {}).get("circular", [])
                if feature.get("type") == "countersunk"
            ),
        )
    )
    total_holes = (
        circular_holes
        + elongated_holes
        + rounded_rectangular_holes
        + polygonal_holes
        + formed_holes
        + unknown_holes
    )
    physical_openings_total = int(
        (cad_data.get("holes", {}) or {}).get("physical_openings_total")
        or total_holes
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
    flat_pattern = cad_data.get("flat_pattern", {}) or {}
    if total_cut_length_mm is None:
        total_cut_length_mm = flat_pattern.get("total_cut_length_mm")
    flat_pattern_status = flat_pattern.get("status", "unavailable")
    flat_validation = flat_pattern.get("validation", {}) or {}
    flat_usable_for_costing = bool(flat_pattern.get("usable_for_costing")) and (
        flat_pattern_status in {"exact", "validated_estimate"}
        and bool(flat_validation.get("passed"))
    )
    laser_costing_available = not quote_not_applicable and flat_usable_for_costing
    if not laser_costing_available:
        total_cut_length_mm = None
    gross_blank_area_mm2 = flat_pattern.get("gross_blank_area_mm2")
    flat_pattern_confidence = flat_pattern.get("confidence", "low")
    thickness_mm = cad_data.get("detected_thickness_mm") or cad_data.get("declared_thickness_mm")
    part_weight_kg = None
    if material_config is not None and volume_cm3 is not None:
        part_weight_kg = round(float(volume_cm3) * material_config["density_g_cm3"] / 1000, 3)
    if (
        material_config is not None
        and gross_blank_area_mm2 is not None
        and thickness_mm is not None
        and flat_pattern_confidence in {"medium", "high"}
        and flat_usable_for_costing
    ):
        estimated_weight_kg = round(
            float(gross_blank_area_mm2)
            * float(thickness_mm)
            * material_config["density_g_cm3"]
            / 1_000_000,
            3,
        )
        weight_source = "flat_pattern_gross_blank"
    elif part_weight_kg is not None and quote_not_applicable:
        estimated_weight_kg = part_weight_kg
        weight_source = "recalculated_from_volume"
    else:
        estimated_weight_kg = None
        weight_source = None
    warnings = [quote_applicability["reason"]] if quote_not_applicable else [
        "Preventivo preliminare: parametri economici caricati da config e da validare con dati aziendali reali.",
        "Il motore non applica margine e non decide il prezzo finale commerciale.",
    ]
    if not quote_not_applicable and material_config is None:
        warnings.append("Materiale non presente in config/materials.json: costo materiale non calcolabile in modo affidabile.")
    if not quote_not_applicable and estimated_weight_kg is None:
        warnings.append("Peso grezzo non disponibile: lo sviluppo piano non e validato per il costing.")
    if not quote_not_applicable and not flat_usable_for_costing:
        warnings.append(
            "Costo laser non disponibile: sviluppo piano exact/validated_estimate non disponibile; nessun fallback euristico applicato."
        )
    if not quote_not_applicable and material_config is not None and volume_cm3 is None:
        warnings.append("Volume CAD non disponibile: peso materiale mantenuto dalla stima CAD originale.")
    if not quote_not_applicable and weight_source == "flat_pattern_gross_blank":
        warnings.append(
            "Costo materiale calcolato sul peso del grezzo sviluppato; dimensioni e sfrido richiedono verifica produttiva."
        )
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
        polygonal_holes + rounded_rectangular_holes,
        formed_holes,
        unknown_holes,
        bends,
    )
    amounts = _not_applicable_amounts(quote_applicability["reason"]) if quote_not_applicable else _estimate_amounts(
        quantity=quantity,
        circular_holes=circular_holes,
        elongated_holes=elongated_holes,
        # A rounded rectangle uses the existing non-circular opening effort;
        # P1 intentionally introduces no new economic coefficient.
        polygonal_holes=polygonal_holes + rounded_rectangular_holes,
        formed_holes=formed_holes,
        unknown_holes=unknown_holes,
        total_holes=physical_openings_total,
        bends=bends,
        bends_count_available=bends_count_available,
        estimated_weight_kg=estimated_weight_kg,
        material_config=material_config,
        parameters=parameters,
        total_cut_length_mm=total_cut_length_mm,
        laser_costing_available=laser_costing_available,
    )
    quantity_breakdown = []
    for break_quantity in (() if quote_not_applicable else STANDARD_QUANTITY_BREAKS):
        break_amounts = _estimate_amounts(
            quantity=break_quantity,
            circular_holes=circular_holes,
            elongated_holes=elongated_holes,
            polygonal_holes=polygonal_holes + rounded_rectangular_holes,
            formed_holes=formed_holes,
            unknown_holes=unknown_holes,
            total_holes=physical_openings_total,
            bends=bends,
            bends_count_available=bends_count_available,
            estimated_weight_kg=estimated_weight_kg,
            material_config=material_config,
            parameters=parameters,
            total_cut_length_mm=total_cut_length_mm,
            laser_costing_available=laser_costing_available,
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
        "process_plan": [] if quote_not_applicable else _process_plan(bends),
        "quote_applicability": quote_applicability,
        "laser_applicability": {
            "status": "applicable" if laser_costing_available else "not_available",
            "reason": (
                "Sviluppo piano geometricamente validato."
                if laser_costing_available
                else "Sviluppo piano non validato per il costing; fallback laser disabilitato."
            ),
        },
        "welding_quote": welding_quote,
        "part_classification": classification,
        "material": {
            "name": material_name,
            "density_g_cm3": material_config["density_g_cm3"] if material_config else None,
            "cost_eur_kg": material_config["cost_eur_kg"] if material_config else None,
            "estimated_weight_kg": estimated_weight_kg,
            "part_weight_kg": part_weight_kg,
            "blank_weight_kg": estimated_weight_kg if weight_source == "flat_pattern_gross_blank" else None,
            "weight_source": weight_source,
            "thickness_mm": thickness_mm,
        },
        "features_summary": {
            "circular_holes": circular_holes,
            "countersunk_holes": countersunk_holes,
            "elongated_holes": elongated_holes,
            "rounded_rectangular_holes": rounded_rectangular_holes,
            "polygonal_holes": polygonal_holes,
            "formed_holes": formed_holes,
            "unknown_holes": unknown_holes,
            "total_holes": total_holes,
            "physical_openings_total": physical_openings_total,
            "bends": bends,
        },
        "cost_drivers": {
            "complexity": (
                "high"
                if cad_data.get("complexity_score") == "high"
                else complexity
            ),
            "laser_cutting_complexity": "not_applicable" if quote_not_applicable else (
                "medium: profilo lamiera con fori circolari, asole e fori poligonali"
                if (
                    circular_holes
                    + elongated_holes
                    + rounded_rectangular_holes
                    + polygonal_holes
                    + unknown_holes
                    >= 6
                )
                else "low: geometria semplice"
            ),
            "bending_complexity": "not_applicable" if quote_not_applicable else (
                "high: molte pieghe rilevate, verifica tecnica richiesta"
                if bends >= 8
                else (
                    f"medium: {bends} flange semplici da piegare"
                    if bends > 0
                    else "low: nessuna piega rilevata"
                )
            ),
            "setup_required": not quote_not_applicable,
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
