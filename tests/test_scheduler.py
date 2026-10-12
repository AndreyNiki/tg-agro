import asyncio
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.pdf_parser import DaySchedule, Lesson, Schedule, schedule_to_dict
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    daily_send_job,
    hourly_check_job,
    reschedule_group_jobs,
    schedule_group_jobs,
    weekly_weektype_refresh_job,
)
from schedule_bot.weektype import WeekFormula, load_week_formula

FIXTURE_PDF_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    storage.ensure_default_settings(connection, {"weektype_url": "https://example.com/week"})
    return connection


def _make_group(conn, name="Б-Мв-101", send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow"):
    group_id = storage.create_group(
        conn, name=name, source_page_url="https://example.com/schedules",
        pdf_source_url="/tmp/does-not-matter.pdf", send_time=send_time,
        check_interval_minutes=check_interval_minutes, tz_name=tz_name,
    )
    return group_id


def _seed_active_version(conn, group_id, pdf_cache_path=FIXTURE_PDF_PATH):
    schedule = Schedule(
        table_x0=0.0, table_x1=500.0, header_y0=0.0, header_y1=5.0,
        days={"Понедельник": DaySchedule(y0=0.0, y1=10.0, lessons=[Lesson(time="8.30-10.00", upper="Лекция", lower="Семинар")])},
    )
    storage.insert_new_version(
        conn, group_id, json.dumps(schedule_to_dict(schedule)), pdf_cache_path,
        datetime.now(timezone.utc).isoformat(),
    )


class FixedMonday(date):
    @classmethod
    def today(cls):
        return date(2026, 9, 14)


def _config():
    return MagicMock(
        pdf_cache_dir="/tmp/cache", admin_chat_id=999,
        default_daily_send_time="09:00", default_check_interval_minutes=60, default_timezone="Europe/Moscow",
    )


@pytest.mark.asyncio
async def test_hourly_check_job_swallows_sync_failure_instead_of_crashing(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    group_id = _make_group(conn)

    async def failing_run_sync(*args, **kwargs):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(scheduler_module, "_run_sync", failing_run_sync)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await hourly_check_job(conn, _config(), bot, group_id)  # must not raise


@pytest.mark.asyncio
async def test_hourly_check_job_notifies_admin_with_group_name_on_sync_failure(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    group_id = _make_group(conn, name="Б-Мв-101")

    async def failing_run_sync(*args, **kwargs):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(scheduler_module, "_run_sync", failing_run_sync)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await hourly_check_job(conn, _config(), bot, group_id)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 999
    assert "Б-Мв-101" in bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_hourly_check_job_skips_missing_group_without_crashing(conn):
    await hourly_check_job(conn, _config(), MagicMock(), group_id=9999)  # must not raise


@pytest.mark.asyncio
async def test_hourly_check_job_survives_admin_notify_failure(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    group_id = _make_group(conn)

    async def failing_run_sync(*args, **kwargs):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(scheduler_module, "_run_sync", failing_run_sync)

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("bot blocked by admin"))

    await hourly_check_job(conn, _config(), bot, group_id)  # must not raise


@pytest.mark.asyncio
async def test_daily_send_job_sends_even_when_sync_fails(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    _seed_active_version(conn, group_id)

    async def failing_run_sync(*args, **kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr(scheduler_module, "_run_sync", failing_run_sync)
    monkeypatch.setattr(scheduler_module, "today_in_configured_timezone", lambda tz: FixedMonday.today())
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: None)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    await daily_send_job(conn, _config(), bot, group_id, holder)

    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_daily_send_job_isolates_per_subscriber_failures(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    _seed_active_version(conn, group_id)

    async def noop_run_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler_module, "_run_sync", noop_run_sync)
    monkeypatch.setattr(scheduler_module, "today_in_configured_timezone", lambda tz: FixedMonday.today())
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: None)

    bot = MagicMock()

    async def send_message(chat_id, text):
        if chat_id == 111:
            raise RuntimeError("bot blocked by user")

    bot.send_message = AsyncMock(side_effect=send_message)
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    await daily_send_job(conn, _config(), bot, group_id, holder)

    assert bot.send_message.await_count == 2
    sent_chat_ids = {call.kwargs["chat_id"] for call in bot.send_message.await_args_list}
    assert sent_chat_ids == {111, 222}


@pytest.mark.asyncio
async def test_daily_send_job_warns_when_group_last_sync_failed(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    _seed_active_version(conn, group_id)
    storage.set_group_last_sync_failed_at(conn, group_id, "2026-09-17T08:00:00+00:00")

    async def noop_run_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler_module, "_run_sync", noop_run_sync)
    monkeypatch.setattr(scheduler_module, "today_in_configured_timezone", lambda tz: FixedMonday.today())
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: None)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    await daily_send_job(conn, _config(), bot, group_id, holder)

    assert "могли устареть" in bot.send_message.await_args_list[0].kwargs["text"]


@pytest.mark.asyncio
async def test_daily_send_job_skips_missing_group_without_crashing(conn):
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    await daily_send_job(conn, _config(), MagicMock(), 9999, holder)  # must not raise


def test_build_scheduler_registers_one_job_pair_per_group(conn):
    group_a = _make_group(conn, name="A")
    group_b = _make_group(conn, name="B")
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, _config(), bot, holder)

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert {f"daily_send_{group_a}", f"hourly_check_{group_a}", f"startup_sync_{group_a}"}.issubset(job_ids)
    assert {f"daily_send_{group_b}", f"hourly_check_{group_b}", f"startup_sync_{group_b}"}.issubset(job_ids)
    assert "weekly_weektype_refresh" in job_ids
    scheduler.shutdown(wait=False)


def test_build_scheduler_binds_daily_trigger_to_group_timezone(conn):
    group_id = _make_group(conn, tz_name="Asia/Yekaterinburg")
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, _config(), bot, holder)

    job = scheduler.get_job(f"daily_send_{group_id}")
    assert str(job.trigger.timezone) == "Asia/Yekaterinburg"
    scheduler.shutdown(wait=False)


