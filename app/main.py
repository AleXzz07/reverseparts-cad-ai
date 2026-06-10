from __future__ import annotations

from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .cad_analyzer import VALID_STEP_SUFFIXES, analyze_step_file, get_freecad_status
from .quote_engine import load_materials_config, quote_from_cad
from .schemas import AnalyzeAndQuoteResponse, CadAnalysisResponse, HealthResponse, QuoteRequest


app = FastAPI(
    title="REVERSEPARTS CAD AI",
    description="Backend for verifiable STEP/STP CAD analysis.",
    version="0.1.0",
)


def _model_to_dict(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def _validate_quantity(quantity: int) -> int:
    quantity = int(quantity)
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than 0.")
    return quantity


def _material_config_or_400(material: str) -> dict[str, Any]:
    material_key = material.lower()
    materials = load_materials_config()
    material_config = materials.get(material_key)
    if material_config is None:
        available = ", ".join(sorted(materials))
        raise HTTPException(
            status_code=400,
            detail=f"Unknown material '{material}'. Available materials: {available}.",
        )
    return material_config


async def _analyze_uploaded_cad(
    *,
    file: UploadFile,
    material: str | None,
    density_g_cm3: float | None,
    declared_thickness_mm: float | None,
    quantity: int,
) -> CadAnalysisResponse:
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="CAD file is required.")

    if not any(filename.lower().endswith(suffix) for suffix in VALID_STEP_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only .stp and .step files are accepted.")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded CAD file is empty.")

    freecad_status = get_freecad_status()
    if not freecad_status.available:
        raise HTTPException(
            status_code=503,
            detail={
                "message": "FreeCAD is not available, CAD analysis cannot run.",
                "freecad_error": freecad_status.error,
            },
        )

    result = analyze_step_file(
        file_bytes=file_bytes,
        source_file=filename,
        material=material,
        density_g_cm3=density_g_cm3,
        declared_thickness_mm=declared_thickness_mm,
        quantity=quantity,
    )
    if result.raw_bounding_box_mm.x is None and result.warnings:
        raise HTTPException(status_code=422, detail=result.warnings)
    return result


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    status = get_freecad_status()
    return HealthResponse(
        freecad_available=status.available,
        freecad_error=status.error,
    )


@app.post("/analyze-cad", response_model=CadAnalysisResponse)
async def analyze_cad(
    file: UploadFile = File(...),
    material: str | None = Form(default=None),
    density_g_cm3: float | None = Form(default=None),
    declared_thickness_mm: float | None = Form(default=None),
    quantity: int = Form(default=1),
) -> CadAnalysisResponse:
    return await _analyze_uploaded_cad(
        file=file,
        material=material,
        density_g_cm3=density_g_cm3,
        declared_thickness_mm=declared_thickness_mm,
        quantity=quantity,
    )


@app.post("/quote")
def quote(request: QuoteRequest) -> dict[str, Any]:
    quantity = _validate_quantity(request.quantity)
    _material_config_or_400(request.material)
    try:
        return quote_from_cad(
            request.analysis,
            quantity=quantity,
            material=request.material,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/analyze-and-quote", response_model=AnalyzeAndQuoteResponse)
async def analyze_and_quote(
    file: UploadFile = File(...),
    material: str = Form(...),
    quantity: int = Form(...),
    declared_thickness_mm: float | None = Form(default=None),
) -> AnalyzeAndQuoteResponse:
    quantity = _validate_quantity(quantity)
    material_config = _material_config_or_400(material)
    analysis = await _analyze_uploaded_cad(
        file=file,
        material=material,
        density_g_cm3=material_config["density_g_cm3"],
        declared_thickness_mm=declared_thickness_mm,
        quantity=quantity,
    )
    analysis_payload = _model_to_dict(analysis)
    quote_payload = quote_from_cad(
        analysis_payload,
        quantity=quantity,
        material=material,
    )
    return AnalyzeAndQuoteResponse(
        analysis=analysis_payload,
        quote=quote_payload,
    )
