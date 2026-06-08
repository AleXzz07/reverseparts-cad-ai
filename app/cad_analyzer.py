from __future__ import annotations

import importlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .schemas import CadAnalysisResponse, Dimensions


VALID_STEP_SUFFIXES = {".stp", ".step"}
FREECAD_PATH_CANDIDATES = (
    "/usr/lib/freecad-python3/lib",
    "/usr/lib/freecad/lib",
    "/usr/lib/freecad-python3",
)


@dataclass(frozen=True)
class FreeCadStatus:
    available: bool
    error: str | None = None


def _configure_freecad_path() -> None:
    import sys

    for candidate in FREECAD_PATH_CANDIDATES:
        if os.path.isdir(candidate) and candidate not in sys.path:
            sys.path.append(candidate)


def get_freecad_status() -> FreeCadStatus:
    try:
        _configure_freecad_path()
        importlib.import_module("FreeCAD")
        importlib.import_module("Part")
    except Exception as exc:  # pragma: no cover - depends on host FreeCAD install
        return FreeCadStatus(available=False, error=str(exc))
    return FreeCadStatus(available=True)


def _round_or_none(value: float | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _base_response(
    *,
    source_file: str,
    material: str | None,
    density_g_cm3: float | None,
    declared_thickness_mm: float | None,
) -> CadAnalysisResponse:
    return CadAnalysisResponse(
        part_name=Path(source_file).stem,
        source_file=source_file,
        declared_material=material,
        density_g_cm3=density_g_cm3,
        declared_thickness_mm=declared_thickness_mm,
    )


def analyze_step_file(
    *,
    file_bytes: bytes,
    source_file: str,
    material: str | None = None,
    density_g_cm3: float | None = None,
    declared_thickness_mm: float | None = None,
    quantity: int = 1,
) -> CadAnalysisResponse:
    response = _base_response(
        source_file=source_file,
        material=material,
        density_g_cm3=density_g_cm3,
        declared_thickness_mm=declared_thickness_mm,
    )

    suffix = Path(source_file).suffix.lower()
    if suffix not in VALID_STEP_SUFFIXES:
        response.warnings.append("Unsupported CAD extension. Only .stp and .step files are accepted.")
        return response

    if not file_bytes:
        response.warnings.append("Uploaded CAD file is empty.")
        return response

    status = get_freecad_status()
    if not status.available:
        response.warnings.append(f"FreeCAD is not available: {status.error}")
        return response

    _configure_freecad_path()
    Part = importlib.import_module("Part")

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            temp_file.write(file_bytes)
            temp_path = temp_file.name

        shape = Part.Shape()
        shape.read(temp_path)

        bbox = shape.BoundBox
        response.raw_bounding_box_mm = Dimensions(
            x=_round_or_none(bbox.XLength),
            y=_round_or_none(bbox.YLength),
            z=_round_or_none(bbox.ZLength),
        )
        response.effective_dimensions_mm = response.raw_bounding_box_mm
        response.volume_cm3 = _round_or_none(shape.Volume / 1000.0)
        response.surface_area_cm2 = _round_or_none(shape.Area / 100.0)

        if response.volume_cm3 is not None and density_g_cm3 is not None:
            response.estimated_weight_kg = _round_or_none(
                response.volume_cm3 * density_g_cm3 * max(quantity, 1) / 1000.0
            )

        response.warnings.extend(
            [
                "Hole detection is not reported because this implementation has no high-confidence feature classifier yet.",
                "Bend detection is not reported because this implementation has no high-confidence sheet-metal classifier yet.",
                "Detected thickness is not reported because wall-thickness inference is not yet reliable.",
            ]
        )
    except Exception as exc:
        response.warnings.append(f"FreeCAD failed to parse the STEP file: {exc}")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    return response
