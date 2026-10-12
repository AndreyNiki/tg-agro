# Институты — план реализации, часть 3 (валидация, планировщик, admin/menu/wiring, чистка)

> Продолжение части 2. Global Constraints/Review Focus — в части 1.

## Фаза 6 — Валидация группы при подписке и немедленный sync

### Task 13: `group_commands.py` — институтский пикер, валидация непроверенной группы, немедленный sync

**Files:**
- Modify: `schedule_bot/group_commands.py` (полная замена: убрать `ConversationHandler` создания группы, добавить институтский пикер)
- Test: `tests/test_group_commands.py` (полная замена)

**Interfaces:**
- Consumes: `storage.{list_institutes, get_institute, list_groups, count_groups, get_group, update_group_is_verified, upsert_member, update_member_profile}` (Phase 1); `pdf_parser.{_load_pdf_bytes, find_group_column_index}` (Phase 3); `sync.sync_institute_link` (Task 12) via `context.bot_data["sync_institute_link_fn"]` (new bot_data key wired in Task 19).
- Produces: `build_institute_picker_keyboard(conn, page=0) -> InlineKeyboardMarkup`; `build_group_picker_keyboard(conn, institute_id, page=0) -> InlineKeyboardMarkup` (new required `institute_id` arg); `institute_select_callback`, `institute_page_callback`, `group_select_callback`, `group_page_callback`; `async def validate_and_sync_group_on_first_subscribe(conn, group, pdf_cache_dir, notify_admin, sync_institute_link_fn) -> bool` (returns `True` if the group is usable — `is_verified in (0 already resolved to 1, 1)` — `False` if it was just marked `-1`).

- [ ] **Step 1: Падающие тесты (замена `tests/test_group_commands.py`)**

