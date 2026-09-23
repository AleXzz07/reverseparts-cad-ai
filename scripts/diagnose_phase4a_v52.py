#!/usr/bin/env python3
"""Read-only FreeCAD diagnostics for the five Phase 4A v5.1 regressions.

The script wraps existing analyzer functions only to record their inputs and
outputs.  It does not replace thresholds, alter decisions, or write STEP files.
Run it inside the project Docker image so the observed path is the real
FreeCAD/OCC path used by the failing tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.cad_analyzer as cad  # noqa: E402


CASES = {
    "staffa_16_pieghe_stress_test": PROJECT_ROOT
    / "tests/dataset/staffa_16_pieghe_stress_test/input.stp",
    "moderate_part_1": PROJECT_ROOT
    / "tests/test_files/real_openings_phase4a/SWPR-moderate sheetmetal part 1.STEP",
    "validation_05_staffa_2_pieghe_non_parallele": PROJECT_ROOT
    / "tests/dataset/validation_05_staffa_2_pieghe_non_parallele/input.stp",
    "validation_13_piastra_foro_svasato": PROJECT_ROOT
    / "tests/dataset/validation_13_piastra_foro_svasato/input.stp",
    "correct_and_flatten": PROJECT_ROOT
    / "tests/test_files/real_openings_phase4a/"
    "SWPR-Moderate-Correct-and-Flatten-Imported-Sheet-Metal.STEP",
}


V4_REFERENCE = {
    "staffa_16_pieghe_stress_test": {
        "total_holes": 12,
        "formed_holes": 1,
        "note": "Phase 4A v4 validated baseline",
    },
    "moderate_part_1": {
        "total_holes": 10,
        "through_bend_slot": "12.70 x 6.35 mm",
        "note": "Phase 4A v4 validated baseline",
    },
    "validation_05_staffa_2_pieghe_non_parallele": {
        "flat_status": ["exact", "validated_estimate"],
        "note": "validated dataset baseline before v5 safety gate",
    },
    "validation_13_piastra_foro_svasato": {
        "flat_status": "exact",
        "physical_openings": 2,
        "countersunk_holes": 1,
        "note": "validated dataset baseline before v5 safety gate",
    },
    "correct_and_flatten": {
        "expected_current_geometry": {
            "thickness_mm": 0.76,
            "holes": 2,
            "lances": 2,
            "detected_bends": 9,
            "flat_status": "partial",
            "usable_for_costing": False,
        },
        "independent_audit": {
            "physical_bend_count": 11,
            "production_graph_zone_count": 9,
            "physical_hems_not_yet_graph_nodes": 2,
            "note": "The two missing hems remain future Phase 4B work.",
        },
    },
}


def _round(value: Any, digits: int = 6) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return round(number, digits) if math.isfinite(number) else str(number)


def _vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    try:
        if isinstance(value, (list, tuple)):
            parts = value[:3]
        else:
            parts = [
                getattr(value, lower)
                if hasattr(value, lower)
                else getattr(value, upper)
                for lower, upper in (("x", "X"), ("y", "Y"), ("z", "Z"))
            ]
        return [_round(part) for part in parts]
    except (AttributeError, TypeError, ValueError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return value


def _face_index(context: Any, face: Any) -> int | None:
    if context is None or face is None:
        return None
    for index, candidate in enumerate(getattr(context, "faces", ()) or (), start=1):
        if cad._topology_same(face, candidate):
            return index
    return None


def _edge_summary(edge: Any) -> dict[str, Any]:
    return {
        "hash": cad._topology_hash(edge),
        "curve_type": cad._curve_type(edge),
        "length_mm": _round(getattr(edge, "Length", None)),
    }


def _contour_summary(contour: Any) -> dict[str, Any]:
    return {
        "key": f"face_{contour.face_index}_wire_{contour.wire_index}",
        "face_index": contour.face_index,
        "wire_index": contour.wire_index,
        "component_id": contour.component_id,
        "panel_id": contour.panel_id,
        "center": _vector(contour.center),
        "normal": _vector(contour.normal),
        "plane_offset": _round(contour.plane_offset),
        "perimeter_mm": _round(contour.perimeter_mm),
        "bbox_dimensions_mm": [_round(value) for value in contour.bbox_dimensions_mm],
        "edge_types": list(contour.edge_types),
        "wall_face_indices": list(contour.wall_face_indices),
        "edge_hashes": [
            cad._topology_hash(edge)
            for edge in getattr(contour.wire, "Edges", ()) or ()
        ],
    }


def _identity_summary(identity: Any, context: Any = None) -> dict[str, Any]:
    return {
        "id": identity.id,
        "feature_kind": identity.feature_kind,
        "evidence": identity.evidence,
        "confidence": identity.confidence,
        "component_id": identity.component_id,
        "panel_id": identity.panel_id,
        "panel_ids": list(identity.panel_ids),
        "bend_zone_ids": list(identity.bend_zone_ids),
        "center": _vector(identity.center),
        "axis": _vector(identity.axis),
        "contours": [_contour_summary(contour) for contour in identity.contours],
        "wall_face_indices": [
            _face_index(context, face) for face in identity.wall_faces
        ],
        "source_face_indices": list(identity.source_face_indices),
        "developed_category": identity.developed_category,
        "forming_depth_mm": _round(identity.forming_depth_mm),
        "forming_length_mm": _round(identity.forming_length_mm),
        "forming_width_mm": _round(identity.forming_width_mm),
        "forming_cut_length_mm": _round(identity.forming_cut_length_mm),
        "forming_connected_edge_length_mm": _round(
            identity.forming_connected_edge_length_mm
        ),
    }


def _feature_summary(feature: Any) -> dict[str, Any]:
    fields = (
        "feature_id",
        "type",
        "reason",
        "diameter_mm",
        "through_diameter_mm",
        "countersink_major_diameter_mm",
        "countersink_depth_mm",
        "overall_length_mm",
        "straight_length_mm",
        "length_mm",
        "width_mm",
        "max_dimension_mm",
        "num_sides",
        "perimeter_mm",
        "area_mm2",
        "depth_mm",
        "confidence",
    )
    result = {name: _round(getattr(feature, name, None)) for name in fields}
    result["center"] = _vector(getattr(feature, "center", None))
    result["axis"] = _vector(getattr(feature, "axis", None))
    result["orientation_axis"] = _vector(
        getattr(feature, "orientation_axis", None)
    )
    return result


def _bend_summary(bend: Any) -> dict[str, Any]:
    return {
        "radius_mm": _round(getattr(bend, "radius_mm", None)),
        "angle_deg": _round(getattr(bend, "angle_deg", None)),
        "length_mm": _round(getattr(bend, "length_mm", None)),
        "axis": _vector(getattr(bend, "axis", None)),
        "center": _vector(getattr(bend, "center", None)),
        "confidence": getattr(bend, "confidence", None),
    }


def _zone_summary(zone: Any, context: Any) -> dict[str, Any]:
    return {
        "id": zone.id,
        "inner_face_index": _face_index(context, zone.inner_face),
        "outer_face_index": _face_index(context, zone.outer_face),
        "panel_ids": list(zone.panel_ids),
        "inner_radius_mm": _round(zone.inner_radius_mm),
        "angle_deg": _round(zone.angle_deg),
        "length_mm": _round(zone.length_mm),
        "axis": _vector(zone.axis),
        "center": _vector(zone.center),
        "direct": bool(zone.direct),
    }


def _thickness_audit(shape: Any, declared: float | None, parameters: Any) -> dict[str, Any]:
    faces = list(getattr(shape, "Faces", ()) or ())
    adjacency = cad._face_adjacency_from_shared_edges(faces)
    planes: list[dict[str, Any]] = []
    for face_index, face in enumerate(faces):
        surface = getattr(face, "Surface", None)
        if getattr(surface, "TypeId", "") != "Part::GeomPlane":
            continue
        normal = cad._normalize_vector(surface.Axis)
        planes.append(
            {
                "face_index_zero_based": face_index,
                "face_index": face_index + 1,
                "area_mm2": float(face.Area),
                "normal": normal,
                "offset": cad._plane_offset(normal, cad._vector_tuple(surface.Position)),
            }
        )
    try:
        thin_solid = 2.0 * float(shape.Volume) / float(shape.Area)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        thin_solid = 0.0
    if not math.isfinite(thin_solid) or thin_solid <= 0:
        thin_solid = 0.0

    candidates: list[dict[str, Any]] = []
    grouped: dict[float, dict[str, float]] = {}
    for left_index, left in enumerate(planes):
        for right in planes[left_index + 1 :]:
            alignment = cad._dot(left["normal"], right["normal"])
            if abs(alignment) < 0.98:
                continue
            distance = (
                abs(left["offset"] - right["offset"])
                if alignment > 0
                else abs(left["offset"] + right["offset"])
            )
            if not parameters.sheet_thickness_min_mm <= distance <= parameters.sheet_thickness_max_mm:
                continue
            area_ratio = min(left["area_mm2"], right["area_mm2"]) / max(
                left["area_mm2"], right["area_mm2"]
            )
            if area_ratio < 0.85:
                continue
            common = adjacency[left["face_index_zero_based"]] & adjacency[
                right["face_index_zero_based"]
            ]
            value = round(distance, 2)
            support_area = min(left["area_mm2"], right["area_mm2"])
            record = {
                "value_mm": value,
                "left_face_index": left["face_index"],
                "right_face_index": right["face_index"],
                "area_ratio": _round(area_ratio),
                "support_area_mm2": _round(support_area),
                "alignment_abs": _round(abs(alignment)),
                "direct_wall_evidence": bool(common),
                "common_neighbour_face_indices": sorted(index + 1 for index in common),
            }
            candidates.append(record)
            group = grouped.setdefault(
                value,
                {
                    "count": 0.0,
                    "support_area": 0.0,
                    "topological_count": 0.0,
                    "topological_support_area": 0.0,
                },
            )
            group["count"] += 1.0
            group["support_area"] += support_area
            if common:
                group["topological_count"] += 1.0
                group["topological_support_area"] += support_area

    group_records: list[dict[str, Any]] = []
    for value, evidence in grouped.items():
        relative_error = (
            abs(value - thin_solid) / thin_solid if thin_solid > 0 else None
        )
        declared_match = bool(
            declared is not None
            and abs(value - float(declared)) <= max(0.1, value * 0.1)
        )
        coherent = bool(
            thin_solid <= 0
            or (relative_error is not None and relative_error <= parameters.sheet_thickness_max_thin_solid_relative_error)
            or declared_match
        )
        rank = (
            evidence["support_area"],
            evidence["count"],
            evidence["topological_support_area"],
            evidence["topological_count"],
            float(declared_match),
            -(relative_error or 0.0),
        )
        group_records.append(
            {
                "value_mm": value,
                "count": int(evidence["count"]),
                "support_area_mm2": _round(evidence["support_area"]),
                "topological_count": int(evidence["topological_count"]),
                "topological_support_area_mm2": _round(
                    evidence["topological_support_area"]
                ),
                "thin_solid_relative_error": _round(relative_error),
                "declared_match": declared_match,
                "coherent": coherent,
                "rank_tuple": [_round(item) for item in rank],
            }
        )
    group_records.sort(key=lambda item: item["rank_tuple"], reverse=True)
    candidates.sort(
        key=lambda item: (
            item["value_mm"],
            item["left_face_index"],
            item["right_face_index"],
        )
    )
    return {
        "thin_solid_2v_over_a_mm": _round(thin_solid),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "groups_in_rank_order": group_records,
    }


def _opening_completeness_audit(
    context: Any,
    holes: list[Any],
    parameters: Any,
    topology_context: Any = None,
) -> dict[str, Any]:
    if context is None:
        return {"result": True, "reason": "no opening context"}
    represented = {
        (contour.face_index, contour.wire_index)
        for identity in (*context.identities, *context.forming_identities)
        for contour in identity.contours
    }
    raw_unresolved = [
        contour
        for contour in context.raw_contours
        if (contour.face_index, contour.wire_index) not in represented
    ]
    semantic: list[dict[str, Any]] = []
    final_unresolved: list[Any] = []
    for contour in raw_unresolved:
        countersink_matches = [
            hole
            for hole in holes
            if cad._contour_matches_confirmed_countersink_profile(
                contour, hole, parameters
            )
        ]
        bend_transition = cad._contour_is_bend_transition_boundary(
            contour,
            topology_context,
            parameters,
        )
        semantic.append(
            {
                "contour": _contour_summary(contour),
                "confirmed_countersink_profile_matches": [
                    _feature_summary(hole) for hole in countersink_matches
                ],
                "bend_transition_boundary": bend_transition,
            }
        )
        if not countersink_matches and not bend_transition:
            final_unresolved.append(contour)
    return {
        "represented_contour_keys": [
            f"face_{face}_wire_{wire}" for face, wire in sorted(represented)
        ],
        "raw_unresolved": [_contour_summary(item) for item in raw_unresolved],
        "semantic_resolution": semantic,
        "final_unresolved": [
            _contour_summary(item) for item in final_unresolved
        ],
        "result": not final_unresolved,
    }


def _trace_lance_candidate(
    left: Any,
    right: Any,
    topology_context: Any,
    component_face_indices: set[int],
    wall_by_index: dict[int, Any],
) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "contours": [
            f"face_{left.face_index}_wire_{left.wire_index}",
            f"face_{right.face_index}_wire_{right.wire_index}",
        ],
        "component_wall_face_indices": sorted(component_face_indices),
    }
    if topology_context is None or not component_face_indices:
        trace.update(result="rejected", first_failed_gate="missing_topology_or_wall_component")
        return trace

    def contacts(contour: Any) -> dict[str, list[Any]]:
        result: dict[str, list[Any]] = {}
        for bend in topology_context.bends:
            matched: list[Any] = []
            for bend_face in (bend.inner_face, bend.outer_face):
                if bend_face is None:
                    continue
                for wire_edge in getattr(contour.wire, "Edges", ()) or ():
                    if any(
                        cad._topology_same(wire_edge, bend_edge)
                        for bend_edge in getattr(bend_face, "Edges", ()) or ()
                    ) and not any(cad._topology_same(wire_edge, item) for item in matched):
                        matched.append(wire_edge)
            if matched:
                result[bend.id] = matched
        return result

    left_contacts = contacts(left)
    right_contacts = contacts(right)
    shared = sorted(set(left_contacts) & set(right_contacts))
    trace["left_bend_contacts"] = {
        key: [_edge_summary(edge) for edge in value]
        for key, value in left_contacts.items()
    }
    trace["right_bend_contacts"] = {
        key: [_edge_summary(edge) for edge in value]
        for key, value in right_contacts.items()
    }
    trace["shared_bend_ids"] = shared
    if len(shared) != 1:
        trace.update(result="rejected", first_failed_gate="shared_bend_count_not_one")
        return trace
    bend = next(item for item in topology_context.bends if item.id == shared[0])
    trace["bend_panel_ids"] = list(bend.panel_ids)
    if len(bend.panel_ids) != 2 or left.panel_id not in bend.panel_ids:
        trace.update(result="rejected", first_failed_gate="bend_panel_membership")
        return trace
    wall_edges = [
        edge
        for index in component_face_indices
        for edge in getattr(wall_by_index.get(index), "Edges", ()) or ()
    ]
    contact_edges = (*left_contacts[bend.id], *right_contacts[bend.id])
    shared_with_wall = [
        _edge_summary(contact)
        for contact in contact_edges
        if any(cad._topology_same(contact, wall) for wall in wall_edges)
    ]
    trace["bend_contact_edges_also_in_through_wall"] = shared_with_wall
    if shared_with_wall:
        trace.update(result="rejected", first_failed_gate="bend_contact_is_through_wall")
        return trace
    left_length = sum(float(edge.Length) for edge in left_contacts[bend.id])
    right_length = sum(float(edge.Length) for edge in right_contacts[bend.id])
    tolerance = max(
        float(topology_context.parameters.flat_pattern_edge_match_tolerance_mm),
        max(left_length, right_length) * 0.02,
    )
    trace.update(
        left_connected_mm=_round(left_length),
        right_connected_mm=_round(right_length),
        connected_tolerance_mm=_round(tolerance),
        left_cut_mm=_round(left.perimeter_mm - left_length),
        right_cut_mm=_round(right.perimeter_mm - right_length),
    )
    if left_length <= tolerance or right_length <= tolerance or abs(left_length - right_length) > tolerance:
        trace.update(result="rejected", first_failed_gate="connected_edge_length")
        return trace
    if min(left.perimeter_mm - left_length, right.perimeter_mm - right_length) <= tolerance:
        trace.update(result="rejected", first_failed_gate="cut_length_not_positive")
        return trace
    dimensions = cad._wire_local_planar_dimensions(left.wire, left.normal)
    if dimensions is None:
        trace.update(result="rejected", first_failed_gate="local_dimensions_unavailable")
        return trace
    trace.update(
        result="eligible",
        first_failed_gate=None,
        dimensions_mm=[_round(item) for item in dimensions],
    )
    return trace


def _trace_through_bend_candidate(
    component_face_indices: set[int],
    wall_by_index: dict[int, Any],
    topology_context: Any,
    parameters: Any,
    thickness_mm: float | None,
) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "component_wall_face_indices": sorted(component_face_indices),
        "thickness_mm": _round(thickness_mm),
    }
    if thickness_mm is None or thickness_mm <= 0:
        trace.update(result="rejected", first_failed_gate="thickness_unavailable")
        return trace
    solids = list(getattr(getattr(topology_context, "shape", None), "Solids", ()) or ())
    trace["solid_count"] = len(solids)
    if solids and len(solids) != 1:
        trace.update(result="rejected", first_failed_gate="solid_count_not_one")
        return trace
    component_faces = [wall_by_index[index] for index in sorted(component_face_indices)]
    if not component_faces:
        trace.update(result="rejected", first_failed_gate="empty_wall_component")
        return trace
    shared_edges = getattr(topology_context, "shared_edges", cad._shared_topology_edges)
    touched_panels: list[tuple[Any, tuple[list[Any], list[Any]]]] = []
    panel_trace: list[dict[str, Any]] = []
    for panel in topology_context.panels:
        per_skin = tuple(
            [
                edge
                for wall_face in component_faces
                for edge in shared_edges(wall_face, skin_face)
            ]
            for skin_face in panel.faces
        )
        if any(per_skin):
            panel_trace.append(
                {
                    "panel_id": panel.id,
                    "per_skin_shared_edge_counts": [len(items) for items in per_skin],
                    "skin_face_indices": [
                        _face_index(topology_context, face) for face in panel.faces
                    ],
                }
            )
            if not all(per_skin):
                trace["touched_panels"] = panel_trace
                trace.update(result="rejected", first_failed_gate="panel_only_one_skin")
                return trace
            touched_panels.append((panel, per_skin))
    trace["touched_panels"] = panel_trace
    if len(touched_panels) != 2:
        trace.update(result="rejected", first_failed_gate="touched_panel_count_not_two")
        return trace
    panel_ids = tuple(sorted(panel.id for panel, _ in touched_panels))
    touched_bends: list[Any] = []
    bend_boundary_edges: list[Any] = []
    bend_trace: list[dict[str, Any]] = []
    for bend in topology_context.bends:
        inner = [
            edge
            for wall_face in component_faces
            for edge in shared_edges(wall_face, bend.inner_face)
        ]
        outer = (
            [
                edge
                for wall_face in component_faces
                for edge in shared_edges(wall_face, bend.outer_face)
            ]
            if bend.outer_face is not None
            else []
        )
        if inner or outer:
            bend_trace.append(
                {
                    "bend_id": bend.id,
                    "inner_edge_count": len(inner),
                    "outer_edge_count": len(outer),
                    "bend_panel_ids": sorted(bend.panel_ids),
                    "required_panel_ids": list(panel_ids),
                }
            )
            if not inner or not outer:
                trace["touched_bends"] = bend_trace
                trace.update(result="rejected", first_failed_gate="bend_only_one_skin")
                return trace
            if tuple(sorted(bend.panel_ids)) != panel_ids:
                trace["touched_bends"] = bend_trace
                trace.update(result="rejected", first_failed_gate="bend_panel_pair_mismatch")
                return trace
            touched_bends.append(bend)
            bend_boundary_edges.extend(inner)
            bend_boundary_edges.extend(outer)
    trace["touched_bends"] = bend_trace
    if not cad._bend_zones_are_geometrically_compatible(touched_bends, parameters):
        trace.update(result="rejected", first_failed_gate="bend_zones_incompatible")
        return trace

    panel_boundary_edges = [
        edge
        for _, per_skin in touched_panels
        for skin_edges in per_skin
        for edge in skin_edges
    ]
    allowed = [*panel_boundary_edges, *bend_boundary_edges]
    buckets: dict[int, list[tuple[int, Any]]] = {}
    for face_index, wall_face in enumerate(component_faces):
        for edge in getattr(wall_face, "Edges", ()) or ():
            buckets.setdefault(cad._topology_hash(edge), []).append((face_index, edge))
    boundary: list[Any] = []
    for bucket in buckets.values():
        consumed: set[int] = set()
        for position, (face_index, edge) in enumerate(bucket):
            if position in consumed:
                continue
            matches = {
                other_position
                for other_position, (other_face_index, other_edge) in enumerate(bucket)
                if cad._topology_same(edge, other_edge) and other_face_index != face_index
            }
            consumed.update(matches)
            if matches:
                continue
            if not any(cad._topology_same(edge, item) for item in allowed):
                trace["unallowed_boundary_edge"] = _edge_summary(edge)
                trace.update(result="rejected", first_failed_gate="open_or_foreign_boundary_edge")
                return trace
            if not any(cad._topology_same(edge, item) for item in boundary):
                boundary.append(edge)
    loops = cad._closed_topology_edge_loops(boundary)
    trace["actual_boundary_edge_count"] = len(boundary)
    trace["boundary_loop_count"] = None if loops is None else len(loops)
    if loops is None or len(loops) != 2:
        trace.update(result="rejected", first_failed_gate="boundary_not_two_closed_loops")
        return trace

    def is_bend_edge(edge: Any) -> bool:
        return any(cad._topology_same(edge, item) for item in bend_boundary_edges)

    cap_radii: list[float] = []
    loop_trace: list[dict[str, Any]] = []
    for loop in loops:
        panel_edges = [edge for edge in loop if not is_bend_edge(edge)]
        types = [cad._curve_type(edge) for edge in panel_edges]
        circles = [edge for edge in panel_edges if cad._curve_type(edge) == "Part::GeomCircle"]
        lines = [edge for edge in panel_edges if cad._curve_type(edge) == "Part::GeomLine"]
        item = {
            "perimeter_mm": _round(sum(float(edge.Length) for edge in loop)),
            "panel_edge_types": types,
            "circle_count": len(circles),
            "line_count": len(lines),
        }
        loop_trace.append(item)
        if any(kind not in {"Part::GeomLine", "Part::GeomCircle"} for kind in types):
            trace["loops"] = loop_trace
            trace.update(result="rejected", first_failed_gate="unsupported_loop_curve")
            return trace
        if len(circles) < 2 or len(lines) < 2:
            trace["loops"] = loop_trace
            trace.update(result="rejected", first_failed_gate="slot_profile_edge_counts")
            return trace
        radii = [float(edge.Curve.Radius) for edge in circles]
        circular_length = sum(float(edge.Length) for edge in circles)
        radius = sum(radii) / len(radii)
        item.update(radii_mm=[_round(value) for value in radii], circular_length_mm=_round(circular_length))
        if max(radii) - min(radii) > parameters.hole_diameter_tolerance_mm / 2.0:
            trace["loops"] = loop_trace
            trace.update(result="rejected", first_failed_gate="loop_radius_mismatch")
            return trace
        tolerance = max(
            parameters.hole_diameter_tolerance_mm * math.pi,
            parameters.flat_pattern_continuity_tolerance_mm * len(circles),
        )
        if abs(circular_length - 2.0 * math.pi * radius) > tolerance:
            trace["loops"] = loop_trace
            trace.update(result="rejected", first_failed_gate="circle_arc_not_full_cap")
            return trace
        cap_radii.append(radius)
    trace["loops"] = loop_trace
    if max(cap_radii) - min(cap_radii) > parameters.hole_diameter_tolerance_mm / 2.0:
        trace.update(result="rejected", first_failed_gate="opposite_cap_radius_mismatch")
        return trace
    trace.update(result="eligible", first_failed_gate=None)
    return trace


class Hooks:
    def __init__(self) -> None:
        self.originals: dict[str, Any] = {}
        self.current: dict[str, Any] | None = None

    def _wrap(self, name: str, replacement: Any) -> None:
        self.originals[name] = getattr(cad, name)
        setattr(cad, name, replacement)

    def install(self) -> None:
        original_thickness = cad._detect_sheet_thickness

        def thickness(shape: Any, declared_thickness_mm: float | None = None, parameters: Any = None):
            params = parameters or cad.load_analysis_config()
            audit = _thickness_audit(shape, declared_thickness_mm, params)
            result = original_thickness(shape, declared_thickness_mm, params)
            audit["selected_mm"] = _round(result[0])
            audit["selected_confidence"] = result[1]
            if self.current is not None:
                self.current["thickness"] = audit
            return result

        self._wrap("_detect_sheet_thickness", thickness)

        original_topology = cad.build_sheet_topology_context

        def topology(shape: Any, thickness_mm: float, k_factor: float, parameters: Any):
            context = original_topology(shape, thickness_mm, k_factor, parameters)
            if self.current is not None:
                self.current["_topology_object"] = context
                self.current["topology"] = {
                    "panel_count": len(context.panels),
                    "bend_zone_count": len(context.bends),
                    "graph_connected": bool(context.graph and context.graph.connected),
                    "graph_direct": bool(context.graph and context.graph.direct),
                    "graph_warnings": list(context.graph.warnings if context.graph else []),
                    "panels": [
                        {
                            "id": panel.id,
                            "skin_face_indices": [
                                _face_index(context, face) for face in panel.faces
                            ],
                            "area_mm2": _round(panel.area_mm2),
                            "center": _vector(panel.center),
                            "normal": _vector(panel.normal),
                        }
                        for panel in context.panels
                    ],
                    "bend_zones": [
                        _zone_summary(zone, context) for zone in context.bends
                    ],
                }
            return context

        self._wrap("build_sheet_topology_context", topology)

        original_lance = cad._lance_tab_forming_identity

        def lance(left: Any, right: Any, **kwargs: Any):
            trace = _trace_lance_candidate(
                left,
                right,
                kwargs.get("topology_context"),
                kwargs.get("component_face_indices", set()),
                kwargs.get("wall_by_index", {}),
            )
            result = original_lance(left, right, **kwargs)
            trace["engine_result"] = (
                "accepted" if result is not None else "rejected"
            )
            if result is not None:
                trace["identity"] = _identity_summary(
                    result, kwargs.get("topology_context")
                )
            if self.current is not None:
                self.current.setdefault("lance_candidate_traces", []).append(trace)
            return result

        self._wrap("_lance_tab_forming_identity", lance)

        original_through_bend = cad._through_bend_opening_identity

        def through_bend(
            component_face_indices: set[int],
            wall_by_index: dict[int, Any],
            topology_context: Any,
            parameters: Any,
            thickness_mm: float | None,
            component_id: str,
        ):
            trace = _trace_through_bend_candidate(
                component_face_indices,
                wall_by_index,
                topology_context,
                parameters,
                thickness_mm,
            )
            result = original_through_bend(
                component_face_indices,
                wall_by_index,
                topology_context,
                parameters,
                thickness_mm,
                component_id,
            )
            trace["engine_result"] = (
                "accepted" if result is not None else "rejected"
            )
            if result is not None:
                trace["identity"] = _identity_summary(result, topology_context)
            if self.current is not None:
                self.current.setdefault("through_bend_candidate_traces", []).append(trace)
            return result

        self._wrap("_through_bend_opening_identity", through_bend)

        original_opening_context = cad._build_physical_opening_context

        def opening_context(*args: Any, **kwargs: Any):
            context = original_opening_context(*args, **kwargs)
            topology_context = kwargs.get("topology_context")
            if self.current is not None:
                self.current["_opening_context_object"] = context
                self.current["openings"] = {
                    "raw_contour_count": context.raw_contour_count,
                    "raw_contours": [
                        _contour_summary(contour) for contour in context.raw_contours
                    ],
                    "accepted_merges": context.accepted_merges,
                    "rejected_merges": context.rejected_merges,
                    "physical_identities": [
                        _identity_summary(identity, topology_context)
                        for identity in context.identities
                    ],
                    "forming_identities": [
                        _identity_summary(identity, topology_context)
                        for identity in context.forming_identities
                    ],
                }
            return context

        self._wrap("_build_physical_opening_context", opening_context)

        original_classify = cad._classify_physical_opening

        def classify(identity: Any, parameters: Any, thickness_mm: float | None):
            result = original_classify(identity, parameters, thickness_mm)
            if self.current is not None:
                self.current.setdefault("opening_classification", []).append(
                    {
                        "identity_id": identity.id,
                        "identity_evidence": identity.evidence,
                        "category": result[0],
                        "feature": _feature_summary(result[1]),
                        "raw_cylindrical_evidence": result[2],
                    }
                )
            return result

        self._wrap("_classify_physical_opening", classify)

        original_completeness = cad._opening_context_is_complete

        def completeness(
            context: Any,
            holes: list[Any] | None = None,
            parameters: Any = None,
            topology_context: Any = None,
        ):
            result = original_completeness(
                context,
                holes,
                parameters,
                topology_context,
            )
            if self.current is not None:
                audit = _opening_completeness_audit(
                    context,
                    holes or [],
                    parameters or cad.load_analysis_config(),
                    topology_context,
                )
                audit["engine_result"] = result
                self.current["opening_completeness_gate"] = audit
            return result

        self._wrap("_opening_context_is_complete", completeness)

        original_bends = cad._detect_bends

        def bends(*args: Any, **kwargs: Any):
            result = original_bends(*args, **kwargs)
            if self.current is not None:
                self.current["detected_bends"] = [
                    _bend_summary(item) for item in result
                ]
            return result

        self._wrap("_detect_bends", bends)

        original_unfold = cad.unfold_sheet

        def unfold(**kwargs: Any):
            result = original_unfold(**kwargs)
            if self.current is not None:
                self.current["flat_before_post_unfold_gates"] = _model_dump(result)
            return result

        self._wrap("unfold_sheet", unfold)

        original_unmatched = cad._unmatched_topology_bend_zones

        def unmatched(context: Any, bends_value: list[Any], thickness_mm: float, parameters: Any):
            result = original_unmatched(context, bends_value, thickness_mm, parameters)
            if self.current is not None:
                matrix = []
                for zone in getattr(context, "bends", ()) or ():
                    matrix.append(
                        {
                            "zone": _zone_summary(zone, context),
                            "matches": [
                                {
                                    "bend_index": index,
                                    "bend": _bend_summary(bend),
                                    "matched": cad._topology_zone_matches_detected_bend(
                                        zone, bend, thickness_mm, parameters
                                    ),
                                }
                                for index, bend in enumerate(bends_value, start=1)
                            ],
                        }
                    )
                self.current["bend_zone_coverage_gate"] = {
                    "zone_count": len(getattr(context, "bends", ()) or ()),
                    "detected_bend_count": len(bends_value),
                    "unmatched_zone_ids": [zone.id for zone in result],
                    "coverage_matrix": matrix,
                }
            return result

        self._wrap("_unmatched_topology_bend_zones", unmatched)

        original_planarity = cad._flat_planarity_gate

        def planarity(*args: Any, **kwargs: Any):
            result = original_planarity(*args, **kwargs)
            if self.current is not None:
                self.current["zero_bend_planarity_gate"] = {
                    "passed": result[0],
                    "reason": result[1],
                }
            return result

        self._wrap("_flat_planarity_gate", planarity)

        original_estimate = cad._estimate_flat_pattern

        def estimate(**kwargs: Any):
            if self.current is not None:
                self.current["flat_gate_inputs"] = {
                    "thickness_mm": _round(kwargs.get("thickness_mm")),
                    "detected_bend_count": len(kwargs.get("bends") or []),
                    "opening_count": len(kwargs.get("holes") or []),
                    "opening_identity_complete": kwargs.get(
                        "opening_identity_complete"
                    ),
                    "part_category": kwargs.get("part_category"),
                }
            result = original_estimate(**kwargs)
            if self.current is not None:
                self.current["flat_after_all_gates"] = _model_dump(result)
            return result

        self._wrap("_estimate_flat_pattern", estimate)

    def restore(self) -> None:
        for name, original in self.originals.items():
            setattr(cad, name, original)


def _final_summary(result: Any) -> dict[str, Any]:
    holes = result.holes
    all_features = {
        "circular": holes.circular,
        "elongated": holes.elongated,
        "rounded_rectangular": holes.rounded_rectangular,
        "polygonal": holes.polygonal,
        "formed": holes.formed,
        "unknown": holes.unknown,
    }
    return {
        "detected_thickness_mm": result.detected_thickness_mm,
        "thickness_confidence": result.thickness_confidence,
        "classification": _model_dump(result.part_classification),
        "geometry": _model_dump(result.geometry),
        "holes": {
            "total_holes": holes.total_holes,
            "physical_openings_total": holes.physical_openings_total,
            "countersunk_holes": holes.countersunk_holes,
            "counts": {
                name: len(features) for name, features in all_features.items()
            },
            "features": {
                name: [_feature_summary(feature) for feature in features]
                for name, features in all_features.items()
            },
        },
        "forming_features": [_model_dump(item) for item in result.forming_features],
        "bends": [_bend_summary(item) for item in result.bends.items],
        "flat_pattern": _model_dump(result.flat_pattern),
        "cutting": _model_dump(result.cutting),
        "warnings": list(result.warnings),
    }


def _strip_private_objects(case: dict[str, Any]) -> None:
    for key in [key for key in case if key.startswith("_")]:
        case.pop(key, None)


def run_case(name: str, path: Path, hooks: Hooks) -> dict[str, Any]:
    case: dict[str, Any] = {
        "case": name,
        "source_file": path.name,
        "path_in_container": str(path.relative_to(PROJECT_ROOT)),
        "file_size_bytes": path.stat().st_size,
        "v4_reference": V4_REFERENCE.get(name),
        "lance_candidate_traces": [],
        "through_bend_candidate_traces": [],
        "opening_classification": [],
    }
    hooks.current = case
    started = time.perf_counter()
    try:
        result = cad.analyze_step_file(
            file_bytes=path.read_bytes(),
            source_file=path.name,
            material="acciaio",
            density_g_cm3=7.85,
        )
        case["analysis_elapsed_sec"] = round(time.perf_counter() - started, 6)
        case["final"] = _final_summary(result)
        topology = case.get("topology") or {}
        coverage = case.get("bend_zone_coverage_gate") or {}
        independent_audit = (V4_REFERENCE.get(name) or {}).get(
            "independent_audit"
        )
        case["bend_inventory_distinction"] = {
            "production_graph_zone_count": topology.get("bend_zone_count", 0),
            "detected_bend_count": result.bends.count,
            "unmatched_production_zone_ids": coverage.get(
                "unmatched_zone_ids", []
            ),
            "independent_audit": independent_audit,
        }
        case["status"] = "completed"
    except Exception as exc:  # diagnostic batch must preserve the other cases
        case["analysis_elapsed_sec"] = round(time.perf_counter() - started, 6)
        case["status"] = "failed"
        case["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _strip_private_objects(case)
        hooks.current = None
    return case


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "phase4a_v52_diagnostic.json",
    )
    args = parser.parse_args()

    missing = [str(path) for path in CASES.values() if not path.is_file()]
    if missing:
        parser.error("Missing fixtures: " + "; ".join(missing))

    hooks = Hooks()
    hooks.install()
    try:
        cases = [run_case(name, path, hooks) for name, path in CASES.items()]
    finally:
        hooks.restore()

    report = {
        "report": "Phase 4A v5.2 FreeCAD regression diagnostics",
        "diagnostic_only": True,
        "production_code_modified_by_script": False,
        "source_hashes": {
            "app/cad_analyzer.py": _sha256(PROJECT_ROOT / "app/cad_analyzer.py"),
            "app/sheetmetal_unfolder.py": _sha256(
                PROJECT_ROOT / "app/sheetmetal_unfolder.py"
            ),
        },
        "case_count": len(cases),
        "completed": sum(case["status"] == "completed" for case in cases),
        "failed": sum(case["status"] == "failed" for case in cases),
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "case_count": report["case_count"],
        "completed": report["completed"],
        "failed": report["failed"],
    }, ensure_ascii=False, indent=2))
    return 0 if not report["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
