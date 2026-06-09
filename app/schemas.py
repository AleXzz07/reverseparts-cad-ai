from typing import Literal

from pydantic import BaseModel, Field


Confidence = Literal["low", "medium", "high"]


class Dimensions(BaseModel):
    x: float | None = None
    y: float | None = None
    z: float | None = None


class HoleFeature(BaseModel):
    num_sides: int | None = None
    max_dimension_mm: float | None = None
    bounding_box_mm: Dimensions | None = None
    diameter_mm: float | None = None
    radius_mm: float | None = None
    length_mm: float | None = None
    width_mm: float | None = None
    depth_mm: float | None = None
    center: list[float] | None = None
    axis: list[float] | None = None
    position_mm: Dimensions | None = None
    confidence: Confidence = "low"


class Holes(BaseModel):
    circular: list[HoleFeature] = Field(default_factory=list)
    elongated: list[HoleFeature] = Field(default_factory=list)
    polygonal: list[HoleFeature] = Field(default_factory=list)
    confidence: Confidence = "low"


class BendFeature(BaseModel):
    type: str = "simple flange"
    radius_mm: float | None = None
    length_mm: float | None = None
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
    complexity_score: Literal["unknown", "low", "medium", "high"] = "unknown"
    warnings: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    freecad_available: bool
    freecad_error: str | None = None
