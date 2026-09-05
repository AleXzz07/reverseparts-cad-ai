from __future__ import annotations

import base64
import binascii
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from PIL import Image as PillowImage


UNKNOWN_HOLE_WARNING = (
    "Some openings were detected but their shape could not be classified "
    "with confidence."
)


def _value(value: Any, unit: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return f"{text} {unit}".strip()


def _dimensions(analysis: dict[str, Any]) -> str:
    dimensions = analysis.get("effective_dimensions_mm") or analysis.get("raw_bounding_box_mm") or {}
    values = [dimensions.get(axis) for axis in ("x", "y", "z")]
    if any(value is None for value in values):
        return "-"
    return " x ".join(_value(value) for value in values)


def _vector(values: Any, unit: str = "") -> str:
    if not isinstance(values, (list, tuple)) or len(values) < 3:
        return "-"
    text = " / ".join(_value(value) for value in values[:3])
    return f"{text} {unit}".strip()


def _xyz_dimensions(values: Any) -> str:
    if not isinstance(values, dict):
        return "-"
    dimensions = [values.get(axis) for axis in ("x", "y", "z")]
    if any(value is None for value in dimensions):
        return "-"
    return f"{_value(dimensions[0])} x {_value(dimensions[1])} x {_value(dimensions[2])} mm"


def _part_rows(
    analysis: dict[str, Any],
    quote: dict[str, Any],
) -> list[tuple[str, Any]]:
    material = quote.get("material", {})
    cutting = analysis.get("cutting", {})
    bends = analysis.get("bends", {})
    holes = analysis.get("holes", {})
    features = quote.get("features_summary", {})

    def hole_count(summary_key: str, group_key: str) -> int:
        value = features.get(summary_key)
        if value is None:
            value = holes.get(summary_key)
        if value is None:
            value = len(holes.get(group_key, []) or [])
        return int(value)

    circular_holes = hole_count("circular_holes", "circular")
    elongated_holes = hole_count("elongated_holes", "elongated")
    polygonal_holes = hole_count("polygonal_holes", "polygonal")
    formed_holes = hole_count("formed_holes", "formed")
    unknown_holes = hole_count("unknown_holes", "unknown")
    total_holes = features.get("total_holes")
    if total_holes is None:
        total_holes = (
            circular_holes
            + elongated_holes
            + polygonal_holes
            + formed_holes
            + unknown_holes
        )

    return [
        ("Nome pezzo", quote.get("part_name") or analysis.get("part_name")),
        ("Materiale", material.get("name")),
        ("Quantità", quote.get("quantity")),
        ("Dimensioni mm", _dimensions(analysis)),
        (
            "Spessore",
            material.get("thickness_mm")
            or analysis.get("detected_thickness_mm"),
        ),
        (
            "Peso unitario stimato",
            _value(material.get("estimated_weight_kg"), "kg"),
        ),
        (
            "Lunghezza taglio totale",
            _value(cutting.get("total_cut_length_mm"), "mm"),
        ),
        ("Fori circolari", circular_holes),
        ("Asole", elongated_holes),
        ("Fori poligonali", polygonal_holes),
        ("Fori sagomati/imbutiti", formed_holes),
        ("Fori non riconosciuti", unknown_holes),
        ("Fori totali", total_holes),
        (
            "Numero pieghe",
            bends.get("count")
            if bends.get("count") is not None
            else features.get("bends"),
        ),
    ]


def _verification_rows(
    analysis: dict[str, Any],
    quote: dict[str, Any],
) -> list[tuple[str, Any]]:
    holes = analysis.get("holes", {})
    features = quote.get("features_summary", {})
    unknown_holes = features.get("unknown_holes")
    if unknown_holes is None:
        unknown_holes = holes.get("unknown_holes")
    if unknown_holes is None:
        unknown_holes = len(holes.get("unknown", []) or [])
    rows: list[tuple[str, Any]] = []
    if int(unknown_holes) > 0:
        rows.append(("Aperture non riconosciute", UNKNOWN_HOLE_WARNING))
    rows.extend(
        (f"Producibilita {index}", warning)
        for index, warning in enumerate(
            analysis.get("manufacturability", {}).get("warnings", []) or [],
            start=1,
        )
    )
    return rows


def _section(title: str, rows: list[tuple[str, Any]]) -> list[Any]:
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "CompactTableBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=10,
    )
    heading_style = ParagraphStyle(
        "CompactHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        spaceBefore=0,
        spaceAfter=4,
    )
    table_data = [[Paragraph("<b>Voce</b>", body_style), Paragraph("<b>Valore</b>", body_style)]]
    table_data.extend(
        [
            [
                Paragraph(escape(str(label)), body_style),
                Paragraph(escape(_value(value)), body_style),
            ]
            for label, value in rows
        ]
    )
    table = Table(table_data, colWidths=[70 * mm, 100 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAEFF5")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C2CC")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("LEADING", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return [
        KeepTogether(
            [Paragraph(title, heading_style), table, Spacer(1, 4 * mm)]
        )
    ]


def _detail_table(title: str, headers: list[str], rows: list[list[Any]]) -> list[Any]:
    if not rows:
        return []
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        f"{title}Body",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7,
        leading=8,
    )
    heading_style = ParagraphStyle(
        f"{title}Heading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        spaceBefore=0,
        spaceAfter=4,
    )
    table_data = [
        [Paragraph(f"<b>{escape(header)}</b>", body_style) for header in headers]
    ]
    table_data.extend(
        [Paragraph(escape(_value(value)), body_style) for value in row]
        for row in rows
    )
    available_width = 170 * mm
    table = Table(
        table_data,
        colWidths=[available_width / len(headers)] * len(headers),
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAEFF5")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C2CC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ]
        )
    )
    return [Paragraph(title, heading_style), table, Spacer(1, 4 * mm)]


