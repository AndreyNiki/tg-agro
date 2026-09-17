import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.pdf_parser import DAY_ORDER, DaySchedule, Lesson, Schedule, schedule_to_dict
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    daily_send_job,
    hourly_check_job,
    reschedule_daily_job,
    reschedule_hourly_job,
    resolve_sync_settings,
    weekly_weektype_refresh_job,
)
from schedule_bot.weektype import WeekFormula, load_week_formula

FIXTURE_PDF_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    storage.ensure_default_settings(
        connection,
        {
            "daily_send_time": "09:00",
            "check_interval_minutes": "60",
            "timezone": "Europe/Moscow",
            "pdf_source": "/tmp/does-not-matter.pdf",
            "group_name": "Б-Мв-101",
            "weektype_url": "https://example.com/week",
        },
    )
    return connection


def _seed_active_version(conn, group_name, pdf_cache_path=FIXTURE_PDF_PATH):
    schedule = Schedule(
        table_x0=0.0,
        table_x1=500.0,
        days={
            "Понедельник": DaySchedule(
                y0=0.0, y1=10.0, lessons=[Lesson(time="8.30-10.00", upper="Лекция", lower="Семинар")]
            )
        },
    )
    storage.insert_new_version(
        conn,
        group_name,
        json.dumps(schedule_to_dict(schedule)),
        pdf_cache_path,
        datetime.now(timezone.utc).isoformat(),
    )


class FixedMonday(date):
    @classmethod
    def today(cls):
        return date(2026, 9, 14)  # a Monday


@pytest.mark.asyncio
async def test_daily_send_job_sends_even_when_sync_fails(conn, monkeypatch):
    """I1 regression: a sync failure must not prevent the daily send."""
    import schedule_bot.scheduler as scheduler_module

    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    _seed_active_version(conn, "Б-Мв-101")

    async def failing_run_sync(*args, **kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr(scheduler_module, "_run_sync", failing_run_sync)
    monkeypatch.setattr(scheduler_module, "date", FixedMonday)
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: None)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    config = MagicMock(default_group_name="Б-Мв-101")

    await daily_send_job(conn, config, bot, holder)

    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_daily_send_job_isolates_per_subscriber_failures(conn, monkeypatch):
    """I2 regression: one subscriber's failure must not block the rest."""
    import schedule_bot.scheduler as scheduler_module

    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 222, "Б-Мв-101", "2026-09-16T00:00:00")
    _seed_active_version(conn, "Б-Мв-101")

    async def noop_run_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler_module, "_run_sync", noop_run_sync)
    monkeypatch.setattr(scheduler_module, "date", FixedMonday)
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: None)

    bot = MagicMock()

    async def send_message(chat_id, text):
        if chat_id == 111:
            raise RuntimeError("bot blocked by user")

    bot.send_message = AsyncMock(side_effect=send_message)
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    config = MagicMock(default_group_name="Б-Мв-101")

    await daily_send_job(conn, config, bot, holder)

    assert bot.send_message.await_count == 2
    sent_chat_ids = {call.kwargs["chat_id"] for call in bot.send_message.await_args_list}
    assert sent_chat_ids == {111, 222}


@pytest.mark.asyncio
async def test_daily_send_job_sends_day_screenshot_when_available(conn, monkeypatch):
    """I3 regression: the daily send must include the day screenshot when rendering succeeds."""
    import schedule_bot.scheduler as scheduler_module

    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    _seed_active_version(conn, "Б-Мв-101")

    async def noop_run_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler_module, "_run_sync", noop_run_sync)
    monkeypatch.setattr(scheduler_module, "date", FixedMonday)
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: b"fake-png-bytes")

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    config = MagicMock(default_group_name="Б-Мв-101")

    await daily_send_job(conn, config, bot, holder)

    bot.send_photo.assert_awaited_once_with(chat_id=111, photo=b"fake-png-bytes")


@pytest.mark.asyncio
async def test_daily_send_job_swallows_render_error_and_still_sends_text(conn, monkeypatch):
    """I4 regression: a missing/stale cached PDF must not crash the daily send."""
    import schedule_bot.scheduler as scheduler_module

    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    _seed_active_version(conn, "Б-Мв-101", pdf_cache_path="/does/not/exist.pdf")

    async def noop_run_sync(*args, **kwargs):
        return None

    def raise_error(*args, **kwargs):
        raise RuntimeError("file not found")

    monkeypatch.setattr(scheduler_module, "_run_sync", noop_run_sync)
    monkeypatch.setattr(scheduler_module, "date", FixedMonday)
    monkeypatch.setattr(scheduler_module, "render_day_image", raise_error)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    config = MagicMock(default_group_name="Б-Мв-101")

    await daily_send_job(conn, config, bot, holder)

    bot.send_message.assert_awaited_once()
    bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_send_job_warns_when_last_sync_failed(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    _seed_active_version(conn, "Б-Мв-101")
    storage.set_setting(conn, "last_sync_failed_at", "2026-09-17T08:00:00+00:00")

    async def noop_run_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler_module, "_run_sync", noop_run_sync)
    monkeypatch.setattr(scheduler_module, "date", FixedMonday)
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: None)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    config = MagicMock(default_group_name="Б-Мв-101")

    await daily_send_job(conn, config, bot, holder)

    sent_text = bot.send_message.await_args_list[0].kwargs["text"]
    assert "могли устареть" in sent_text