```python
# tests/test_group_commands.py
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.group_commands import (
    build_group_picker_keyboard,
    build_institute_picker_keyboard,
    validate_and_sync_group_on_first_subscribe,
)

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_institute(conn, name="ИТ"):
    return storage.create_institute(
        conn, name=name, catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def _make_link(conn, institute_id, pdf_url=FIXTURE_PATH, names=("Б-Мв-101",)):
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=pdf_url, group_names=list(names), raw_label=",".join(names), last_seen_at="t"
    )
    group_ids = [storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name) for name in names]
    return link_id, group_ids


def test_build_institute_picker_keyboard_lists_institute_names(conn):
    _make_institute(conn, "Первый")
    _make_institute(conn, "Второй")
    keyboard = build_institute_picker_keyboard(conn)
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Первый" in texts
    assert "Второй" in texts


def test_build_group_picker_keyboard_hides_unverified_minus_one_groups(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Good", "Bad"))
    storage.update_group_is_verified(conn, group_ids[1], -1)
    keyboard = build_group_picker_keyboard(conn, institute_id)
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Good" in texts
    assert "Bad" not in texts


@pytest.mark.asyncio
async def test_validate_marks_group_verified_when_column_found(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101",))
    group = storage.get_group(conn, group_ids[0])
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    ok = await validate_and_sync_group_on_first_subscribe(conn, group, "/tmp/cache", notify_admin, sync_fn)

    assert ok is True
    assert storage.get_group(conn, group.id).is_verified == 1
    sync_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_marks_group_invalid_and_alerts_admin_when_column_missing(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("НЕТ-ТАКОЙ-ГРУППЫ",))
    group = storage.get_group(conn, group_ids[0])
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    ok = await validate_and_sync_group_on_first_subscribe(conn, group, "/tmp/cache", notify_admin, sync_fn)

    assert ok is False
    assert storage.get_group(conn, group.id).is_verified == -1
    notify_admin.assert_awaited_once()
    sync_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_marks_sibling_groups_of_same_link_in_one_pass(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101", "НЕТ-ТАКОЙ-ГРУППЫ"))
    good_group = storage.get_group(conn, group_ids[0])
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    await validate_and_sync_group_on_first_subscribe(conn, good_group, "/tmp/cache", notify_admin, sync_fn)

    assert storage.get_group(conn, group_ids[0]).is_verified == 1
    assert storage.get_group(conn, group_ids[1]).is_verified == -1


@pytest.mark.asyncio
async def test_validate_is_skipped_for_already_verified_group(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101",))
    storage.update_group_is_verified(conn, group_ids[0], 1)
    group = storage.get_group(conn, group_ids[0])
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    ok = await validate_and_sync_group_on_first_subscribe(conn, group, "/tmp/cache", notify_admin, sync_fn)

    assert ok is True
    sync_fn.assert_not_awaited()  # already verified -> no re-validation, no forced immediate sync
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/group_commands.py — full replacement
import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.join_requests import create_join_request_and_notify, requires_approval
from schedule_bot.pdf_parser import _load_pdf_bytes, find_group_column_index, extract_raw_rows
from schedule_bot.telegram_utils import reply

logger = logging.getLogger(__name__)

INSTITUTES_PER_PAGE = 8
GROUPS_PER_PAGE = 8

INSTITUTE_SELECT_PREFIX = "inst:select:"
INSTITUTE_PAGE_PREFIX = "inst:page:"
SELECT_PREFIX = "grp:select:"
PAGE_PREFIX = "grp:page:"

INSTITUTE_SELECT_CALLBACK_PATTERN = r"^inst:select:\d+$"
INSTITUTE_PAGE_CALLBACK_PATTERN = r"^inst:page:\d+$"
SELECT_CALLBACK_PATTERN = r"^grp:select:\d+:\d+$"
PAGE_CALLBACK_PATTERN = r"^grp:page:\d+:\d+$"


def _format_telegram_user_label(user) -> str:
    display_name = user.full_name
    username = user.username
    if display_name and username:
        return f"{display_name} (@{username})"
    if display_name:
        return display_name
    if username:
        return f"@{username}"
    return "Неизвестный"


def build_institute_picker_keyboard(conn, page: int = 0) -> InlineKeyboardMarkup:
    offset = page * INSTITUTES_PER_PAGE
    institutes = storage.list_institutes(conn)[offset : offset + INSTITUTES_PER_PAGE]
    total = len(storage.list_institutes(conn))

    rows = [[InlineKeyboardButton(i.name, callback_data=f"{INSTITUTE_SELECT_PREFIX}{i.id}")] for i in institutes]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{INSTITUTE_PAGE_PREFIX}{page - 1}"))
    if offset + INSTITUTES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{INSTITUTE_PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


def build_group_picker_keyboard(conn, institute_id: int, page: int = 0) -> InlineKeyboardMarkup:
    offset = page * GROUPS_PER_PAGE
    groups = storage.list_groups(conn, institute_id, limit=GROUPS_PER_PAGE, offset=offset)
    total = storage.count_groups(conn, institute_id)

    rows = [
        [InlineKeyboardButton(group.name, callback_data=f"{SELECT_PREFIX}{institute_id}:{group.id}")]
        for group in groups
    ]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{PAGE_PREFIX}{institute_id}:{page - 1}"))
    if offset + GROUPS_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{PAGE_PREFIX}{institute_id}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


async def institute_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(INSTITUTE_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    await query.message.edit_reply_markup(reply_markup=build_institute_picker_keyboard(conn, page))


async def institute_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    institute_id = int(query.data.removeprefix(INSTITUTE_SELECT_PREFIX))
    conn = context.bot_data["conn"]
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        await query.message.reply_text("Этот институт больше не существует.")
        return
    await query.message.edit_text(
        f"Институт «{institute.name}». Выберите группу:",
        reply_markup=build_group_picker_keyboard(conn, institute_id),
    )


async def group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    institute_id_str, page_str = query.data.removeprefix(PAGE_PREFIX).split(":")
    conn = context.bot_data["conn"]
    await query.message.edit_reply_markup(
        reply_markup=build_group_picker_keyboard(conn, int(institute_id_str), int(page_str))
    )


def _blocking_validate_group_names(pdf_url: str, group_names: list[str]) -> dict[str, bool]:
    pdf_bytes = _load_pdf_bytes(pdf_url)
    tmp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp_file.write(pdf_bytes)
        tmp_file.close()
        rows, _ = extract_raw_rows(tmp_file.name)
        found = {}
        for name in group_names:
            try:
                find_group_column_index(rows, name)
                found[name] = True
            except ValueError:
                found[name] = False
        return found
    finally:
        try:
            os.unlink(tmp_file.name)
        except OSError:
            pass


async def validate_and_sync_group_on_first_subscribe(conn, group, pdf_cache_dir: str, notify_admin, sync_institute_link_fn) -> bool:
    """Design spec section 1.5/4.2: a group straight from catalog scanning
    (is_verified == 0) is unproven until someone actually tries to
    subscribe. On that first attempt, validate every sibling group of the
    same institute_link against the real PDF in one pass (one
    download+parse, reused by the immediate first sync that follows on
    success). Already-verified groups (1 or -1) skip this entirely."""
    if group.is_verified != 0:
        return group.is_verified == 1

    institute_link = storage.get_institute_link(conn, group.institute_link_id)
    sibling_groups = storage.list_groups_for_link(conn, institute_link.id)
    sibling_names = [g.name for g in sibling_groups]

    try:
        found = await asyncio.to_thread(_blocking_validate_group_names, institute_link.pdf_url, sibling_names)
    except Exception as error:
        logger.warning("group_commands: validation fetch failed for link %r: %s", institute_link.pdf_url, error)
        await notify_admin(f"Не удалось скачать PDF для проверки группы «{group.name}»: {error}")
        return False

    for sibling in sibling_groups:
        storage.update_group_is_verified(conn, sibling.id, 1 if found.get(sibling.name) else -1)

    if not found.get(group.name):
        await notify_admin(f"Группа «{group.name}» не найдена в PDF по ссылке института — is_verified=-1, скрыта из пикера.")
        return False

    institute = storage.get_institute(conn, group.institute_id)
    await sync_institute_link_fn(institute_link.id)
    return True


async def group_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, institute_id_str, group_id_str = query.data.removeprefix(SELECT_PREFIX).rsplit(":", 1) if False else (None, None, None)
    # callback_data shape is "grp:select:<institute_id>:<group_id>"
    institute_id_str, group_id_str = query.data.removeprefix(SELECT_PREFIX).split(":")
    group_id = int(group_id_str)
    conn = context.bot_data["conn"]
    group = storage.get_group(conn, group_id)
    if group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return

    chat_id = update.effective_chat.id

    if requires_approval(conn, group, chat_id):
        if storage.get_pending_request_for_chat(conn, chat_id) is not None:
            await query.message.reply_text("У вас уже есть заявка на рассмотрении.")
            return
        requester_label = _format_telegram_user_label(update.effective_user)
        await create_join_request_and_notify(
            context.bot, conn, chat_id, None, group, context.bot_data["admin_chat_id"],
            requester_label=requester_label,
        )
        await query.message.reply_text("Эта группа закрыта или ограничена для вас — заявка отправлена, ожидайте решения.")
        return

    async def notify_admin(text: str) -> None:
        try:
            await context.bot.send_message(chat_id=context.bot_data["admin_chat_id"], text=text)
        except Exception as error:
            logger.warning("group_commands: failed to notify admin: %s", error)

    ok = await validate_and_sync_group_on_first_subscribe(
        conn, group, context.bot_data["config"].pdf_cache_dir, notify_admin,
        context.bot_data["sync_institute_link_fn"],
    )
    if not ok:
        await query.message.reply_text("Группа временно недоступна, администратор уведомлён.")
        return

    storage.upsert_member(conn, chat_id, group_id, joined_at=datetime.now(timezone.utc).isoformat(), subscribed=True)
    storage.update_member_profile(
        conn, chat_id,
        telegram_username=update.effective_user.username,
        display_name=update.effective_user.full_name,
    )
    await query.message.reply_text(
        f"Вы подписались на рассылку расписания группы «{group.name}». "
        "Команда /schedule покажет расписание на выбранный день."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    logger.info("group_commands: /start from chat_id=%s", chat_id)
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Выберите институт:", reply_markup=build_institute_picker_keyboard(conn))
        return

    group = storage.get_group(conn, member.group_id)
    group_name = group.name if group else "—"
    status = "подписаны на рассылку" if member.subscribed else "отписаны от рассылки (группа остаётся закреплённой)"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data="menu:root:_")]])
    await update.message.reply_text(
        f"Вы {status} группы «{group_name}». Команда /schedule покажет расписание на выбранный день.",
        reply_markup=keyboard,
    )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Сначала выберите группу: /start")
        return
    storage.set_member_subscribed(conn, chat_id, True)
    await reply(update, "Рассылка расписания включена.")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Вы ещё не выбрали группу: /start")
        return
    storage.set_member_subscribed(conn, chat_id, False)
    await reply(update, "Рассылка отключена. Группа остаётся закреплённой, /schedule продолжит работать.")
```

