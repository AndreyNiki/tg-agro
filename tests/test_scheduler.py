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
async def test_institute_catalog_refresh_job_schedules_jobs_for_newly_found_link(shutdown_scheduler, monkeypatch):
    """A link discovered by the BACKGROUND catalog scan must get its
    link_check_*/link_daily_send_* jobs immediately — not only after a
    process restart (the manual rescan paths already reschedule)."""
    import schedule_bot.scheduler as scheduler_module

    # check_same_thread=False: refresh_institute_catalog runs in
    # asyncio.to_thread here, exactly as in production (see main.py).
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    storage.init_db(connection)
    storage.ensure_default_settings(connection, {"weektype_url": "https://example.com/week"})

    institute_id = _make_institute(connection)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = shutdown_scheduler(build_scheduler(connection, _config(), bot, holder))

    assert not [job.id for job in scheduler.get_jobs() if job.id.startswith("link_")]

    created: dict = {}

    def fake_refresh(conn_, institute, *, notify_admin):
        created["link_id"] = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/new.pdf",
            group_names=["Б-А-301"], raw_label="Б-А-301", last_seen_at="t",
        )

    monkeypatch.setattr(scheduler_module, "refresh_institute_catalog", fake_refresh)

    job = scheduler.get_job(f"institute_catalog_refresh_{institute_id}")
    await job.func(*job.args)

    job_ids = {j.id for j in scheduler.get_jobs()}
    # Shut down inside the test: this scheduler is bound to the
    # pytest-asyncio event loop, which is closed before fixture teardown.
    scheduler.shutdown(wait=False)
    assert f"link_check_{created['link_id']}" in job_ids
    assert f"link_daily_send_{created['link_id']}" in job_ids


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


def test_build_scheduler_sets_generous_misfire_grace_time(conn, shutdown_scheduler):
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))

    assert scheduler._job_defaults["misfire_grace_time"] >= 3600
    assert scheduler._job_defaults["coalesce"] is True


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
async def test_link_check_job_shares_catalog_rescan_state_across_calls(conn, monkeypatch):
    # The rate-limiting structure must be created ONCE and passed into
    # every link_check_job call — not freshly created inside
    # link_check_job itself (which would defeat its whole purpose of
    # rate-limiting catalog refreshes across all of one institute's links).
    import schedule_bot.scheduler as scheduler_module

    institute_id = _make_institute(conn)
    link_a, _ = _make_link_and_group(conn, institute_id, pdf_url="a.pdf", name="A")
    link_b, _ = _make_link_and_group(conn, institute_id, pdf_url="b.pdf", name="B")

    seen_states = []

    async def recording_sync(conn_, institute_link, institute, pdf_cache_dir, notify, *, catalog_rescan_state, notify_admin=None):
        seen_states.append(catalog_rescan_state)
        catalog_rescan_state[institute.id] = 1.0
        return []

    monkeypatch.setattr(scheduler_module, "sync_institute_link", recording_sync)

    rescan_state: dict = {}
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await link_check_job(conn, _config(), bot, link_a, catalog_rescan_state=rescan_state)
    await link_check_job(conn, _config(), bot, link_b, catalog_rescan_state=rescan_state)

    assert seen_states[0] is seen_states[1] is rescan_state
    assert institute_id in rescan_state


def test_build_scheduler_shares_one_catalog_rescan_state_across_all_link_jobs(conn, shutdown_scheduler):
    """Every link's check AND daily-send job of every institute must get
    the identical rate-limit dict from a single build_scheduler call —
    otherwise "one catalog rescan per institute per cycle" degenerates
    into "one per link per call"."""
    institute_id = _make_institute(conn)
    link_a, _ = _make_link_and_group(conn, institute_id, pdf_url="a.pdf", name="A")
    link_b, _ = _make_link_and_group(conn, institute_id, pdf_url="b.pdf", name="B")
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))

    states = [
        scheduler.get_job(f"link_check_{link_a}").args[-1],
        scheduler.get_job(f"link_check_{link_b}").args[-1],
        scheduler.get_job(f"link_daily_send_{link_a}").args[-1],
        scheduler.get_job(f"link_daily_send_{link_b}").args[-1],
    ]
    assert all(isinstance(state, dict) for state in states)
    assert all(state is states[0] for state in states)


@pytest.mark.asyncio
async def test_sync_rate_limits_second_404_for_same_institute_through_scheduler_jobs(conn, shutdown_scheduler, monkeypatch):
    """End-to-end over the REAL sync function: two different links of one
    institute both 404 within one check interval -> exactly one catalog
    rescan, because both scheduled jobs share the rate-limit dict."""
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn, check_interval_minutes=60)
    link_a, group_a = _make_link_and_group(conn, institute_id, pdf_url="https://example.com/a.pdf", name="A")
    link_b, group_b = _make_link_and_group(conn, institute_id, pdf_url="https://example.com/b.pdf", name="B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 222, group_b, joined_at="t")

    monkeypatch.setattr(
        sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404"))
    )
    refresh_calls = []
    monkeypatch.setattr(
        sync_module, "refresh_institute_catalog",
        lambda conn_, institute, **kwargs: refresh_calls.append(institute.id),
    )

    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = shutdown_scheduler(build_scheduler(conn, _config(), bot, holder))

    job_a = scheduler.get_job(f"link_check_{link_a}")
    job_b = scheduler.get_job(f"link_check_{link_b}")
    await job_a.func(*job_a.args)
    await job_b.func(*job_b.args)
    scheduler.shutdown(wait=False)  # bound to the pytest-asyncio loop; see note above

    assert refresh_calls == [institute_id]