def _hole_detail_rows(analysis: dict[str, Any]) -> list[list[Any]]:
    holes = analysis.get("holes", {})
    groups = (
        ("Circolare", holes.get("circular", [])),
        ("Asola", holes.get("elongated", [])),
        ("Poligonale", holes.get("polygonal", [])),
        ("Sagomato", holes.get("formed", [])),
        ("Non riconosciuto", holes.get("unknown", [])),
    )
    rows: list[list[Any]] = []
    for label, features in groups:
        for index, feature in enumerate(features or [], start=1):
            if feature.get("diameter_mm") is not None:
                measure = f"Diam. {_value(feature['diameter_mm'], 'mm')}"
            elif feature.get("width_mm") is not None:
                measure = (
                    f"L {_value(feature.get('overall_length_mm'), 'mm')} / "
                    f"W {_value(feature.get('width_mm'), 'mm')}"
                )
            elif feature.get("bounding_box_mm"):
                measure = _xyz_dimensions(feature["bounding_box_mm"])
            else:
                measure = _value(
                    feature.get("max_dimension_mm") or feature.get("perimeter_mm"),
                    "mm",
                )
            rows.append(
                [
                    f"{label} {index}",
                    measure,
                    _value(
                        feature.get("circumference_mm") or feature.get("perimeter_mm"),
                        "mm",
                    ),
                    _value(feature.get("area_mm2"), "mm2"),
                    _vector(feature.get("center"), "mm"),
                    _vector(feature.get("axis")),
                    _value(feature.get("edge_distance_mm"), "mm"),
                    _value(feature.get("nearest_hole_distance_mm"), "mm"),
                    feature.get("confidence", "low"),
                ]
            )
    return rows


def _hole_group_rows(analysis: dict[str, Any]) -> list[list[Any]]:
    detail_rows = _hole_detail_rows(analysis)
    groups: dict[tuple[str, str], int] = {}
    for row in detail_rows:
        type_name = str(row[0]).rsplit(" ", 1)[0]
        key = (type_name, str(row[1]))
        groups[key] = groups.get(key, 0) + 1
    return [[count, type_name, measure] for (type_name, measure), count in groups.items()]