Fix the stray dead-code line in `group_select_callback` left over from drafting (`_, institute_id_str, group_id_str = ... if False else (None, None, None)`) — delete it; only the real `institute_id_str, group_id_str = query.data.removeprefix(SELECT_PREFIX).split(":")` line stays.

`group_creation_handler` (the `ConversationHandler` for user-driven group creation) is deleted entirely — no replacement conversation, since groups are now scanner-created only (spec, section 4.5).

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_commands.py tests/test_group_commands.py
git commit -m "feat: institute/group picker replaces manual group creation; validate-on-subscribe"
```

---

## Фаза 7 — Планировщик

### Task 14: `scheduler.py` — job'ы на уровне ссылки + `institute_catalog_refresh_*`

**Files:**
- Modify: `schedule_bot/scheduler.py` (полная замена)
- Test: `tests/test_scheduler.py` (полная замена)

**Interfaces:**
- Consumes: `storage.{list_institutes, list_institute_links, get_institute_link, get_institute, list_groups_for_link, get_active_version}` (Phase 1); `sync.sync_institute_link` (Task 12); `group_links.refresh_institute_catalog` (Task 7); `screenshot.render_day_image` (Task 11); `formatting.format_day_schedule`, `timeutil.today_in_configured_timezone` (unchanged).
- Produces: `link_check_job(conn, config, bot, institute_link_id, lock=None, catalog_refreshed_this_cycle=None)`; `link_daily_send_job(conn, config, bot, institute_link_id, week_formula_holder, lock=None)`; `institute_catalog_refresh_job(conn, config, bot, institute_id)`; `schedule_link_jobs(scheduler, conn, config, bot, institute_link, institute, week_formula_holder, lock)`; `schedule_institute_catalog_job(scheduler, conn, config, bot, institute)`; `reschedule_institute_link_jobs(scheduler, conn, config, bot, institute_link_id, week_formula_holder, lock)`; `reschedule_institute_jobs(scheduler, conn, config, bot, institute_id, week_formula_holder, locks)`; `build_scheduler(conn, config, bot, week_formula_holder, locks=None) -> AsyncIOScheduler`.

- [ ] **Step 1: Падающие тесты (замена `tests/test_scheduler.py`)**

```python
# tests/test_scheduler.py
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


