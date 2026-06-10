from __future__ import annotations

from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


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
    table_data = [[Paragraph("<b>Voce</b>", styles["Normal"]), Paragraph("<b>Valore</b>", styles["Normal"])]]
    table_data.extend([[label, _value(value)] for label, value in rows])
    table = Table(table_data, colWidths=[70 * mm, 100 * mm])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAEFF5")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C2CC")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return [Paragraph(title, styles["Heading2"]), table, Spacer(1, 8 * mm)]


def generate_quote_pdf(analysis: dict[str, Any], quote: dict[str, Any]) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()

    material = quote.get("material", {})
    costs = quote.get("estimated_internal_cost_eur", {})
    times = quote.get("estimated_times_min", {})
    laser = quote.get("laser_details", {})
    bending = quote.get("bending_details", {})
    cutting = analysis.get("cutting", {})
    bends = analysis.get("bends", {})

    elements: list[Any] = [
        Paragraph("REVERSEPARTS - Preventivo tecnico interno", styles["Title"]),
        Spacer(1, 8 * mm),
    ]

    elements.extend(
        _section(
            "Pezzo",
            [
                ("Nome pezzo", quote.get("part_name") or analysis.get("part_name")),
                ("Materiale", material.get("name")),
                ("Quantita", quote.get("quantity")),
                ("Dimensioni mm", _dimensions(analysis)),
                ("Spessore", material.get("thickness_mm") or analysis.get("detected_thickness_mm")),
                ("Peso unitario stimato", _value(material.get("estimated_weight_kg"), "kg")),
                ("Lunghezza taglio totale", _value(cutting.get("total_cut_length_mm"), "mm")),
                ("Numero pieghe", bends.get("count") or quote.get("features_summary", {}).get("bends")),
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
                ("Velocita taglio", _value(laser.get("cut_speed_mm_min"), "mm/min")),
                ("Pierce count", laser.get("pierce_count")),
                ("Tempo per piega", _value(bending.get("bending_time_sec_per_bend"), "sec")),
            ],
        )
    )
    elements.append(
        Paragraph(
            "Preventivo tecnico preliminare. Il margine commerciale e il prezzo finale devono essere decisi dall'azienda.",
            styles["Italic"],
        )
    )

    document.build(elements)
    return buffer.getvalue()
