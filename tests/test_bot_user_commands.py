from datetime import date
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import (
    DAY_LABELS,
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