def _bend_detail_rows(analysis: dict[str, Any]) -> list[list[Any]]:
    return [
        [
            index,
            _value(bend.get("radius_mm"), "mm"),
            _value(bend.get("length_mm"), "mm"),
            _value(bend.get("angle_deg"), "deg"),
            _vector(bend.get("axis")),
            bend.get("confidence", "low"),
        ]
        for index, bend in enumerate(
            analysis.get("bends", {}).get("items", []) or [],
            start=1,
        )
    ]


PREVIEW_LABELS = {
    "isometric": "Isometrica",
    "front": "Frontale",
    "right": "Destra",
    "top": "Alto",
    "rear": "Posteriore",
    "left": "Sinistra",
}


def _preview_image(
    encoded: str,
    *,
    max_width: float,
    max_height: float,
) -> Image | None:
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
        if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid PNG signature")
        source = BytesIO(image_bytes)
        normalized = BytesIO()
        with PillowImage.open(source) as pillow_image:
            pillow_image.load()
            pillow_image.convert("RGB").save(normalized, format="PNG")
        normalized.seek(0)
        image = Image(normalized)
        scale = min(
            max_width / image.imageWidth,
            max_height / image.imageHeight,
            1.0,
        )
        image.drawWidth = image.imageWidth * scale
        image.drawHeight = image.imageHeight * scale
        image.hAlign = "CENTER"
        return image
    except (binascii.Error, OSError, TypeError, ValueError):
        return None


def _preview_section(preview: dict[str, Any] | None) -> list[Any]:
    styles = getSampleStyleSheet()
    heading_style = ParagraphStyle(
        "PreviewHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        spaceBefore=0,
        spaceAfter=4,
    )
    fallback_style = ParagraphStyle(
        "PreviewFallback",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=8.5,
        leading=10,
        textColor=colors.HexColor("#63707C"),
    )
    elements: list[Any] = [Paragraph("Anteprima pezzo", heading_style)]
    preview_mode = (preview or {}).get("mode")
    if preview_mode in {"light", "ultra_light"}:
        elements.append(
            Paragraph(
                "Anteprima semplificata per pezzo complesso",
                fallback_style,
            )
        )
    label_style = ParagraphStyle(
        "PreviewLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=9,
        alignment=1,
        spaceAfter=2,
    )
    view_payloads = [
        view
        for view in (preview or {}).get("views", [])
        if view.get("image_png_base64")
    ]
    if not view_payloads and (preview or {}).get("image_png_base64"):
        view_payloads = [
            {
                "name": "isometric",
                "image_png_base64": preview["image_png_base64"],
            }
        ]
    if not view_payloads:
        message = (
            "Anteprima pezzo non generata"
            if (preview or {}).get("mode") == "not_generated"
            else "Anteprima pezzo non disponibile"
        )
        return [
            *elements,
            Paragraph(message, fallback_style),
            Spacer(1, 4 * mm),
        ]

    rendered_views = []
    for view in view_payloads[:4]:
        image = _preview_image(
            view["image_png_base64"],
            max_width=78 * mm,
            max_height=48 * mm,
        )
        if image is not None:
            view_name = view.get("name") or view.get("key", "")
            rendered_views.append(
                (
                    view_name,
                    view.get("label")
                    or PREVIEW_LABELS.get(
                        view_name,
                        view_name.title(),
                    ),
                    image,
                    view["image_png_base64"],
                )
            )

    if not rendered_views:
        message = (
            "Anteprima pezzo non generata"
            if (preview or {}).get("mode") == "not_generated"
            else "Anteprima pezzo non disponibile"
        )
        return [
            *elements,
            Paragraph(message, fallback_style),
            Spacer(1, 4 * mm),
        ]
    if len(rendered_views) == 1:
        _, label, _, image_png_base64 = rendered_views[0]
        larger_image = _preview_image(
            image_png_base64,
            max_width=120 * mm,
            max_height=72 * mm,
        )
        if larger_image is not None:
            return [
                *elements,
                Paragraph(label, label_style),
                larger_image,
                Spacer(1, 4 * mm),
            ]

    primary_view = next(
        (
            rendered_view
            for rendered_view in rendered_views
            if rendered_view[0] == "isometric"
        ),
        rendered_views[0],
    )
    primary_name, primary_label, _, primary_png = primary_view
    primary_image = _preview_image(
        primary_png,
        max_width=120 * mm,
        max_height=62 * mm,
    )
    primary_elements = []
    if primary_image is not None:
        primary_elements = [
            Paragraph(primary_label, label_style),
            primary_image,
            Spacer(1, 3 * mm),
        ]
    secondary_views = [
        rendered_view
        for rendered_view in rendered_views
        if rendered_view[0] != primary_name
    ]
    if not secondary_views:
        return [*elements, *primary_elements, Spacer(1, 4 * mm)]

    cells = [
        [
            Paragraph(label, label_style),
            image,
        ]
        for _, label, image, _ in secondary_views
    ]
    if len(cells) % 2:
        cells.append([])
    grid_data = [cells[index:index + 2] for index in range(0, len(cells), 2)]
    grid = Table(grid_data, colWidths=[85 * mm, 85 * mm], hAlign="CENTER")
    grid.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#D8DEE5")),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return [*elements, *primary_elements, KeepTogether([grid]), Spacer(1, 4 * mm)]