def test_build_scheduler_registers_link_and_catalog_jobs(conn):
    institute_id = _make_institute(conn)
    link_id, _ = _make_link_and_group(conn, institute_id)
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, _config(), bot, holder)

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert f"link_check_{link_id}" in job_ids
    assert f"link_daily_send_{link_id}" in job_ids
    assert f"institute_catalog_refresh_{institute_id}" in job_ids
    assert "weekly_weektype_refresh" in job_ids
    scheduler.shutdown(wait=False)


def test_build_scheduler_binds_daily_trigger_to_institute_timezone(conn):
    institute_id = _make_institute(conn, tz_name="Asia/Yekaterinburg")
    link_id, _ = _make_link_and_group(conn, institute_id)
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, _config(), bot, holder)

    job = scheduler.get_job(f"link_daily_send_{link_id}")
    assert str(job.trigger.timezone) == "Asia/Yekaterinburg"
    scheduler.shutdown(wait=False)


def test_build_scheduler_gives_each_link_its_own_lock(conn):
    institute_id = _make_institute(conn)
    link_a, _ = _make_link_and_group(conn, institute_id, pdf_url="a.pdf", name="A")
    link_b, _ = _make_link_and_group(conn, institute_id, pdf_url="b.pdf", name="B")
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, _config(), bot, holder)

    lock_a = scheduler.get_job(f"link_check_{link_a}").args[-2]
    lock_b = scheduler.get_job(f"link_check_{link_b}").args[-2]
    assert lock_a is not lock_b
    scheduler.shutdown(wait=False)


