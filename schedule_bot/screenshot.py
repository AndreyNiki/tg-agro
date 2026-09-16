import fitz

from schedule_bot.pdf_parser import Schedule


def render_day_image(
    pdf_path: str, schedule: Schedule, day: str, zoom: float = 2.5, padding: float = 4.0
) -> bytes | None:
    day_schedule = schedule.days.get(day)
    if day_schedule is None or not day_schedule.lessons:
        return None

    document = fitz.open(pdf_path)
    try:
        page = document[0]
        clip = fitz.Rect(
            schedule.table_x0,
            max(day_schedule.y0 - padding, 0.0),
            schedule.table_x1,
            day_schedule.y1 + padding,
        )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        return pixmap.tobytes("png")
    finally:
        document.close()
