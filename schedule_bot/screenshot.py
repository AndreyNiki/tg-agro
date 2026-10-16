import fitz

from schedule_bot.pdf_parser import Schedule


def _crop_pixmap(
    page, y0: float, y1: float, x_ranges: list[tuple[float, float]], zoom: float, padding: float,
    *, y0_padding: float | None = None, y1_padding: float | None = None,
):
    """Renders one or two adjacent clip rects at the given y-range and
    stitches them horizontally if there are two (non-adjacent time/group
    columns); a single rect if there's only one (adjacent/merged case).
    Returns a fitz.Pixmap.

    y0_padding/y1_padding override `padding` on the top/bottom edge only
    (x-axis padding always uses `padding`). A day's row band is directly
    adjacent to the next band with no gap (one day's y1 equals the next
    day's y0), so symmetric padding there pulls in a sliver of the
    neighboring band/header — pass 0 on whichever edge borders another
    band to avoid that bleed-through."""
    top_pad = padding if y0_padding is None else y0_padding
    bottom_pad = padding if y1_padding is None else y1_padding
    pixmaps = [
        page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            clip=fitz.Rect(x0 - padding, max(y0 - top_pad, 0.0), x1 + padding, y1 + bottom_pad),
        )
        for x0, x1 in x_ranges
    ]
    # get_pixmap's result carries the clip rect's own device-space offset as
    # its origin (not (0, 0)) — Pixmap.copy()'s bbox is interpreted in that
    # shared device space, not translated, so copying straight into a
    # (0, 0)-based canvas at a nonzero offset silently reads/writes the
    # wrong region. set_origin re-anchors each piece to where it belongs on
    # the destination canvas before copy() runs.
    if len(pixmaps) == 1:
        pixmaps[0].set_origin(0, 0)
        return pixmaps[0]
    stitched = fitz.Pixmap(
        pixmaps[0].colorspace, (0, 0, sum(p.width for p in pixmaps), pixmaps[0].height), pixmaps[0].alpha
    )
    offset = 0
    for pixmap in pixmaps:
        pixmap.set_origin(offset, 0)
        stitched.copy(pixmap, pixmap.irect)
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
        header_pixmap = _crop_pixmap(
            page, schedule.header_y0, schedule.header_y1, x_ranges, zoom, padding, y1_padding=0.0,
        )
        body_pixmap = _crop_pixmap(
            page, day_schedule.y0, day_schedule.y1, x_ranges, zoom, padding, y0_padding=0.0,
        )

        combined = fitz.Pixmap(
            body_pixmap.colorspace,
            (0, 0, body_pixmap.width, header_pixmap.height + body_pixmap.height),
            body_pixmap.alpha,
        )
        combined.copy(header_pixmap, header_pixmap.irect)
        body_pixmap.set_origin(0, header_pixmap.height)
        combined.copy(body_pixmap, body_pixmap.irect)
        return combined.tobytes("png")
    finally:
        document.close()