def test_resolve_sync_settings_uses_stored_values_when_present(conn):
    config = MagicMock(default_pdf_source="/default.pdf", default_group_name="Default-Group")

    pdf_source, group_name = resolve_sync_settings(conn, config)

    assert pdf_source == "/tmp/does-not-matter.pdf"
    assert group_name == "Б-Мв-101"


def test_resolve_sync_settings_falls_back_to_config_defaults_when_missing():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    config = MagicMock(default_pdf_source="/default.pdf", default_group_name="Default-Group")

    pdf_source, group_name = resolve_sync_settings(connection, config)

    assert pdf_source == "/default.pdf"
    assert group_name == "Default-Group"


def test_build_scheduler_registers_daily_hourly_and_weekly_jobs(conn):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, MagicMock(pdf_cache_dir="/tmp/cache"), bot, holder)

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"daily_send", "hourly_check", "weekly_weektype_refresh", "startup_sync"}


def test_build_scheduler_schedules_startup_sync_to_run_immediately(conn):
    """A fresh install has no active schedule yet — build_scheduler must
    kick off a one-off sync right away instead of waiting for the first
    hourly interval to elapse."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    before = datetime.now()
    scheduler = build_scheduler(conn, MagicMock(pdf_cache_dir="/tmp/cache"), bot, holder)
    after = datetime.now()

    job = scheduler.get_job("startup_sync")
    assert job.func is hourly_check_job
    run_date = job.trigger.run_date.replace(tzinfo=None)
    assert before <= run_date <= after


def test_reschedule_daily_job_updates_trigger_time(conn):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, MagicMock(pdf_cache_dir="/tmp/cache"), bot, holder)

    storage.set_setting(conn, "daily_send_time", "07:15")
    reschedule_daily_job(scheduler, conn, "Europe/Moscow")

    job = scheduler.get_job("daily_send")
    assert job.trigger.fields[  # cron trigger fields: hour, minute
        [f.name for f in job.trigger.fields].index("hour")
    ].expressions[0].first == 7
    assert job.trigger.fields[
        [f.name for f in job.trigger.fields].index("minute")
    ].expressions[0].first == 15


def test_build_scheduler_falls_back_to_config_default_on_corrupted_daily_send_time(conn):
    """C2 regression: a corrupted daily_send_time setting must not crash build_scheduler."""
    storage.set_setting(conn, "daily_send_time", "9h30")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    config = MagicMock(
        pdf_cache_dir="/tmp/cache",
        default_daily_send_time="09:00",
        default_check_interval_minutes=60,
        default_timezone="Europe/Moscow",
    )

    scheduler = build_scheduler(conn, config, bot, holder)

    job = scheduler.get_job("daily_send")
    assert job.trigger.fields[
        [f.name for f in job.trigger.fields].index("hour")
    ].expressions[0].first == 9
    assert job.trigger.fields[
        [f.name for f in job.trigger.fields].index("minute")
    ].expressions[0].first == 0


def test_build_scheduler_falls_back_to_config_default_on_corrupted_check_interval(conn):
    """C2 regression: a corrupted check_interval_minutes setting must not crash build_scheduler."""
    storage.set_setting(conn, "check_interval_minutes", "abc")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    config = MagicMock(
        pdf_cache_dir="/tmp/cache",
        default_daily_send_time="09:00",
        default_check_interval_minutes=60,
        default_timezone="Europe/Moscow",
    )

    scheduler = build_scheduler(conn, config, bot, holder)

    job = scheduler.get_job("hourly_check")
    assert job.trigger.interval.total_seconds() == 60 * 60


def test_reschedule_hourly_job_updates_interval(conn):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, MagicMock(pdf_cache_dir="/tmp/cache"), bot, holder)

    storage.set_setting(conn, "check_interval_minutes", "15")
    reschedule_hourly_job(scheduler, conn)

    job = scheduler.get_job("hourly_check")
    assert job.trigger.interval.total_seconds() == 15 * 60


@pytest.mark.asyncio
async def test_weekly_weektype_refresh_job_persists_successful_fetch(conn, monkeypatch):
    """C3: a successful weekly refresh should persist the new formula so restarts
    use the freshest known-good formula rather than a stale initial-deploy one."""
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    new_formula = WeekFormula(base_month=9, base_day=1, even_index_is_upper=False)

    import schedule_bot.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "fetch_week_formula", lambda url: new_formula)

    await weekly_weektype_refresh_job(conn, holder)

    assert holder.formula == new_formula
    assert load_week_formula(conn) == new_formula


@pytest.mark.asyncio
async def test_weekly_weektype_refresh_job_keeps_old_formula_on_failure(conn, monkeypatch):
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    import schedule_bot.scheduler as scheduler_module

    def raise_error(url):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(scheduler_module, "fetch_week_formula", raise_error)

    await weekly_weektype_refresh_job(conn, holder)

    assert holder.formula == WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    assert load_week_formula(conn) is None