def test_build_scheduler_gives_each_group_its_own_lock(conn):
    """Regression test: build_scheduler used to create a SINGLE lock and
    pass the same object to every group's jobs, which would serialize
    sync across unrelated groups (different `active` rows, different
    cache files) — defeating the point of per-group job pairs. Each
    group's hourly_check job must hold a lock object distinct from every
    other group's."""
    group_a = _make_group(conn, name="A")
    group_b = _make_group(conn, name="B")
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, _config(), bot, holder)

    lock_a = scheduler.get_job(f"hourly_check_{group_a}").args[-1]
    lock_b = scheduler.get_job(f"hourly_check_{group_b}").args[-1]
    assert lock_a is not lock_b

    async def _acquire_both_without_blocking():
        await asyncio.wait_for(lock_a.acquire(), timeout=1)
        try:
            # group B's lock must be acquirable while group A's is held —
            # if they were the same object this would deadlock/time out.
            await asyncio.wait_for(lock_b.acquire(), timeout=1)
            lock_b.release()
        finally:
            lock_a.release()

    asyncio.run(_acquire_both_without_blocking())
    scheduler.shutdown(wait=False)


def test_reschedule_group_jobs_updates_trigger_time(conn):
    group_id = _make_group(conn)
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, _config(), bot, holder)

    storage.update_group_send_time(conn, group_id, "07:15")
    reschedule_group_jobs(scheduler, conn, _config(), bot, group_id, holder, lock=None)

    job = scheduler.get_job(f"daily_send_{group_id}")
    field_names = [f.name for f in job.trigger.fields]
    assert job.trigger.fields[field_names.index("hour")].expressions[0].first == 7
    assert job.trigger.fields[field_names.index("minute")].expressions[0].first == 15
    scheduler.shutdown(wait=False)


def test_reschedule_group_jobs_adds_jobs_for_a_brand_new_group(conn):
    """Creating a group after the scheduler already started must be able
    to add its jobs without a full scheduler rebuild — reschedule_group_jobs
    must work even when no job for this group_id exists yet."""
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, _config(), bot, holder)

    new_group_id = _make_group(conn, name="New-Group")
    reschedule_group_jobs(scheduler, conn, _config(), bot, new_group_id, holder, lock=None)

    assert scheduler.get_job(f"daily_send_{new_group_id}") is not None
    assert scheduler.get_job(f"hourly_check_{new_group_id}") is not None
    scheduler.shutdown(wait=False)


def test_reschedule_group_jobs_is_noop_for_deleted_group(conn):
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, _config(), bot, holder)

    reschedule_group_jobs(scheduler, conn, _config(), bot, 9999, holder, lock=None)  # must not raise
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_weekly_weektype_refresh_job_persists_successful_fetch(conn, monkeypatch):
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
