from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .cad_analyzer import VALID_STEP_SUFFIXES, analyze_step_file, get_freecad_status
from .schemas import CadAnalysisResponse, HealthResponse


app = FastAPI(
    title="REVERSEPARTS CAD AI",
    description="Backend for verifiable STEP/STP CAD analysis.",
    version="0.1.0",
)


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
