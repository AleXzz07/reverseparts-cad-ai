from typing import Literal
from typing import Any

from pydantic import BaseModel, Field


Confidence = Literal["low", "medium", "high"]


class Dimensions(BaseModel):
    x: float | None = None
    y: float | None = None
    z: float | None = None


class HoleFeature(BaseModel):
    feature_id: str | None = None
    component_id: str | None = None
    type: str | None = None
    reason: str | None = None
    num_sides: int | None = None
    max_dimension_mm: float | None = None
    bounding_box_mm: Dimensions | None = None
    perimeter_mm: float | None = None
    circumference_mm: float | None = None
    area_mm2: float | None = None
    diameter_mm: float | None = None
    through_diameter_mm: float | None = None
    countersink_major_diameter_mm: float | None = None
    countersink_depth_mm: float | None = None
    radius_mm: float | None = None
    length_mm: float | None = None
    overall_length_mm: float | None = None
    straight_length_mm: float | None = None
    end_radius_mm: float | None = None
    corner_radius_mm: float | None = None
    width_mm: float | None = None
    depth_mm: float | None = None
    center: list[float] | None = None
    axis: list[float] | None = None
    orientation_axis: list[float] | None = None
    position_mm: Dimensions | None = None
    edge_distance_mm: float | None = None
    nearest_hole_distance_mm: float | None = None
    flat_center_mm: Dimensions | None = None
    flat_bend_distance_mm: float | None = None
    flat_contour_propagated: bool = False
    confidence: Confidence = "low"


class Holes(BaseModel):
    circular: list[HoleFeature] = Field(default_factory=list)
    elongated: list[HoleFeature] = Field(default_factory=list)
    rounded_rectangular: list[HoleFeature] = Field(default_factory=list)
    polygonal: list[HoleFeature] = Field(default_factory=list)
    formed: list[HoleFeature] = Field(default_factory=list)
    unknown: list[HoleFeature] = Field(default_factory=list)
    circular_holes: int = 0
    countersunk_holes: int = 0
    elongated_holes: int = 0
    rounded_rectangular_holes: int = 0
    polygonal_holes: int = 0
    formed_holes: int = 0
    unknown_holes: int = 0
    total_holes: int = 0
    physical_openings_total: int = 0
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
    source: Literal["validated_flat_pattern", "unavailable"] = "unavailable"
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
    min_hole_to_hole_mm: float | None = None
    hole_to_hole_confidence: Confidence = "low"
    measured_hole_pairs: int = 0
    min_hole_to_bend_mm: float | None = None
    hole_to_bend_confidence: Confidence = "low"
    measured_hole_to_bend: int = 0
    warnings: list[str] = Field(default_factory=list)


class CoordinateReference(BaseModel):
    origin: str = "Original STEP file origin"
    axes: str = "Original STEP X/Y/Z axes; no automatic reorientation"
    units: Literal["mm"] = "mm"


class FlatBendLine(BaseModel):
    id: str
    start_mm: Dimensions
    end_mm: Dimensions
    radius_mm: float
    angle_deg: float
    allowance_mm: float
    length_mm: float


class FlatPatternValidation(BaseModel):
    graph_connected: bool = False
    topology_continuous: bool = False
    self_intersections: int = 0
    overlap_area_mm2: float | None = None
    area_coherence_error_pct: float | None = None
    perimeter_coherence_error_pct: float | None = None
    all_openings_propagated: bool = False
    passed: bool = False


class FlatPattern(BaseModel):
    status: Literal["unavailable", "partial", "validated_estimate", "exact"] = "unavailable"
    available: bool = False
    usable_for_costing: bool = False
    method: str | None = None
    is_estimate: bool = True
    root_panel_id: str | None = None
    thickness_mm: float | None = None
    k_factor: float | None = None
    k_factor_standard: Literal["ANSI"] = "ANSI"
    panel_count: int = 0
    bend_zone_count: int = 0
    net_developed_area_mm2: float | None = None
    opening_area_mm2: float | None = None
    gross_blank_area_mm2: float | None = None
    blank_dimensions_mm: Dimensions | None = None
    outer_perimeter_mm: float | None = None
    inner_perimeter_mm: float | None = None
    total_cut_length_mm: float | None = None
    total_bend_length_mm: float | None = None
    total_bend_allowance_mm: float | None = None
    blank_weight_kg: float | None = None
    diagnostic_volume_area_mm2: float | None = None
    diagnostic_volume_area_error_pct: float | None = None
    propagated_opening_count: int = 0
    bend_lines: list[FlatBendLine] = Field(default_factory=list)
    validation: FlatPatternValidation = Field(default_factory=FlatPatternValidation)
    confidence: Confidence = "low"
    warnings: list[str] = Field(default_factory=list)


