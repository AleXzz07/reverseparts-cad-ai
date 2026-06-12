from __future__ import annotations

import base64
import binascii
from io import BytesIO
from typing import Any

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
    table_data.extend([[label, _value(value)] for label, value in rows])
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
    encoded = (preview or {}).get("image_png_base64")
    if not encoded:
        return [
            *elements,
            Paragraph("Anteprima pezzo non disponibile", fallback_style),
            Spacer(1, 4 * mm),
        ]

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
        max_width = 120 * mm
        max_height = 72 * mm
        scale = min(max_width / image.imageWidth, max_height / image.imageHeight, 1.0)
        image.drawWidth = image.imageWidth * scale
        image.drawHeight = image.imageHeight * scale
        image.hAlign = "CENTER"
        return [*elements, image, Spacer(1, 4 * mm)]
    except (binascii.Error, OSError, TypeError, ValueError):
        return [
            *elements,
            Paragraph("Anteprima pezzo non disponibile", fallback_style),
            Spacer(1, 4 * mm),
        ]


def generate_quote_pdf(
    analysis: dict[str, Any],
    quote: dict[str, Any],
    preview: dict[str, Any] | None = None,
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
    cutting = analysis.get("cutting", {})
    bends = analysis.get("bends", {})
    features = quote.get("features_summary", {})
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
            [
                ("Nome pezzo", quote.get("part_name") or analysis.get("part_name")),
                ("Materiale", material.get("name")),
                ("Quantità", quote.get("quantity")),
                ("Dimensioni mm", _dimensions(analysis)),
                ("Spessore", material.get("thickness_mm") or analysis.get("detected_thickness_mm")),
                ("Peso unitario stimato", _value(material.get("estimated_weight_kg"), "kg")),
                ("Lunghezza taglio totale", _value(cutting.get("total_cut_length_mm"), "mm")),
                ("Fori circolari", features.get("circular_holes")),
                ("Fori poligonali", features.get("polygonal_holes")),
                ("Fori sagomati/imbutiti", features.get("formed_holes")),
                ("Fori totali", features.get("total_holes")),
                ("Numero pieghe", bends.get("count") or features.get("bends")),
            ],
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
            ],
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
    elements.append(
        Paragraph(
            "Preventivo tecnico preliminare. Il margine commerciale e il prezzo finale devono essere decisi dall'azienda.",
            note_style,
        )
    )

    document.build(elements)
    return buffer.getvalue()
