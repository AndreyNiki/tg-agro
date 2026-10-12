"""Standalone script: render a CSV report as a PDF table.

Usage:
    python3 scripts/csv_to_pdf.py --input report.csv --output report.pdf

Written for the output of find_surnames_in_schedules.py (CSV cells can
contain multiple newline-separated entries), but works for any CSV.

Requires a TrueType font with Cyrillic glyphs, since the built-in PDF base
fonts (Helvetica etc.) cannot render Cyrillic text. A handful of common
macOS/Linux font paths are tried automatically; pass --font to point at a
specific .ttf file if none of them are found on your system.
"""

import argparse
import csv
import os
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

FONT_BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def find_font(explicit: str | None, candidates: list[str]) -> str | None:
    if explicit and os.path.isfile(explicit):
        return explicit
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def register_fonts(font_path: str | None) -> tuple[str, str]:
    regular_path = find_font(font_path, FONT_CANDIDATES)
    if regular_path is None:
        sys.exit(
            "Не найден TTF-шрифт с кириллицей. Укажи путь явно через --font "
            "(например --font '/System/Library/Fonts/Supplemental/Arial.ttf')."
        )
    bold_path = find_font(None, FONT_BOLD_CANDIDATES) or regular_path

    pdfmetrics.registerFont(TTFont("ReportFont", regular_path))
    pdfmetrics.registerFont(TTFont("ReportFont-Bold", bold_path))
    return "ReportFont", "ReportFont-Bold"


def read_csv_rows(input_path: str) -> list[list[str]]:
    with open(input_path, encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def build_pdf(rows: list[list[str]], output_path: str, regular_font: str, bold_font: str) -> None:
    header_style = ParagraphStyle(
        "Header", fontName=bold_font, fontSize=10, leading=12, textColor=colors.white
    )
    cell_style = ParagraphStyle("Cell", fontName=regular_font, fontSize=8, leading=10)

    table_data = []
    for row_index, row in enumerate(rows):
        style = header_style if row_index == 0 else cell_style
        table_data.append([Paragraph(value.replace("\n", "<br/>"), style) for value in row])

    page_size = landscape(A3)
    usable_width = page_size[0] - 20 * mm
    num_cols = len(rows[0]) if rows else 1
    first_col_width = usable_width * 0.12
    other_col_width = (usable_width - first_col_width) / max(num_cols - 1, 1)
    col_widths = [first_col_width] + [other_col_width] * (num_cols - 1)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=page_size,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f4f4f")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
            ]
        )
    )
    doc.build([table])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Input CSV path")
    parser.add_argument("--output", default="report.pdf", help="Output PDF path")
    parser.add_argument("--font", default=None, help="Path to a .ttf font with Cyrillic glyphs")
    args = parser.parse_args()

    rows = read_csv_rows(args.input)
    if not rows:
        sys.exit(f"{args.input} пустой — нечего конвертировать.")

    regular_font, bold_font = register_fonts(args.font)
    build_pdf(rows, args.output, regular_font, bold_font)
    print(f"PDF written to {args.output}")


if __name__ == "__main__":
    main()