def test_reschedule_institute_link_jobs_updates_trigger_time(conn):
    institute_id = _make_institute(conn)
    link_id, _ = _make_link_and_group(conn, institute_id)
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, _config(), bot, holder)

    storage.update_institute_send_time(conn, institute_id, "07:15")
    reschedule_institute_link_jobs(scheduler, conn, _config(), bot, link_id, holder, lock=None)

    job = scheduler.get_job(f"link_daily_send_{link_id}")
    field_names = [f.name for f in job.trigger.fields]
    assert job.trigger.fields[field_names.index("hour")].expressions[0].first == 7
    assert job.trigger.fields[field_names.index("minute")].expressions[0].first == 15
    scheduler.shutdown(wait=False)


def test_reschedule_institute_link_jobs_is_noop_for_deleted_link(conn):
    bot = MagicMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, _config(), bot, holder)
    reschedule_institute_link_jobs(scheduler, conn, _config(), bot, 9999, holder, lock=None)  # must not raise
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
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/scheduler.py — full replacement
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from schedule_bot import storage
from schedule_bot.bot import STALE_SCHEDULE_WARNING
from schedule_bot.formatting import format_day_schedule
from schedule_bot.group_links import refresh_institute_catalog
from schedule_bot.pdf_parser import DAY_ORDER, schedule_from_dict
from schedule_bot.screenshot import render_day_image
from schedule_bot.sync import sync_institute_link
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, send_with_retry
from schedule_bot.timeutil import today_in_configured_timezone
from schedule_bot.weektype import WeekFormula, fetch_week_formula, save_week_formula

logger = logging.getLogger(__name__)


@dataclass
class WeekFormulaHolder:
    formula: WeekFormula


async def _notify(bot, chat_id: int, text: str) -> None:
    await send_with_retry(lambda: bot.send_message(chat_id=chat_id, text=text))


async def link_check_job(
    conn, config, bot, institute_link_id: int, lock: asyncio.Lock | None = None,
    catalog_refreshed_this_cycle: set | None = None,
) -> None:
    institute_link = storage.get_institute_link(conn, institute_link_id)
    if institute_link is None:
        logger.warning("scheduler: link_check_job skipped, institute_link_id=%s no longer exists", institute_link_id)
        return
    institute = storage.get_institute(conn, institute_link.institute_id)
    if institute is None:
        return

    async def _do_sync():
        return await sync_institute_link(
            conn, institute_link, institute, config.pdf_cache_dir,
            lambda chat_id, text: _notify(bot, chat_id, text),
            catalog_refreshed_this_cycle=catalog_refreshed_this_cycle if catalog_refreshed_this_cycle is not None else set(),
        )

    logger.info("scheduler: link_check_job starting for link id=%s", institute_link_id)
    try:
        if lock is None:
            await _do_sync()
        else:
            async with lock:
                await _do_sync()
    except Exception as error:
        logger.warning("scheduler: link_check_job sync failed for link id=%s: %s", institute_link_id, error)
        try:
            await send_with_retry(
                lambda: bot.send_message(
                    chat_id=config.admin_chat_id,
                    text=f"Ежечасная проверка расписания по ссылке (id={institute_link_id}) не удалась: {error}",
                )
            )
        except Exception as notify_error:
            logger.warning("scheduler: link_check_job failed to notify admin: %s", notify_error)


