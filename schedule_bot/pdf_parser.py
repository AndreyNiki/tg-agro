import io
from dataclasses import dataclass

import pdfplumber


@dataclass
class Cell:
    text: str | None
    x0: float
    x1: float


@dataclass
class RawRow:
    day_marker: bool
    time_label: str | None
    y0: float
    y1: float
    cells: list["Cell | None"]


def _load_pdf_bytes(source: str) -> bytes:
    if source.startswith("http://") or source.startswith("https://"):
        import requests

        response = requests.get(source, timeout=30)
        response.raise_for_status()
        return response.content
    with open(source, "rb") as handle:
        return handle.read()


def extract_raw_rows(source: str) -> tuple[list[RawRow], tuple[float, float, float, float]]:
    pdf_bytes = _load_pdf_bytes(source)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        table = page.find_tables()[0]
        texts = table.extract()
        rows: list[RawRow] = []
        for table_row, row_texts in zip(table.rows, texts):
            cells: list[Cell | None] = []
            for cell_bbox, text in zip(table_row.cells, row_texts):
                if cell_bbox is None:
                    cells.append(None)
                else:
                    cells.append(Cell(text=text, x0=cell_bbox[0], x1=cell_bbox[2]))
            time_label = cells[1].text if len(cells) > 1 and cells[1] is not None else None
            rows.append(
                RawRow(
                    day_marker=cells[0] is not None,
                    time_label=time_label or None,
                    y0=table_row.bbox[1],
                    y1=table_row.bbox[3],
                    cells=cells,
                )
            )
        return rows, table.bbox


def find_group_column_index(rows: list[RawRow], group_name: str) -> tuple[int, int]:
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(row.cells):
            if cell is not None and cell.text and group_name in cell.text:
                return row_index, col_index
    raise ValueError(f"Column for group {group_name!r} not found in table header")
