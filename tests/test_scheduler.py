import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    reschedule_daily_job,
    reschedule_hourly_job,
)
from schedule_bot.weektype import WeekFormula


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


def test_build_scheduler_registers_daily_hourly_and_weekly_jobs(conn):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, MagicMock(pdf_cache_dir="/tmp/cache"), bot, holder)

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"daily_send", "hourly_check", "weekly_weektype_refresh"}


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
