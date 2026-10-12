import fitz

from schedule_bot.pdf_parser import Schedule


def _crop_pixmap(page, y0: float, y1: float, x_ranges: list[tuple[float, float]], zoom: float, padding: float):
    """Renders one or two adjacent clip rects at the given y-range and
    stitches them horizontally if there are two (non-adjacent time/group
    columns); a single rect if there's only one (adjacent/merged case).
    Returns a fitz.Pixmap."""
    pixmaps = [
        page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            clip=fitz.Rect(x0 - padding, max(y0 - padding, 0.0), x1 + padding, y1 + padding),
        )
        for x0, x1 in x_ranges
    ]
    if len(pixmaps) == 1:
        return pixmaps[0]
    stitched = fitz.Pixmap(
        pixmaps[0].colorspace, (0, 0, sum(p.width for p in pixmaps), pixmaps[0].height), pixmaps[0].alpha
    )
    offset = 0
    for pixmap in pixmaps:
        stitched.copy(pixmap, (offset, 0, offset + pixmap.width, pixmap.height))
        offset += pixmap.width
    return stitched


def _x_ranges_for(
    time_x0: float, time_x1: float, group_x0: float, group_x1: float, padding: float
) -> list[tuple[float, float]]:
    if time_x1 + padding >= group_x0 - padding:
        return [(time_x0, group_x1)]
    return [(time_x0, time_x1), (group_x0, group_x1)]


def render_day_image(
    pdf_path: str, schedule: Schedule, day: str, zoom: float = 2.5, padding: float = 4.0
) -> bytes | None:
    day_schedule = schedule.days.get(day)
    if day_schedule is None or not day_schedule.lessons:
        return None
    if day_schedule.group_col_x0 is None or day_schedule.group_col_x1 is None:
        return None
    if day_schedule.time_col_x0 is None or day_schedule.time_col_x1 is None:
        return None

    x_ranges = _x_ranges_for(
        day_schedule.time_col_x0, day_schedule.time_col_x1,
        day_schedule.group_col_x0, day_schedule.group_col_x1, padding,
    )

    document = fitz.open(pdf_path)
    try:
        page = document[0]
        header_pixmap = _crop_pixmap(page, schedule.header_y0, schedule.header_y1, x_ranges, zoom, padding)
        body_pixmap = _crop_pixmap(page, day_schedule.y0, day_schedule.y1, x_ranges, zoom, padding)

        combined = fitz.Pixmap(
            body_pixmap.colorspace,
            (0, 0, body_pixmap.width, header_pixmap.height + body_pixmap.height),
            body_pixmap.alpha,
        )
        combined.copy(header_pixmap, (0, 0, header_pixmap.width, header_pixmap.height))
        combined.copy(body_pixmap, (0, header_pixmap.height, body_pixmap.width, header_pixmap.height + body_pixmap.height))
        return combined.tobytes("png")
    finally:
        document.close()
