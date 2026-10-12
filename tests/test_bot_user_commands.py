from datetime import date
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import (
    DAY_LABELS,
    build_day_keyboard,
    day_date_from_callback,
    schedule_command,
    day_callback,
)
from schedule_bot.pdf_parser import Schedule, DaySchedule, Lesson, schedule_to_dict
from schedule_bot.weektype import WeekFormula, WEEK_UPPER


def test_build_day_keyboard_shows_remaining_and_next_week_with_dates():
    today = date(2026, 9, 16)  # Wednesday
    keyboard = build_day_keyboard(today)
    day_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("day:")
    ]
    # 4 remaining days this week (Ср..Сб) + 6 days next week
    assert len(day_buttons) == 4 + len(DAY_LABELS)
    dates = [date.fromisoformat(button.callback_data.split(":", 1)[1]) for button in day_buttons]
    assert dates[0] == today
    assert dates[3] == date(2026, 9, 19)  # Saturday, end of this week
    assert dates[4] == date(2026, 9, 21)  # Monday, start of next week
    assert day_buttons[0].text.endswith(today.strftime("%d.%m"))


def test_build_day_keyboard_on_sunday_shows_only_next_week():
    today = date(2026, 9, 20)  # Sunday
    keyboard = build_day_keyboard(today)
    day_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("day:")
    ]
    assert len(day_buttons) == len(DAY_LABELS)
    header_texts = [
        button.text for row in keyboard.inline_keyboard for button in row if button.callback_data == "noop"
    ]
    assert header_texts == ["— Следующая неделя —"]


def test_day_date_from_callback_parses_valid_data():
    assert day_date_from_callback("day:2026-09-16") == date(2026, 9, 16)


def test_day_date_from_callback_rejects_bad_data():
    with pytest.raises(ValueError):
        day_date_from_callback("nope")


def make_context(conn, group_id=None, week_formula=None, active_schedule=None, pdf_cache_path="/tmp/does-not-matter.pdf"):
    context = MagicMock()
    context.bot_data = {
        "conn": conn,
        "week_formula": week_formula,
        "active_schedule_getter": lambda gid: active_schedule,
        "active_pdf_path_getter": lambda gid: pdf_cache_path,
    }
    return context


def _make_group_and_member(conn, chat_id=111, name="Б-Мв-101"):
    group_id = storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, chat_id, group_id, joined_at="t", subscribed=True)
    return group_id


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
        header_y0=0.0,
        header_y1=5.0,
        days={
            "Среда": DaySchedule(
                y0=0.0, y1=10.0, lessons=[Lesson(time="8.30-10.00", upper="Лекция", lower="Семинар")]
            )
        },
    )


@pytest.mark.asyncio
async def test_schedule_command_sends_day_keyboard(conn, sample_schedule):
    group_id = _make_group_and_member(conn, chat_id=111)
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    update = MagicMock()
    update.effective_chat.id = 111
    update.message.reply_text = AsyncMock()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=sample_schedule)

    await schedule_command(update, context)

    update.message.reply_text.assert_awaited_once()
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_schedule_command_warns_when_last_sync_failed(conn, sample_schedule):
    group_id = _make_group_and_member(conn, chat_id=111)
    storage.set_group_last_sync_failed_at(conn, group_id, "2026-09-17T08:00:00+00:00")
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    update = MagicMock()
    update.effective_chat.id = 111
    update.message.reply_text = AsyncMock()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=sample_schedule)

    await schedule_command(update, context)

    sent_text = update.message.reply_text.call_args[0][0]
    assert "могли устареть" in sent_text


@pytest.mark.asyncio
async def test_schedule_command_no_warning_when_last_sync_ok(conn, sample_schedule):
    group_id = _make_group_and_member(conn, chat_id=111)
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    update = MagicMock()
    update.effective_chat.id = 111
    update.message.reply_text = AsyncMock()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=sample_schedule)

    await schedule_command(update, context)

    sent_text = update.message.reply_text.call_args[0][0]
    assert "могли устареть" not in sent_text


@pytest.mark.asyncio
async def test_day_callback_sends_text_and_image_when_lessons_exist(conn, sample_schedule, monkeypatch):
    group_id = _make_group_and_member(conn, chat_id=111)
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday = date(2026, 9, 16)

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{wednesday.isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=sample_schedule)

    import schedule_bot.bot as bot_module
    monkeypatch.setattr(bot_module, "render_day_image", lambda *args, **kwargs: b"fake-png-bytes")

    await day_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_day_callback_skips_image_when_no_lessons(conn, sample_schedule, monkeypatch):
    group_id = _make_group_and_member(conn, chat_id=111)
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    monday = date(2026, 9, 14)  # not present in sample_schedule.days

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{monday.isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=sample_schedule)

    import schedule_bot.bot as bot_module
    monkeypatch.setattr(bot_module, "render_day_image", lambda *args, **kwargs: None)

    await day_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_callback_replies_gracefully_when_no_active_schedule(conn):
    """C1 regression: fresh deploy with no schedule version must not crash."""
    group_id = _make_group_and_member(conn, chat_id=111)
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday = date(2026, 9, 16)

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{wednesday.isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=None, pdf_cache_path=None)

    await day_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_callback_skips_image_when_pdf_path_is_none(conn, sample_schedule):
    group_id = _make_group_and_member(conn, chat_id=111)
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday = date(2026, 9, 16)

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{wednesday.isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=sample_schedule, pdf_cache_path=None)

    await day_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_callback_swallows_render_error_and_sends_text_only(conn, sample_schedule, monkeypatch):
    """I4 regression: a missing/stale cached PDF must not crash the response."""
    group_id = _make_group_and_member(conn, chat_id=111)
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday = date(2026, 9, 16)

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{wednesday.isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=sample_schedule, pdf_cache_path="/does/not/exist.pdf")

    import schedule_bot.bot as bot_module

    def raise_error(*args, **kwargs):
        raise RuntimeError("file not found")

    monkeypatch.setattr(bot_module, "render_day_image", raise_error)

    await day_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_callback_uses_exact_picked_date_not_today(conn):
    """Regression: the callback now carries an explicit date, so picking a
    day from the next-week section must use that exact date for both the
    week-type computation and the message header — not date.today()."""
    group_id = _make_group_and_member(conn, chat_id=111)
    schedule = Schedule(
        table_x0=0.0,
        table_x1=500.0,
        header_y0=0.0,
        header_y1=5.0,
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

    picked_date = date(2026, 9, 21)  # a Monday picked from the "next week" section

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{picked_date.isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()

    formula = RecordingFormula()
    context = make_context(conn, group_id=group_id, week_formula=formula, active_schedule=schedule, pdf_cache_path=None)

    await day_callback(update, context)

    assert formula.calls == [picked_date]
    sent_text = update.callback_query.message.reply_text.call_args[0][0]
    assert picked_date.strftime("%d.%m.%Y") in sent_text
