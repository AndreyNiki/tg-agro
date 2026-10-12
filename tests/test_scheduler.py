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
    link_check_job,
    link_daily_send_job,
    reschedule_institute_link_jobs,
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


def _make_institute(conn, name="ИТ", send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow"):
    return storage.create_institute(
        conn, name=name, catalog_url="https://example.com/catalog",
        check_interval_minutes=check_interval_minutes, send_time=send_time, tz_name=tz_name,
        catalog_refresh_interval_minutes=10080,
    )


def _make_link_and_group(conn, institute_id, pdf_url="/tmp/does-not-matter.pdf", name="Б-Мв-101"):
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=pdf_url, group_names=[name], raw_label=name, last_seen_at="t"
    )
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)
    return link_id, group_id


def _seed_active_version(conn, group_id, pdf_cache_path=FIXTURE_PDF_PATH):
    schedule = Schedule(
        table_x0=0.0, table_x1=500.0, header_y0=0.0, header_y1=10.0,
        days={"Понедельник": DaySchedule(
            y0=0.0, y1=10.0, lessons=[Lesson(time="8.30-10.00", upper="Лекция", lower="Семинар")],
            time_col_x0=30.0, time_col_x1=60.0, group_col_x0=60.0, group_col_x1=300.0,
        )},
    )
    storage.insert_new_version(
        conn, group_id, json.dumps(schedule_to_dict(schedule)), pdf_cache_path, datetime.now(timezone.utc).isoformat(),
    )


class FixedMonday(date):
    @classmethod
    def today(cls):
        return date(2026, 9, 14)


def _config():
    return MagicMock(
        pdf_cache_dir="/tmp/cache", admin_chat_id=999,
        default_daily_send_time="09:00", default_check_interval_minutes=60, default_timezone="Europe/Moscow",
        default_catalog_refresh_interval_minutes=10080,
    )


@pytest.fixture
def shutdown_scheduler():
    schedulers = []

    def _register(scheduler):
        schedulers.append(scheduler)
        return scheduler

    yield _register
    for scheduler in schedulers:
        if scheduler.running:
            scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_link_check_job_swallows_sync_failure_instead_of_crashing(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    institute_id = _make_institute(conn)
    link_id, _ = _make_link_and_group(conn, institute_id)

    async def failing_sync(*args, **kwargs):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(scheduler_module, "sync_institute_link", failing_sync)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await link_check_job(conn, _config(), bot, link_id)  # must not raise


@pytest.mark.asyncio
async def test_link_check_job_notifies_admin_on_sync_failure(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    institute_id = _make_institute(conn)
    link_id, _ = _make_link_and_group(conn, institute_id, name="Б-Мв-101")

    async def failing_sync(*args, **kwargs):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(scheduler_module, "sync_institute_link", failing_sync)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    await link_check_job(conn, _config(), bot, link_id)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 999


@pytest.mark.asyncio
async def test_link_check_job_skips_missing_link_without_crashing(conn):
    await link_check_job(conn, _config(), MagicMock(), institute_link_id=9999)  # must not raise


@pytest.mark.asyncio
async def test_link_daily_send_job_sends_to_all_groups_of_the_link(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    institute_id = _make_institute(conn)
    link_id, group_id = _make_link_and_group(conn, institute_id)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    _seed_active_version(conn, group_id)

    async def noop_sync(*args, **kwargs):
        return []

    monkeypatch.setattr(scheduler_module, "sync_institute_link", noop_sync)
    monkeypatch.setattr(scheduler_module, "today_in_configured_timezone", lambda tz: FixedMonday.today())
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: None)

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    await link_daily_send_job(conn, _config(), bot, link_id, holder)

    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_link_daily_send_job_isolates_per_subscriber_failures(conn, monkeypatch):
    import schedule_bot.scheduler as scheduler_module

    institute_id = _make_institute(conn)
    link_id, group_id = _make_link_and_group(conn, institute_id)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    _seed_active_version(conn, group_id)

    async def noop_sync(*args, **kwargs):
        return []

    monkeypatch.setattr(scheduler_module, "sync_institute_link", noop_sync)
    monkeypatch.setattr(scheduler_module, "today_in_configured_timezone", lambda tz: FixedMonday.today())
    monkeypatch.setattr(scheduler_module, "render_day_image", lambda *a, **k: None)

    bot = MagicMock()

    async def send_message(chat_id, text):
        if chat_id == 111:
            raise RuntimeError("bot blocked by user")

    bot.send_message = AsyncMock(side_effect=send_message)
    bot.send_photo = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    await link_daily_send_job(conn, _config(), bot, link_id, holder)

    assert bot.send_message.await_count == 2


def test_build_scheduler_registers_link_and_catalog_jobs(conn, shutdown_scheduler):
    institute_id = _make_institute(conn)
    link_id, _ = _make_link_and_group(conn, institute_id)
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert f"link_check_{link_id}" in job_ids
    assert f"link_daily_send_{link_id}" in job_ids
    assert f"institute_catalog_refresh_{institute_id}" in job_ids
    assert "weekly_weektype_refresh" in job_ids


def test_build_scheduler_binds_daily_trigger_to_institute_timezone(conn, shutdown_scheduler):
    institute_id = _make_institute(conn, tz_name="Asia/Yekaterinburg")
    link_id, _ = _make_link_and_group(conn, institute_id)
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))

    job = scheduler.get_job(f"link_daily_send_{link_id}")
    assert str(job.trigger.timezone) == "Asia/Yekaterinburg"


