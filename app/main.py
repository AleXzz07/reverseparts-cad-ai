from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .cad_analyzer import VALID_STEP_SUFFIXES, analyze_step_file, get_freecad_status
from .model_service import (
    deferred_viewer_model,
    generate_safe_viewer_model,
)
from .pdf_report import generate_quote_pdf
from .preview_service import generate_safe_step_preview, unavailable_preview
from .quote_engine import load_materials_config, load_pricing_config, quote_from_cad
from .schemas import (
    AnalyzeAndQuoteResponse,
    CadAnalysisResponse,
    HealthResponse,
    QuotePdfRequest,
    QuoteRequest,
    ViewerModelResponse,
)


app = FastAPI(
    title="REVERSEPARTS CAD AI",
    description="Backend for verifiable STEP/STP CAD analysis.",
    version="0.1.0",
)
FRONTEND_INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"
FRONTEND_VENDOR = FRONTEND_INDEX.parent / "vendor"
DEFAULT_CORS_ALLOW_ORIGINS = (
    "https://reverseparts-cad-ai.vercel.app",
)
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS",
        ",".join(DEFAULT_CORS_ALLOW_ORIGINS),
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=CORS_ALLOW_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount(
    "/vendor",
    StaticFiles(directory=FRONTEND_VENDOR),
    name="frontend-vendor",
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


def _json_overrides_or_400(value: str | None, label: str) -> dict[str, float] | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{label} must be a JSON object.")
    return payload


@app.get("/", include_in_schema=False)
def frontend() -> HTMLResponse:
    return HTMLResponse(FRONTEND_INDEX.read_text(encoding="utf-8"))


@app.get("/app-config.js", include_in_schema=False)
def app_config() -> Response:
    api_base_url = os.getenv("API_BASE_URL", "").rstrip("/")
    script = (
        "window.REVERSEPARTS_API_BASE_URL = "
        f"{json.dumps(api_base_url)};\n"
    )
    return Response(
        content=script,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/config/defaults")
def config_defaults() -> dict[str, Any]:
    pricing = load_pricing_config()
    return {
        "pricing": {
            field: getattr(pricing, field)
            for field in pricing.__dataclass_fields__
        },
        "materials": load_materials_config(),
    }


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
            pricing_overrides=request.pricing_overrides,
            material_overrides=request.material_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/quote-pdf")
def quote_pdf(request: QuotePdfRequest) -> Response:
    preview_payload = _model_to_dict(request.preview) if request.preview else None
    viewer_model_payload = (
        _model_to_dict(request.viewer_model)
        if request.viewer_model
        else None
    )
    pdf_bytes = generate_quote_pdf(
        request.analysis,
        request.quote,
        preview=preview_payload,
        viewer_model=viewer_model_payload,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="reverseparts_preventivo.pdf"'},
    )


@app.post("/viewer-model", response_model=ViewerModelResponse)
async def viewer_model(
    file: UploadFile = File(...),
    complexity_score: str = Form(default="unknown"),
) -> ViewerModelResponse:
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="CAD file is required.")
    if not any(filename.lower().endswith(suffix) for suffix in VALID_STEP_SUFFIXES):
        raise HTTPException(
            status_code=400,
            detail="Only .stp and .step files are accepted.",
        )
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded CAD file is empty.")

    step_path: str | None = None
    try:
        suffix = Path(filename).suffix.lower() or ".step"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as step_file:
            step_file.write(file_bytes)
            step_path = step_file.name
        return ViewerModelResponse(
            **generate_safe_viewer_model(
                step_path,
                complexity_score=complexity_score,
            )
        )
    except Exception as exc:
        return ViewerModelResponse(
            available=False,
            warnings=[f"3D model export skipped or failed: {exc}"],
        )
    finally:
        if step_path:
            Path(step_path).unlink(missing_ok=True)


@app.post("/analyze-and-quote", response_model=AnalyzeAndQuoteResponse)
async def analyze_and_quote(
    file: UploadFile = File(...),
    material: str = Form(...),
    quantity: int = Form(...),
    declared_thickness_mm: float | None = Form(default=None),
    pricing_overrides: str | None = Form(default=None),
    material_overrides: str | None = Form(default=None),
) -> AnalyzeAndQuoteResponse:
    quantity = _validate_quantity(quantity)
    material_config = _material_config_or_400(material)
    parsed_pricing_overrides = _json_overrides_or_400(
        pricing_overrides,
        "pricing_overrides",
    )
    parsed_material_overrides = _json_overrides_or_400(
        material_overrides,
        "material_overrides",
    )
    density = (
        parsed_material_overrides.get("density_g_cm3")
        if parsed_material_overrides
        and "density_g_cm3" in parsed_material_overrides
        else material_config["density_g_cm3"]
    )
    analysis = await _analyze_uploaded_cad(
        file=file,
        material=material,
        density_g_cm3=density,
        declared_thickness_mm=declared_thickness_mm,
        quantity=quantity,
    )
    analysis_payload = _model_to_dict(analysis)
    try:
        quote_payload = quote_from_cad(
            analysis_payload,
            quantity=quantity,
            material=material,
            pricing_overrides=parsed_pricing_overrides,
            material_overrides=parsed_material_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    preview_payload: dict[str, Any] = unavailable_preview(
        "preview was not attempted."
    )
    viewer_model_payload = deferred_viewer_model(
        analysis_payload.get("complexity_score", "unknown")
    )
    step_path: str | None = None
    try:
        await file.seek(0)
        file_bytes = await file.read()
        suffix = Path(file.filename or "part.step").suffix.lower() or ".step"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as step_file:
            step_file.write(file_bytes)
            step_path = step_file.name
        try:
            preview_payload = generate_safe_step_preview(
                step_path,
                complexity_score=analysis_payload.get(
                    "complexity_score",
                    "unknown",
                ),
            )
        except Exception as exc:
            preview_payload = unavailable_preview(str(exc))
    except Exception as exc:
        preview_payload = unavailable_preview(str(exc))
    finally:
        if step_path:
            Path(step_path).unlink(missing_ok=True)
    return AnalyzeAndQuoteResponse(
        analysis=analysis_payload,
        quote=quote_payload,
        preview=preview_payload,
        viewer_model=viewer_model_payload,
    )