async def link_daily_send_job(
    conn, config, bot, institute_link_id: int, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None,
) -> None:
    institute_link = storage.get_institute_link(conn, institute_link_id)
    if institute_link is None:
        logger.warning("scheduler: link_daily_send_job skipped, institute_link_id=%s no longer exists", institute_link_id)
        return
    institute = storage.get_institute(conn, institute_link.institute_id)
    if institute is None:
        return

    async def _do_sync():
        return await sync_institute_link(
            conn, institute_link, institute, config.pdf_cache_dir,
            lambda chat_id, text: _notify(bot, chat_id, text), catalog_refreshed_this_cycle=set(),
        )

    try:
        if lock is None:
            await _do_sync()
        else:
            async with lock:
                await _do_sync()
    except Exception as error:
        logger.warning("scheduler: link_daily_send_job sync failed for link id=%s: %s", institute_link_id, error)

    today = today_in_configured_timezone(institute.timezone)
    day_name = DAY_ORDER[today.weekday()] if today.weekday() < len(DAY_ORDER) else None
    if day_name is None:
        return  # Sunday: no classes

    week_type = week_formula_holder.formula.compute(today)
    render_error_notified = False

    for group in storage.list_groups_for_link(conn, institute_link_id):
        active = storage.get_active_version(conn, group.id)
        if active is None:
            continue
        schedule = schedule_from_dict(json.loads(active.payload_json))

        text = format_day_schedule(day_name, today, week_type, schedule)
        group = storage.get_group(conn, group.id)  # re-fetch: sync may have just set last_sync_failed_at
        if group is None:
            continue
        if group.last_sync_failed_at:
            text = STALE_SCHEDULE_WARNING + text

        image_bytes = None
        try:
            image_bytes = render_day_image(active.pdf_cache_path, schedule, day_name)
        except Exception as error:
            logger.warning("scheduler: failed to render day image for %r: %s", active.pdf_cache_path, error)
            if not render_error_notified:
                render_error_notified = True
                try:
                    await send_with_retry(
                        lambda: bot.send_message(
                            chat_id=config.admin_chat_id,
                            text=f"Сборка изображения для группы «{group.name}» ({day_name}) не удалась: {error}",
                        )
                    )
                except Exception as notify_error:
                    logger.warning("scheduler: failed to notify admin about render failure: %s", notify_error)

        members = storage.list_group_members(conn, group.id)
        for chat_id in members:
            try:
                await send_with_retry(lambda chat_id=chat_id: bot.send_message(chat_id=chat_id, text=text))
                if image_bytes is not None:
                    await send_with_retry(lambda chat_id=chat_id: bot.send_photo(chat_id=chat_id, photo=image_bytes))
            except Exception as error:
                logger.warning("scheduler: failed to send daily schedule to chat_id=%s: %s", chat_id, error)
            await asyncio.sleep(SEND_PACING_SECONDS)


async def institute_catalog_refresh_job(conn, config, bot, institute_id: int) -> None:
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        return

    async def notify_admin(text: str) -> None:
        try:
            await send_with_retry(lambda: bot.send_message(chat_id=config.admin_chat_id, text=text))
        except Exception as error:
            logger.warning("scheduler: institute_catalog_refresh_job failed to notify admin: %s", error)

    try:
        await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=lambda text: asyncio.run(notify_admin(text)))
    except Exception as error:
        logger.warning("scheduler: institute_catalog_refresh_job failed for institute %r: %s", institute.name, error)


async def weekly_weektype_refresh_job(conn, week_formula_holder: WeekFormulaHolder) -> None:
    logger.info("scheduler: weekly_weektype_refresh_job starting")
    url = storage.get_setting(conn, "weektype_url")
    if not url:
        return
    try:
        new_formula = await asyncio.to_thread(fetch_week_formula, url)
    except Exception as error:
        logger.warning("scheduler: weekly week-type refresh failed for %r (%s); keeping last known-good formula", url, error)
        return
    week_formula_holder.formula = new_formula
    save_week_formula(conn, new_formula)
    logger.info("scheduler: weekly_weektype_refresh_job updated formula from %r", url)


def _parse_hh_mm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":", 1)
    return int(hour_str), int(minute_str)


def _safe_hh_mm(value: str, default: str) -> tuple[int, int]:
    try:
        return _parse_hh_mm(value)
    except (ValueError, AttributeError) as error:
        logger.warning("scheduler: invalid send_time %r (%s); falling back to default %r", value, error, default)
        return _parse_hh_mm(default)


def _safe_interval_minutes(value: int | str, default: int) -> int:
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("interval must be positive")
        return parsed
    except (ValueError, TypeError) as error:
        logger.warning("scheduler: invalid interval %r (%s); falling back to default %r", value, error, default)
        return int(default)