def test_build_scheduler_gives_each_link_its_own_lock(conn, shutdown_scheduler):
    institute_id = _make_institute(conn)
    link_a, _ = _make_link_and_group(conn, institute_id, pdf_url="a.pdf", name="A")
    link_b, _ = _make_link_and_group(conn, institute_id, pdf_url="b.pdf", name="B")
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))

    lock_a = scheduler.get_job(f"link_check_{link_a}").args[-2]
    lock_b = scheduler.get_job(f"link_check_{link_b}").args[-2]
    assert lock_a is not lock_b


def test_build_scheduler_registers_n_independent_jobs_per_link(conn, shutdown_scheduler):
    # Binding constraint: jobs are scheduled per-LINK, not per-institute —
    # an institute with N links must get N independent check/daily_send
    # job pairs (each its own APScheduler job, independently triggerable),
    # not a single job that loops over all links sequentially.
    institute_id = _make_institute(conn)
    link_ids = [
        _make_link_and_group(conn, institute_id, pdf_url=f"{i}.pdf", name=f"G{i}")[0]
        for i in range(5)
    ]
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))

    job_ids = {job.id for job in scheduler.get_jobs()}
    for link_id in link_ids:
        assert f"link_check_{link_id}" in job_ids
        assert f"link_daily_send_{link_id}" in job_ids
    # Exactly one catalog-refresh job for the institute, regardless of
    # how many links it has.
    catalog_job_count = sum(1 for jid in job_ids if jid == f"institute_catalog_refresh_{institute_id}")
    assert catalog_job_count == 1
    # Each check job's target callable is link_check_job (not some
    # institute-level looping wrapper), confirming per-link dispatch.
    import schedule_bot.scheduler as scheduler_module
    for link_id in link_ids:
        job = scheduler.get_job(f"link_check_{link_id}")
        assert job.func is scheduler_module.link_check_job
        assert job.args[3] == link_id


def test_reschedule_institute_link_jobs_updates_trigger_time(conn, shutdown_scheduler):
    institute_id = _make_institute(conn)
    link_id, _ = _make_link_and_group(conn, institute_id)
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))

    storage.update_institute_send_time(conn, institute_id, "07:15")
    reschedule_institute_link_jobs(scheduler, conn, _config(), bot, link_id, holder, lock=None)

    job = scheduler.get_job(f"link_daily_send_{link_id}")
    field_names = [f.name for f in job.trigger.fields]
    assert job.trigger.fields[field_names.index("hour")].expressions[0].first == 7
    assert job.trigger.fields[field_names.index("minute")].expressions[0].first == 15


def test_reschedule_institute_link_jobs_is_noop_for_deleted_link(conn, shutdown_scheduler):
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))
    reschedule_institute_link_jobs(scheduler, conn, _config(), bot, 9999, holder, lock=None)  # must not raise


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
async def test_link_check_job_shares_catalog_refreshed_this_cycle_across_calls(conn, monkeypatch):
    # The rate-limiting set from Task 12 must be created ONCE per
    # scheduler build/run-cycle and passed into every link_check_job call
    # for that cycle — not freshly created inside link_check_job itself
    # (which would defeat its whole purpose of rate-limiting catalog
    # refreshes across all of one institute's links within a cycle).
    import schedule_bot.scheduler as scheduler_module

    institute_id = _make_institute(conn)
    link_a, _ = _make_link_and_group(conn, institute_id, pdf_url="a.pdf", name="A")
    link_b, _ = _make_link_and_group(conn, institute_id, pdf_url="b.pdf", name="B")

    seen_sets = []

    async def recording_sync(conn_, institute_link, institute, pdf_cache_dir, notify, *, catalog_refreshed_this_cycle):
        seen_sets.append(catalog_refreshed_this_cycle)
        catalog_refreshed_this_cycle.add(institute.id)
        return []

    monkeypatch.setattr(scheduler_module, "sync_institute_link", recording_sync)

    cycle_set: set = set()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await link_check_job(conn, _config(), bot, link_a, catalog_refreshed_this_cycle=cycle_set)
    await link_check_job(conn, _config(), bot, link_b, catalog_refreshed_this_cycle=cycle_set)

    assert seen_sets[0] is seen_sets[1] is cycle_set
    assert institute_id in cycle_set
