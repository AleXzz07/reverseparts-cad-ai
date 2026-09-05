from typing import Literal
from typing import Any

from pydantic import BaseModel, Field


Confidence = Literal["low", "medium", "high"]


class Dimensions(BaseModel):
    x: float | None = None
    y: float | None = None
    z: float | None = None


class HoleFeature(BaseModel):
    type: str | None = None
    reason: str | None = None
    num_sides: int | None = None
    max_dimension_mm: float | None = None
    bounding_box_mm: Dimensions | None = None
    perimeter_mm: float | None = None
    diameter_mm: float | None = None
    radius_mm: float | None = None
    length_mm: float | None = None
    width_mm: float | None = None
    depth_mm: float | None = None
    center: list[float] | None = None
    axis: list[float] | None = None
    position_mm: Dimensions | None = None
    edge_distance_mm: float | None = None
    confidence: Confidence = "low"


class Holes(BaseModel):
    circular: list[HoleFeature] = Field(default_factory=list)
    elongated: list[HoleFeature] = Field(default_factory=list)
    polygonal: list[HoleFeature] = Field(default_factory=list)
    formed: list[HoleFeature] = Field(default_factory=list)
    unknown: list[HoleFeature] = Field(default_factory=list)
    circular_holes: int = 0
    elongated_holes: int = 0
    polygonal_holes: int = 0
    formed_holes: int = 0
    unknown_holes: int = 0
    total_holes: int = 0
    min_circular_diameter_mm: float | None = None
    max_circular_diameter_mm: float | None = None
    confidence: Confidence = "low"


class BendFeature(BaseModel):
    type: str = "simple flange"
    radius_mm: float | None = None
    length_mm: float | None = None
    angle_deg: float | None = None
    axis: list[float] | None = None
    center: list[float] | None = None
    confidence: Confidence = "low"


class Bends(BaseModel):
    count: int | None = None
    confidence: Confidence = "low"
    items: list[BendFeature] = Field(default_factory=list)


class Cutting(BaseModel):
    outer_cut_length_mm: float | None = None
    inner_cut_length_mm: float | None = None
    total_cut_length_mm: float | None = None
    confidence: Confidence = "low"
    warnings: list[str] = Field(default_factory=list)


class GeometryStatistics(BaseModel):
    bounding_box_center_mm: Dimensions | None = None
    center_of_mass_mm: Dimensions | None = None
    solid_count: int = 0
    shell_count: int = 0
    face_count: int = 0
    edge_count: int = 0
    vertex_count: int = 0


class Manufacturability(BaseModel):
    min_hole_to_edge_mm: float | None = None
    hole_to_edge_confidence: Confidence = "low"
    measured_holes: int = 0
    warnings: list[str] = Field(default_factory=list)


class CadAnalysisResponse(BaseModel):
    part_name: str = ""
    source_file: str = ""
    raw_bounding_box_mm: Dimensions = Field(default_factory=Dimensions)
    effective_dimensions_mm: Dimensions = Field(default_factory=Dimensions)
    volume_cm3: float | None = None
    surface_area_cm2: float | None = None
    estimated_weight_kg: float | None = None
    declared_material: str | None = None
    density_g_cm3: float | None = None
    declared_thickness_mm: float | None = None
    detected_thickness_mm: float | None = None
    thickness_confidence: Confidence = "low"
    holes: Holes = Field(default_factory=Holes)
    bends: Bends = Field(default_factory=Bends)
    cutting: Cutting = Field(default_factory=Cutting)
    geometry: GeometryStatistics = Field(default_factory=GeometryStatistics)
    manufacturability: Manufacturability = Field(default_factory=Manufacturability)
    complexity_score: Literal["unknown", "low", "medium", "high"] = "unknown"
    warnings: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    freecad_available: bool
    freecad_error: str | None = None


class QuoteRequest(BaseModel):
    analysis: dict[str, Any]
    quantity: int = Field(gt=0)
    material: str
    pricing_overrides: dict[str, float] | None = None
    material_overrides: dict[str, float] | None = None


class PreviewView(BaseModel):
    name: str | None = None
    key: str | None = None
    label: str | None = None
    image_png_base64: str | None = None
    image_url: str | None = None


class PreviewResponse(BaseModel):
    image_png_base64: str | None = None
    available: bool = False
    mode: Literal[
        "not_generated",
        "full",
        "light",
        "ultra_light",
        "partial",
        "failed",
    ] = "failed"
    partial: bool = False
    views: list[PreviewView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ViewerModelResponse(BaseModel):
    available: bool = False
    model_base64: str | None = None
    format: Literal["glb"] | None = None
    model_url: str | None = None
    warnings: list[str] = Field(default_factory=list)


class AnalyzeAndQuoteResponse(BaseModel):
    analysis: dict[str, Any]
    quote: dict[str, Any]
    preview: PreviewResponse = Field(default_factory=PreviewResponse)
    viewer_model: ViewerModelResponse = Field(default_factory=ViewerModelResponse)


class GeneratePreviewResponse(BaseModel):
    preview: PreviewResponse = Field(default_factory=PreviewResponse)


class QuotePdfRequest(BaseModel):
    analysis: dict[str, Any]
    quote: dict[str, Any]
    preview: PreviewResponse | None = None
    viewer_model: ViewerModelResponse | None = None