def schedule_link_jobs(scheduler, conn, config, bot, institute_link, institute, week_formula_holder, lock) -> None:
    check_id = f"link_check_{institute_link.id}"
    daily_id = f"link_daily_send_{institute_link.id}"

    hour, minute = _safe_hh_mm(institute.send_time, config.default_daily_send_time)
    scheduler.add_job(
        link_daily_send_job,
        CronTrigger(hour=hour, minute=minute, timezone=institute.timezone),
        args=[conn, config, bot, institute_link.id, week_formula_holder, lock],
        id=daily_id,
        replace_existing=True,
    )

    interval_minutes = _safe_interval_minutes(institute.check_interval_minutes, config.default_check_interval_minutes)
    scheduler.add_job(
        link_check_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot, institute_link.id, lock, None],
        id=check_id,
        replace_existing=True,
    )


def schedule_institute_catalog_job(scheduler, conn, config, bot, institute) -> None:
    interval_minutes = _safe_interval_minutes(
        institute.catalog_refresh_interval_minutes, config.default_catalog_refresh_interval_minutes
    )
    scheduler.add_job(
        institute_catalog_refresh_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot, institute.id],
        id=f"institute_catalog_refresh_{institute.id}",
        replace_existing=True,
    )


def reschedule_institute_link_jobs(scheduler, conn, config, bot, institute_link_id: int, week_formula_holder, lock) -> None:
    institute_link = storage.get_institute_link(conn, institute_link_id)
    if institute_link is None:
        return
    institute = storage.get_institute(conn, institute_link.institute_id)
    if institute is None:
        return
    schedule_link_jobs(scheduler, conn, config, bot, institute_link, institute, week_formula_holder, lock)


def reschedule_institute_jobs(scheduler, conn, config, bot, institute_id: int, week_formula_holder, locks: dict) -> None:
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        return
    schedule_institute_catalog_job(scheduler, conn, config, bot, institute)
    for institute_link in storage.list_institute_links(conn, institute_id, include_stale=False):
        link_lock = locks.setdefault(institute_link.id, asyncio.Lock())
        schedule_link_jobs(scheduler, conn, config, bot, institute_link, institute, week_formula_holder, link_lock)


def build_scheduler(conn, config, bot, week_formula_holder: WeekFormulaHolder, locks: dict | None = None) -> AsyncIOScheduler:
    if locks is None:
        locks = {}

    scheduler = AsyncIOScheduler(timezone=config.default_timezone)

    for institute in storage.list_institutes(conn):
        schedule_institute_catalog_job(scheduler, conn, config, bot, institute)
        for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
            link_lock = locks.setdefault(institute_link.id, asyncio.Lock())
            schedule_link_jobs(scheduler, conn, config, bot, institute_link, institute, week_formula_holder, link_lock)

    scheduler.add_job(
        weekly_weektype_refresh_job,
        CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=config.default_timezone),
        args=[conn, week_formula_holder],
        id="weekly_weektype_refresh",
        replace_existing=True,
    )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    scheduler.start()
    return scheduler
```

`institute_catalog_refresh_job`'s `notify_admin` plumbing (sync `refresh_institute_catalog` called via `asyncio.to_thread`, wanting to call an async `notify_admin` from a worker thread) is awkward with `asyncio.run` inside a lambda — fix during implementation by instead collecting alert strings synchronously and sending them after `to_thread` returns:

```python
async def institute_catalog_refresh_job(conn, config, bot, institute_id: int) -> None:
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        return

    alerts: list[str] = []
    try:
        await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
    except Exception as error:
        logger.warning("scheduler: institute_catalog_refresh_job failed for institute %r: %s", institute.name, error)
        return

    for text in alerts:
        try:
            await send_with_retry(lambda text=text: bot.send_message(chat_id=config.admin_chat_id, text=text))
        except Exception as error:
            logger.warning("scheduler: institute_catalog_refresh_job failed to notify admin: %s", error)
```

(This replaces the earlier draft of `institute_catalog_refresh_job` above — use this version.)

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/scheduler.py tests/test_scheduler.py
git commit -m "feat: scheduler jobs run per institute_link, plus one catalog-refresh job per institute"
```

---

**Продолжение:** `docs/superpowers/plans/2026-10-09-institutes-and-shared-schedules-plan-part4.md` (Фаза 8 — admin-команды, меню, `bot.py`/`main.py` wiring; Фаза 9 — финальная чистка оставшихся тестов и self-review).