class PartClassification(BaseModel):
    category: Literal[
        "sheet_metal",
        "non_sheet_metal",
        "multi_solid",
        "unknown",
    ] = "unknown"
    confidence: Confidence = "low"
    reason: str = "Geometric classification is not available."


class AssemblyComponent(BaseModel):
    id: str
    name: str | None = None
    bounding_box_mm: Dimensions = Field(default_factory=Dimensions)
    volume_cm3: float | None = None
    surface_area_cm2: float | None = None
    classification: PartClassification = Field(default_factory=PartClassification)
    holes: Holes = Field(default_factory=Holes)


class AssemblyPassage(BaseModel):
    id: str
    geometry: Literal["circular", "unknown"] = "unknown"
    component_ids: list[str] = Field(default_factory=list)
    feature_ids: list[str] = Field(default_factory=list)
    diameter_mm: float | None = None
    center: list[float] | None = None
    axis: list[float] | None = None
    confidence: Confidence = "low"
    reason: str = "Assembly passage could not be verified."


class WeldEvidence(BaseModel):
    id: str
    state: Literal["weld_detected", "weld_candidate", "manual_weld"]
    review_status: Literal["pending", "confirmed", "rejected"] = "pending"
    component_ids: list[str] = Field(default_factory=list)
    geometry: Literal["circular", "linear", "closed_contour", "unknown"] = "unknown"
    nominal_length_mm: float | None = None
    reference_diameter_mm: float | None = None
    center: list[float] | None = None
    axis: list[float] | None = None
    contact_evidence: str | None = None
    confidence: Confidence = "low"
    reason: str = "Weld evidence is not available."


class AssemblyAnalysis(BaseModel):
    component_count: int = 0
    components: list[AssemblyComponent] = Field(default_factory=list)
    component_opening_features_total: int = 0
    physical_passages_total: int = 0
    physical_passages: list[AssemblyPassage] = Field(default_factory=list)
    weld_candidates: list[WeldEvidence] = Field(default_factory=list)
    confidence: Confidence = "low"
    warnings: list[str] = Field(default_factory=list)


class WeldConfiguration(BaseModel):
    weld_id: str
    source_state: Literal["weld_detected", "weld_candidate", "manual_weld"]
    review_status: Literal["confirmed", "rejected"] = "confirmed"
    process: Literal["TIG", "MIG", "MAG"] | None = None
    continuity: Literal["continuous", "intermittent"] | None = None
    joint_type: str | None = None
    side: Literal["one", "both"] | None = None
    weld_length_mm: float | None = Field(default=None, gt=0)
    segment_length_mm: float | None = Field(default=None, gt=0)
    pitch_mm: float | None = Field(default=None, gt=0)
    gap_mm: float | None = Field(default=None, ge=0)
    segment_count: int | None = Field(default=None, gt=0)
    size_basis: Literal["a", "z"] | None = None
    size_mm: float | None = Field(default=None, gt=0)
    passes: int | None = Field(default=None, gt=0)
    speed_mm_min: float | None = Field(default=None, gt=0)
    time_sec_per_mm: float | None = Field(default=None, gt=0)
    setup_time_min: float = Field(default=0.0, ge=0)
    setup_scope: Literal["per_lot", "per_process", "per_weld"] = "per_process"
    preparation_time_min_per_piece: float = Field(default=0.0, ge=0)
    finishing_grinding: bool = False
    finishing_time_min_per_piece: float = Field(default=0.0, ge=0)
    hourly_rate_eur: float | None = Field(default=None, ge=0)


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
    coordinate_reference: CoordinateReference = Field(default_factory=CoordinateReference)
    part_classification: PartClassification = Field(default_factory=PartClassification)
    assembly: AssemblyAnalysis = Field(default_factory=AssemblyAnalysis)
    flat_pattern: FlatPattern = Field(default_factory=FlatPattern)
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
    welds: list[WeldConfiguration] | None = None


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
