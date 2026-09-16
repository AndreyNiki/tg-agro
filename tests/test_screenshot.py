from pathlib import Path
from schedule_bot.pdf_parser import parse_schedule
from schedule_bot.screenshot import render_day_image

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


def test_render_day_image_returns_png_bytes_for_a_day_with_lessons():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    image_bytes = render_day_image(FIXTURE_PATH, schedule, "Среда")
    assert image_bytes is not None
    assert image_bytes[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_day_image_returns_none_for_a_day_with_no_lessons():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    assert render_day_image(FIXTURE_PATH, schedule, "Понедельник") is None


def test_render_day_image_returns_none_for_unknown_day():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    assert render_day_image(FIXTURE_PATH, schedule, "NotADay") is None