def generate_quote_pdf(
    analysis: dict[str, Any],
    quote: dict[str, Any],
    preview: dict[str, Any] | None = None,
    viewer_model: dict[str, Any] | None = None,
) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CompactTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=19,
        spaceAfter=0,
    )
    note_style = ParagraphStyle(
        "CompactNote",
        parent=styles["Italic"],
        fontName="Helvetica-Oblique",
        fontSize=8.5,
        leading=10,
    )

    material = quote.get("material", {})
    costs = quote.get("estimated_internal_cost_eur", {})
    times = quote.get("estimated_times_min", {})
    laser = quote.get("laser_details", {})
    bending = quote.get("bending_details", {})
    geometry = analysis.get("geometry", {})
    manufacturability = analysis.get("manufacturability", {})
    holes = analysis.get("holes", {})
    cutting = analysis.get("cutting", {})
    coordinate_reference = analysis.get("coordinate_reference", {})
    config_used = quote.get("config_used", {})
    pricing = config_used.get("pricing", {})

    elements: list[Any] = [
        Paragraph("REVERSEPARTS - Preventivo tecnico interno", title_style),
        Spacer(1, 4 * mm),
    ]

    elements.extend(_preview_section(preview))
    elements.extend(
        _section(
            "Pezzo",
            _part_rows(analysis, quote),
        )
    )
    elements.extend(
        _section(
            "Costi",
            [
                ("Materiale", _value(costs.get("material"), "EUR")),
                ("Laser", _value(costs.get("laser"), "EUR")),
                ("Piegatura", _value(costs.get("bending"), "EUR")),
                ("CAD check", _value(costs.get("cad_check"), "EUR")),
                ("Handling", _value(costs.get("handling"), "EUR")),
                ("Setup", _value(costs.get("setup"), "EUR")),
                ("Totale costo interno", _value(costs.get("total"), "EUR")),
                ("Costo unitario interno", _value(costs.get("unit_cost"), "EUR")),
            ],
        )
    )
    elements.extend(
        _section(
            "Dettagli tecnici",
            [
                ("Tempo laser totale", _value(times.get("laser_cutting"), "min")),
                ("Tempo piegatura totale", _value(bending.get("bending_time_total_min"), "min")),
                ("Velocità taglio", _value(laser.get("cut_speed_mm_min"), "mm/min")),
                ("Pierce count", laser.get("pierce_count")),
                ("Tempo per piega", _value(bending.get("bending_time_sec_per_bend"), "sec")),
                ("Perimetro esterno stimato", _value(cutting.get("outer_cut_length_mm"), "mm")),
                ("Perimetro aperture interne", _value(cutting.get("inner_cut_length_mm"), "mm")),
                ("Lunghezza totale taglio", _value(cutting.get("total_cut_length_mm"), "mm")),
                ("Confidence taglio", cutting.get("confidence", "low")),
                ("Volume", _value(analysis.get("volume_cm3"), "cm3")),
                ("Superficie", _value(analysis.get("surface_area_cm2"), "cm2")),
                (
                    "Centro bounding box X / Y / Z",
                    _vector(
                        [
                            (geometry.get("bounding_box_center_mm") or {}).get(axis)
                            for axis in ("x", "y", "z")
                        ],
                        "mm",
                    ),
                ),
                (
                    "Baricentro X / Y / Z",
                    _vector(
                        [
                            (geometry.get("center_of_mass_mm") or {}).get(axis)
                            for axis in ("x", "y", "z")
                        ],
                        "mm",
                    ),
                ),
                ("Solidi CAD", geometry.get("solid_count", "-")),
                ("Facce CAD (B-Rep)", geometry.get("face_count", "-")),
                ("Bordi topologici (B-Rep)", geometry.get("edge_count", "-")),
                ("Vertici topologici (B-Rep)", geometry.get("vertex_count", "-")),
                (
                    "Nota conteggi B-Rep",
                    "Descrivono la struttura matematica STEP; includono pareti dei fori e giunzioni delle superfici curve, non lavorazioni o spigoli fisici.",
                ),
                ("Diametro circolare min", _value(holes.get("min_circular_diameter_mm"), "mm")),
                ("Diametro circolare max", _value(holes.get("max_circular_diameter_mm"), "mm")),
                ("Distanza minima foro-bordo", _value(manufacturability.get("min_hole_to_edge_mm"), "mm")),
                ("Confidence foro-bordo", manufacturability.get("hole_to_edge_confidence", "low")),
                ("Distanza minima foro-foro", _value(manufacturability.get("min_hole_to_hole_mm"), "mm")),
                ("Confidence foro-foro", manufacturability.get("hole_to_hole_confidence", "low")),
                ("Distanza minima foro-piega", _value(manufacturability.get("min_hole_to_bend_mm"), "mm")),
                ("Confidence foro-piega", manufacturability.get("hole_to_bend_confidence", "low")),
                ("Riferimento coordinate", coordinate_reference.get("origin", "Original STEP file origin")),
                ("Assi coordinate", coordinate_reference.get("axes", "Original STEP X/Y/Z axes")),
            ],
        )
    )
    elements.extend(
        _detail_table(
            "Raggruppamento fori uguali",
            ["Quantita", "Tipo", "Misura"],
            _hole_group_rows(analysis),
        )
    )
    elements.extend(
        _detail_table(
            "Dettaglio fori",
            ["Foro", "Misura", "Perim.", "Area", "Centro", "Asse", "Bordo", "Altro foro", "Conf."],
            _hole_detail_rows(analysis),
        )
    )
    elements.extend(
        _detail_table(
            "Dettaglio pieghe",
            ["#", "Raggio", "Lunghezza", "Angolo", "Asse X/Y/Z", "Conf."],
            _bend_detail_rows(analysis),
        )
    )
    elements.extend(
        _section(
            "Parametri effettivi",
            [
                ("Override temporanei", "Si" if quote.get("overrides_used") else "No"),
                ("Densita materiale", _value(material.get("density_g_cm3"), "g/cm3")),
                ("Costo materiale", _value(material.get("cost_eur_kg"), "EUR/kg")),
                ("Tariffa laser", _value(pricing.get("laser_rate_eur_min"), "EUR/min")),
                ("Velocita taglio", _value(laser.get("cut_speed_mm_min"), "mm/min")),
                ("Tempo pierce", _value(laser.get("pierce_time_sec"), "sec")),
                ("Tariffa piegatura", _value(pricing.get("bending_rate_eur_min"), "EUR/min")),
                ("Tempo per piega", _value(bending.get("bending_time_sec_per_bend"), "sec")),
                ("Costo setup", _value(pricing.get("setup_cost_eur"), "EUR")),
                ("Minimo ordine", _value(pricing.get("minimum_order_value_eur"), "EUR")),
            ],
        )
    )
    verification_rows = _verification_rows(analysis, quote)
    if verification_rows:
        elements.extend(
            _section(
                "Avvisi di verifica",
                verification_rows,
            )
        )
    elements.append(
        Paragraph(
            "Preventivo tecnico preliminare. Il margine commerciale e il prezzo finale devono essere decisi dall'azienda.",
            note_style,
        )
    )

    document.build(elements)
    return buffer.getvalue()
