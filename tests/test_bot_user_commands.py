from datetime import date
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import (
    DAY_LABELS,
    _next_occurrence_of,
    build_day_keyboard,
    day_index_from_callback,
    start_command,
    stop_command,
    schedule_command,
    day_callback,
)
from schedule_bot.pdf_parser import DAY_ORDER, Schedule, DaySchedule, Lesson, schedule_to_dict
from schedule_bot.weektype import WeekFormula, WEEK_UPPER


def test_build_day_keyboard_has_one_button_per_day():
    keyboard = build_day_keyboard()
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == len(DAY_ORDER) == len(DAY_LABELS)
    assert [button.callback_data for button in buttons] == [f"day:{i}" for i in range(len(DAY_ORDER))]


def test_day_index_from_callback_parses_valid_data():
    assert day_index_from_callback("day:3") == 3


def test_day_index_from_callback_rejects_bad_data():
    with pytest.raises(ValueError):
        day_index_from_callback("nope")


def make_context(conn, week_formula, active_schedule, pdf_cache_path="/tmp/does-not-matter.pdf"):
    context = MagicMock()
    context.bot_data = {
        "conn": conn,
        "group_name": "Б-Мв-101",
        "week_formula": week_formula,
        "active_schedule_getter": lambda: active_schedule,
        "active_pdf_path_getter": lambda: pdf_cache_path,
    }
    return context


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


@pytest.fixture
def sample_schedule():
    return Schedule(
        table_x0=0.0,
        table_x1=500.0,
        days={
            "Среда": DaySchedule(
                y0=0.0, y1=10.0, lessons=[Lesson(time="8.30-10.00", upper="Лекция", lower="Семинар")]
            )
        },
    )


@pytest.mark.asyncio
async def test_schedule_command_sends_day_keyboard(conn, sample_schedule):
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = make_context(conn, formula, sample_schedule)

    await schedule_command(update, context)

    update.message.reply_text.assert_awaited_once()
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_day_callback_sends_text_and_image_when_lessons_exist(conn, sample_schedule, monkeypatch):
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday_index = DAY_ORDER.index("Среда")

    update = MagicMock()
    update.callback_query.data = f"day:{wednesday_index}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, formula, sample_schedule)

    import schedule_bot.bot as bot_module
    monkeypatch.setattr(bot_module, "render_day_image", lambda *args, **kwargs: b"fake-png-bytes")

    await day_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_day_callback_skips_image_when_no_lessons(conn, sample_schedule, monkeypatch):
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    monday_index = DAY_ORDER.index("Понедельник")  # not present in sample_schedule.days

    update = MagicMock()
    update.callback_query.data = f"day:{monday_index}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, formula, sample_schedule)

    import schedule_bot.bot as bot_module
    monkeypatch.setattr(bot_module, "render_day_image", lambda *args, **kwargs: None)

    await day_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_callback_replies_gracefully_when_no_active_schedule(conn):
    """C1 regression: fresh deploy with no schedule version must not crash."""
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday_index = DAY_ORDER.index("Среда")

    update = MagicMock()
    update.callback_query.data = f"day:{wednesday_index}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, formula, active_schedule=None, pdf_cache_path=None)

    await day_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_callback_skips_image_when_pdf_path_is_none(conn, sample_schedule):
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday_index = DAY_ORDER.index("Среда")

    update = MagicMock()
    update.callback_query.data = f"day:{wednesday_index}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, formula, sample_schedule, pdf_cache_path=None)

    await day_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_callback_swallows_render_error_and_sends_text_only(conn, sample_schedule, monkeypatch):
    """I4 regression: a missing/stale cached PDF must not crash the response."""
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday_index = DAY_ORDER.index("Среда")

    update = MagicMock()
    update.callback_query.data = f"day:{wednesday_index}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, formula, sample_schedule, pdf_cache_path="/does/not/exist.pdf")

    import schedule_bot.bot as bot_module

    def raise_error(*args, **kwargs):
        raise RuntimeError("file not found")

    monkeypatch.setattr(bot_module, "render_day_image", raise_error)

    await day_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()


def test_next_occurrence_of_returns_today_when_matching():
    today = date(2026, 9, 16)  # Wednesday
    assert _next_occurrence_of(2, today) == today  # index 2 == Среда


def test_next_occurrence_of_returns_upcoming_day_when_today_is_sunday():
    today = date(2026, 9, 20)  # Sunday
    monday_index = 0
    assert _next_occurrence_of(monday_index, today) == date(2026, 9, 21)


@pytest.mark.asyncio
async def test_day_callback_uses_upcoming_date_not_today_for_non_today_day(conn, monkeypatch):
    """I6 regression: picking a day other than today must use that day's nearest
    upcoming date for both the week-type computation and the message header."""
    schedule = Schedule(
        table_x0=0.0,
        table_x1=500.0,
        days={
            "Понедельник": DaySchedule(
                y0=0.0, y1=10.0, lessons=[Lesson(time="8.30-10.00", upper="Лекция", lower="Семинар")]
            )
        },
    )

    class RecordingFormula:
        def __init__(self):
            self.calls = []

        def compute(self, on_date):
            self.calls.append(on_date)
            return WEEK_UPPER

    import schedule_bot.bot as bot_module

    monkeypatch.setattr(bot_module, "date", date)  # ensure real date module used

    fixed_today = date(2026, 9, 20)  # Sunday

    class FixedDate(date):
        @classmethod
        def today(cls):
            return fixed_today

    monkeypatch.setattr(bot_module, "date", FixedDate)

    monday_index = DAY_ORDER.index("Понедельник")
    update = MagicMock()
    update.callback_query.data = f"day:{monday_index}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()

    formula = RecordingFormula()
    context = make_context(conn, formula, schedule, pdf_cache_path=None)

    await day_callback(update, context)

    expected_date = date(2026, 9, 21)  # next Monday after Sunday 2026-09-20
    assert formula.calls == [expected_date]
    sent_text = update.callback_query.message.reply_text.call_args[0][0]
    assert expected_date.strftime("%d.%m.%Y") in sent_text
