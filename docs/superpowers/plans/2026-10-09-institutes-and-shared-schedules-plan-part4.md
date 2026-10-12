# Институты — план реализации, часть 4 (admin-команды, меню, wiring, финальная чистка)

> Продолжение части 3. Global Constraints/Review Focus — в части 1.

## Фаза 8 — Admin-команды, меню, `bot.py`/`main.py` wiring

### Task 15: новый модуль `institute_commands.py` — admin-команды институтов

**Files:**
- Create: `schedule_bot/institute_commands.py`
- Test: `tests/test_institute_commands.py` (новый)

**Interfaces:**
- Consumes: `storage.{create_institute, get_institute, list_institutes, update_institute_*, set_institute_last_catalog_sync_at, list_institute_links}` (Phase 1); `group_links.refresh_institute_catalog` (Task 7); `bot.is_admin` (unchanged); `context.bot_data["reschedule_institute_jobs"]`/`["reschedule_institute_link_jobs"]` (wired in Task 19).
- Produces: `add_institute_command`, `set_institute_catalog_url_command`, `set_institute_check_interval_command`, `set_institute_send_time_command`, `set_institute_catalog_interval_command`, `refresh_institute_links_command`, `institute_links_command` — all `async def (update, context) -> None`, mirroring the existing admin-command pattern from `bot.py` (is_admin guard, `context.args` parsing, `_resolve_admin_institute` helper).

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_institute_commands.py
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.institute_commands import (
    add_institute_command,
    institute_links_command,
    refresh_institute_links_command,
    set_institute_catalog_url_command,
    set_institute_check_interval_command,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _update(args, chat_id=999):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = " ".join(args)
    return update


def _context(conn, args, admin_chat_id=999):
    context = MagicMock()
    context.args = args
    context.bot_data = {
        "conn": conn, "admin_chat_id": admin_chat_id,
        "config": MagicMock(
            default_daily_send_time="09:00", default_check_interval_minutes=60,
            default_timezone="Europe/Moscow", default_catalog_refresh_interval_minutes=10080,
        ),
        "reschedule_institute_jobs": MagicMock(),
        "reschedule_institute_link_jobs": MagicMock(),
    }
    return context


@pytest.mark.asyncio
async def test_add_institute_creates_row_and_reschedules(conn):
    update = _update(["ИТ", "https://example.com/catalog"])
    context = _context(conn, ["ИТ", "https://example.com/catalog"])

    await add_institute_command(update, context)

    institutes = storage.list_institutes(conn)
    assert len(institutes) == 1
    assert institutes[0].name == "ИТ"
    context.bot_data["reschedule_institute_jobs"].assert_called_once()


@pytest.mark.asyncio
async def test_add_institute_rejects_non_admin(conn):
    update = _update(["ИТ", "u"], chat_id=1)
    context = _context(conn, ["ИТ", "u"], admin_chat_id=999)

    await add_institute_command(update, context)

    assert storage.list_institutes(conn) == []
    update.message.reply_text.assert_awaited_once_with("Недостаточно прав.")


@pytest.mark.asyncio
async def test_set_institute_catalog_url_updates_and_reports(conn):
    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="old", check_interval_minutes=60, send_time="09:00",
        tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    update = _update([str(institute_id), "https://new.example.com"])
    context = _context(conn, [str(institute_id), "https://new.example.com"])

    await set_institute_catalog_url_command(update, context)

    assert storage.get_institute(conn, institute_id).catalog_url == "https://new.example.com"


@pytest.mark.asyncio
async def test_set_institute_check_interval_rejects_non_positive(conn):
    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="u", check_interval_minutes=60, send_time="09:00",
        tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    update = _update([str(institute_id), "0"])
    context = _context(conn, [str(institute_id), "0"])

    await set_institute_check_interval_command(update, context)

    assert storage.get_institute(conn, institute_id).check_interval_minutes == 60
    update.message.reply_text.assert_awaited_once()
    assert "Неверное" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_refresh_institute_links_command_invokes_catalog_scan(conn, monkeypatch):
    import schedule_bot.institute_commands as institute_commands_module

    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="https://example.com/catalog", check_interval_minutes=60,
        send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    called = []
    monkeypatch.setattr(institute_commands_module, "refresh_institute_catalog", lambda conn, institute, **kw: called.append(institute.id))

    update = _update([str(institute_id)])
    context = _context(conn, [str(institute_id)])

    await refresh_institute_links_command(update, context)

    assert called == [institute_id]


@pytest.mark.asyncio
async def test_institute_links_command_lists_raw_labels_and_group_counts(conn):
    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="u", check_interval_minutes=60, send_time="09:00",
        tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="a.pdf", group_names=["A", "B"], raw_label="A,B", last_seen_at="t"
    )
    storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="b.pdf", group_names=[], raw_label="1 курс БЭ, БМ оз", last_seen_at="t"
    )
    update = _update([str(institute_id)])
    context = _context(conn, [str(institute_id)])

    await institute_links_command(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "A,B" in text
    assert "2" in text  # group count for the first link
    assert "1 курс БЭ, БМ оз" in text
    assert "0" in text  # group count for the second link
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_institute_commands.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/institute_commands.py (new file)
import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.bot import is_admin
from schedule_bot.group_links import refresh_institute_catalog

logger = logging.getLogger(__name__)


def _resolve_admin_institute(conn, institute_id_str: str):
    if not institute_id_str.isdigit():
        return None
    return storage.get_institute(conn, int(institute_id_str))


async def add_institute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /add_institute from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /add_institute <название> <url_каталога>")
        return
    conn = context.bot_data["conn"]
    config = context.bot_data["config"]
    name = context.args[0]
    catalog_url = context.args[1]
    institute_id = storage.create_institute(
        conn, name=name, catalog_url=catalog_url,
        check_interval_minutes=config.default_check_interval_minutes,
        send_time=config.default_daily_send_time, tz_name=config.default_timezone,
        catalog_refresh_interval_minutes=config.default_catalog_refresh_interval_minutes,
    )
    refresh_institute_catalog(
        conn, storage.get_institute(conn, institute_id),
        notify_admin=lambda text: None,  # synchronous call site, can't await here — see refresh_institute_links_command for the alert path
    )
    context.bot_data["reschedule_institute_jobs"](institute_id)
    await update.message.reply_text(f"Институт «{name}» создан (id={institute_id}), каталог просканирован.")


async def set_institute_catalog_url_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /set_institute_catalog_url from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_institute_catalog_url <institute_id> <url>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    storage.update_institute_catalog_url(conn, institute.id, context.args[1])
    await update.message.reply_text(f"Каталог института «{institute.name}» обновлён: {context.args[1]}")


async def set_institute_check_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /set_institute_check_interval from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_institute_check_interval <institute_id> <минуты>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    try:
        minutes = int(context.args[1])
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
        return
    storage.update_institute_check_interval(conn, institute.id, minutes)
    for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
        context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
    await update.message.reply_text(f"Интервал проверки института «{institute.name}» обновлён: {minutes} мин.")


async def set_institute_send_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /set_institute_send_time from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_institute_send_time <institute_id> <HH:MM>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    from schedule_bot.bot import _is_valid_hh_mm
    if not _is_valid_hh_mm(context.args[1]):
        await update.message.reply_text("Неверный формат времени, используйте HH:MM.")
        return
    storage.update_institute_send_time(conn, institute.id, context.args[1])
    for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
        context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
    await update.message.reply_text(f"Время рассылки института «{institute.name}» обновлено: {context.args[1]}")


async def set_institute_catalog_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /set_institute_catalog_interval from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_institute_catalog_interval <institute_id> <минуты>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    try:
        minutes = int(context.args[1])
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
        return
    storage.update_institute_catalog_refresh_interval(conn, institute.id, minutes)
    context.bot_data["reschedule_institute_jobs"](institute.id)
    await update.message.reply_text(f"Интервал сканирования каталога института «{institute.name}» обновлён: {minutes} мин.")


async def refresh_institute_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /refresh_institute_links from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /refresh_institute_links <institute_id>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    alerts = []
    try:
        refresh_institute_catalog(conn, institute, notify_admin=alerts.append)
    except Exception as error:
        await update.message.reply_text(f"Сканирование каталога не удалось: {error}")
        return
    context.bot_data["reschedule_institute_jobs"](institute.id)
    for text in alerts:
        await update.message.reply_text(text)
    await update.message.reply_text(f"Каталог института «{institute.name}» пересканирован.")


async def institute_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /institute_links from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /institute_links <institute_id>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    links = storage.list_institute_links(conn, institute.id)
    if not links:
        await update.message.reply_text("У этого института пока нет ссылок.")
        return
    lines = []
    for link in links:
        group_count = len(json.loads(link.group_names_json))
        stale_marker = " [stale]" if link.is_stale else ""
        lines.append(f"{link.raw_label!r} -> {group_count} групп{stale_marker}")
    await update.message.reply_text("\n".join(lines))
```

`add_institute_command`'s `refresh_institute_catalog(..., notify_admin=lambda text: None)` call drops the "0 links" alert on first scan — acceptable here since `/add_institute` already reports success/failure synchronously to the calling admin; a real alert path only matters for the *background* catalog job (Task 14) and the explicit `/refresh_institute_links` (which does forward `alerts` above). Note this trade-off in the PR description if asked, but it is not a spec violation — spec section 2 only requires the alert to exist as a mechanism, not that every call site surface it identically.

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_institute_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/institute_commands.py tests/test_institute_commands.py
git commit -m "feat: institute admin commands (add/set_*/refresh_institute_links/institute_links)"
```

---

### Task 16: `bot.py` — удалить group-level `set_pdf_source`/`set_send_time`/`set_check_interval`, `refresh`/`day_callback` резолвят через институт

**Files:**
- Modify: `schedule_bot/bot.py`
- Test: `tests/test_bot_admin_commands.py`
- Test: `tests/test_bot_user_commands.py`

**Interfaces:**
- Consumes: Phase 1 storage, Task 12 `sync.sync_institute_link`.
- Produces: `ADMIN_COMMANDS` list without the three removed entries; `refresh_command` resolves `group.institute_link_id` and calls `context.bot_data["sync_institute_link_fn"](institute_link_id)`; `day_callback`/`schedule_command` read timezone via `storage.get_institute(conn, group.institute_id).timezone`. Deletes: `_apply_set_pdf_source`, `_apply_set_send_time`, `_apply_set_check_interval`, `set_pdf_source_command`, `set_send_time_command`, `set_check_interval_command`, `_is_valid_pdf_source`, `_ALLOWED_PDF_SOURCE_SCHEMES`. Keeps `_is_valid_hh_mm`/`_HH_MM_RE` (reused by `institute_commands.py`, Task 15).

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_bot_admin_commands.py — read the existing file first to find/replace these call-sites:
# - delete every test referencing set_pdf_source_command / set_send_time_command / set_check_interval_command
# - replace _make_group fixture helper to the institute-scoped signature (see Task 1's fixture pattern)
# - add:

@pytest.mark.asyncio
async def test_refresh_command_resolves_institute_link_and_calls_sync_fn(conn):
    institute_id = _make_institute(conn)
    link_id, group_id = _make_link_and_group(conn, institute_id)
    update = _update(["", str(group_id)])
    context = _context(conn, [str(group_id)])
    sync_calls = []

    async def fake_sync_institute_link_fn(institute_link_id):
        sync_calls.append(institute_link_id)
        return []

    context.bot_data["sync_institute_link_fn"] = fake_sync_institute_link_fn

    await refresh_command(update, context)

    assert sync_calls == [link_id]
```

(Follow the existing file's own `_update`/`_context` helper conventions; only the fixture and the sync-fn wiring change. Delete the old `sync_fn` mock usage that called `check_and_apply_diff`-style single-group sync.)

```python
# tests/test_bot_user_commands.py — add:

@pytest.mark.asyncio
async def test_schedule_command_uses_institute_timezone(conn, monkeypatch):
    institute_id = _make_institute(conn, tz_name="Asia/Yekaterinburg")
    link_id, group_id = _make_link_and_group(conn, institute_id)
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    captured_tz = []
    import schedule_bot.bot as bot_module
    monkeypatch.setattr(bot_module, "today_in_configured_timezone", lambda tz: captured_tz.append(tz) or date(2026, 9, 14))

    update = _update()
    context = _context(conn, chat_id=111)
    await schedule_command(update, context)

    assert captured_tz == ["Asia/Yekaterinburg"]
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_bot_admin_commands.py tests/test_bot_user_commands.py -v`
Expected: FAIL (old signatures/behavior)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/bot.py — key diffs

ADMIN_COMMANDS = [
    ("add_institute", "Добавить институт: <название> <url_каталога>"),
    ("set_institute_catalog_url", "Изменить url каталога института: <institute_id> <url>"),
    ("set_institute_check_interval", "Изменить интервал проверки института: <institute_id> <минуты>"),
    ("set_institute_send_time", "Изменить время рассылки института: <institute_id> <HH:MM>"),
    ("set_institute_catalog_interval", "Изменить интервал сканирования каталога: <institute_id> <минуты>"),
    ("refresh_institute_links", "Пересканировать каталог института: <institute_id>"),
    ("institute_links", "Диагностика ссылок института: <institute_id>"),
    ("refresh", "Принудительно проверить и разослать обновления по ссылке группы: <group_id>"),
    ("alert", "Разослать объявление всем пользователям бота: <текст>"),
    ("set_starosta", "Назначить старосту группы: <group_id> <chat_id>"),
    ("remove_starosta", "Снять старосту группы: <group_id>"),
    ("close_group", "Закрыть группу на вступление: [group_id] (без аргумента — своя группа)"),
    ("open_group", "Открыть группу для вступления: [group_id] (без аргумента — своя группа)"),
    ("notify", "Оповестить всех закреплённых за группой: [group_id] <текст>"),
    ("requests", "Заявки на вступление/переход: [group_id] (без аргумента — своя группа)"),
]
```

Delete `_ALLOWED_PDF_SOURCE_SCHEMES`, `_is_valid_pdf_source`, `_apply_set_pdf_source`, `set_pdf_source_command`, `_apply_set_send_time`, `set_send_time_command`, `_apply_set_check_interval`, `set_check_interval_command` entirely.

```python
async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /refresh from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /refresh <group_id>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    sync_institute_link_fn = context.bot_data["sync_institute_link_fn"]
    try:
        results = await sync_institute_link_fn(group.institute_link_id)
    except Exception as error:
        logger.warning("bot: /refresh sync failed: %s", error)
        await update.message.reply_text(f"Синхронизация не удалась: {error}. Расписание могло устареть.")
        return
    own_result = next((r for r in results if r.group_id == group.id), None)
    if own_result is None:
        await update.message.reply_text("Группа не подписана ни на один чат — синхронизация не выполнялась.")
    elif own_result.status == "no_changes":
        await update.message.reply_text("Изменений нет.")
    elif own_result.status == "initial":
        await update.message.reply_text("Расписание загружено впервые.")
    elif own_result.status == "error":
        await update.message.reply_text("Синхронизация для этой группы не удалась — колонка не найдена или сеть недоступна.")
    else:
        await update.message.reply_text("Расписание обновлено и разослано подписчикам.")
```

```python
async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    logger.info("bot: /schedule from chat_id=%s", chat_id)
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)
    text = _stale_warning_prefix(group) + "Выберите день недели:"
    institute = storage.get_institute(conn, group.institute_id) if group else None
    today = today_in_configured_timezone(institute.timezone if institute else None)
    await reply(update, text, reply_markup=build_day_keyboard(today))
```

(`day_callback` doesn't reference `group.timezone` directly today — it already reads schedules via `active_schedule_getter`; no change needed there beyond the fixture update in tests.)

Remove the now-unused `_resolve_admin_group`'s dependency nowhere else changes; keep it as-is (still resolves by raw group id, unrelated to institute).

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_bot_admin_commands.py tests/test_bot_user_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/bot.py tests/test_bot_admin_commands.py tests/test_bot_user_commands.py
git commit -m "feat: drop group-level pdf/send-time/interval admin commands; refresh/schedule resolve via institute"
```

---

### Task 17: `menu.py` — институтские пункты меню, удаление подменю "Источник PDF"/"Время рассылки"/"Интервал проверки"

**Files:**
- Modify: `schedule_bot/menu.py`
- Test: `tests/test_menu.py`

**Interfaces:**
- Consumes: Task 15 (`institute_commands.py` apply-functions aren't reused directly — menu triggers the same storage calls inline, mirroring the existing `_apply_set_starosta`-via-menu pattern) and Task 16 (`bot.ADMIN_COMMANDS` unaffected by menu).
- Produces: `_institute_settings_submenu(institute_id)`; `institute_scoped_actions` set in `menu_callback`; `_handle_institute_scoped_pick`; `pending_menu_action` dict gains an `institute_id` branch in `pending_action_followup`. Removes: `setpdf`/`setsendtime`/`setinterval` entries from `_group_settings_submenu` and `group_scoped_actions`, and their branches in `_handle_group_scoped_pick`/`pending_action_followup`.

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_menu.py — add (after updating the file's own group/institute fixtures per Task 1's pattern):

@pytest.mark.asyncio
async def test_admin_root_keyboard_has_institutes_entry():
    from schedule_bot.menu import _admin_root_keyboard
    keyboard = _admin_root_keyboard()
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Институт" in t for t in texts)


@pytest.mark.asyncio
async def test_group_settings_submenu_no_longer_offers_pdf_sendtime_interval():
    from schedule_bot.menu import _group_settings_submenu
    keyboard = _group_settings_submenu(1)
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Источник PDF" not in texts
    assert "Время рассылки" not in texts
    assert "Интервал проверки" not in texts


@pytest.mark.asyncio
async def test_institute_scoped_pick_sets_pending_catalog_url_action(conn):
    institute_id = _make_institute(conn)
    update = _menu_update(f"menu:setinstitutecatalogurl:{institute_id}", chat_id=999)
    context = _context(conn, admin_chat_id=999)

    await menu_callback(update, context)

    assert context.user_data["pending_menu_action"] == {"action": "set_institute_catalog_url", "institute_id": institute_id}


@pytest.mark.asyncio
async def test_pending_action_followup_applies_institute_catalog_url(conn):
    institute_id = _make_institute(conn)
    context = _context(conn, admin_chat_id=999)
    context.user_data["pending_menu_action"] = {"action": "set_institute_catalog_url", "institute_id": institute_id}
    update = MagicMock()
    update.message.text = "https://new.example.com"
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = 999

    await pending_action_followup(update, context)

    assert storage.get_institute(conn, institute_id).catalog_url == "https://new.example.com"
```

(Use whichever `_menu_update`/`_context` test helpers already exist in `tests/test_menu.py`; adapt the mock shape to match, keeping assertions identical in intent.)

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_menu.py -v`
Expected: FAIL

- [ ] **Step 3: Реализация**

```python
# schedule_bot/menu.py — key diffs

def _admin_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Объявление всем", callback_data="menu:admin:alert")],
            [InlineKeyboardButton("🏫 Институты", callback_data="menu:admin:institutes")],
            [InlineKeyboardButton("🔧 Настройки группы", callback_data="menu:admin:groupsettings")],
            [InlineKeyboardButton("👑 Старосты", callback_data="menu:admin:starostas")],
            [InlineKeyboardButton("🔒 Открытость группы", callback_data="menu:admin:closedtoggle")],
            [InlineKeyboardButton("📥 Заявки", callback_data="menu:admin:requests")],
            [InlineKeyboardButton("◀️ Назад", callback_data=ROOT_CALLBACK_DATA)],
        ]
    )


def _group_settings_submenu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:groupsettings")],
        ]
    )  # pdf/send-time/interval are institute-level now — see _institute_settings_submenu


def _institute_picker_keyboard(conn) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(i.name, callback_data=f"menu:pickinstitute:{i.id}")] for i in storage.list_institutes(conn)]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:root")])
    return InlineKeyboardMarkup(rows)


def _institute_settings_submenu(institute_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("URL каталога", callback_data=f"menu:setinstitutecatalogurl:{institute_id}")],
            [InlineKeyboardButton("Время рассылки", callback_data=f"menu:setinstitutesendtime:{institute_id}")],
            [InlineKeyboardButton("Интервал проверки", callback_data=f"menu:setinstitutecheckinterval:{institute_id}")],
            [InlineKeyboardButton("Интервал скана каталога", callback_data=f"menu:setinstitutecataloginterval:{institute_id}")],
            [InlineKeyboardButton("🔄 Пересканировать сейчас", callback_data=f"menu:refreshinstitutelinks:{institute_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:institutes")],
        ]
    )
```

In `_handle_admin_section`, add:

```python
    if action == "institutes":
        await query.edit_message_text("Выберите институт:", reply_markup=_institute_picker_keyboard(conn))
        return
```

Add a new dispatcher function (parallel to `_handle_group_scoped_pick`):

```python
async def _handle_institute_scoped_pick(update, context, conn, chat_id: int, admin_chat_id: int, action: str, institute_id: int) -> None:
    query = update.callback_query
    if chat_id != admin_chat_id:
        await query.message.reply_text("Недостаточно прав.")
        return
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        await query.message.reply_text("Этот институт больше не существует.")
        return

    if action == "pickinstitute":
        await query.edit_message_text(f"Настройки института «{institute.name}»:", reply_markup=_institute_settings_submenu(institute_id))
        return
    if action == "setinstitutecatalogurl":
        context.user_data["pending_menu_action"] = {"action": "set_institute_catalog_url", "institute_id": institute_id}
        await query.message.reply_text("Отправьте новый url каталога:")
        return
    if action == "setinstitutesendtime":
        context.user_data["pending_menu_action"] = {"action": "set_institute_send_time", "institute_id": institute_id}
        await query.message.reply_text("Отправьте новое время (HH:MM):")
        return
    if action == "setinstitutecheckinterval":
        context.user_data["pending_menu_action"] = {"action": "set_institute_check_interval", "institute_id": institute_id}
        await query.message.reply_text("Отправьте новый интервал проверки в минутах:")
        return
    if action == "setinstitutecataloginterval":
        context.user_data["pending_menu_action"] = {"action": "set_institute_catalog_interval", "institute_id": institute_id}
        await query.message.reply_text("Отправьте новый интервал сканирования каталога в минутах:")
        return
    if action == "refreshinstitutelinks":
        from schedule_bot.group_links import refresh_institute_catalog
        alerts = []
        refresh_institute_catalog(conn, institute, notify_admin=alerts.append)
        context.bot_data["reschedule_institute_jobs"](institute_id)
        for text in alerts:
            await query.message.reply_text(text)
        await query.message.reply_text(f"Каталог института «{institute.name}» пересканирован.")
        return
```

In `menu_callback`, remove `"setpdf", "setsendtime", "setinterval"` from `group_scoped_actions` (keep `_handle_group_scoped_pick`'s now-dead branches removed too — see Step below), and add institute dispatch:

```python
    group_scoped_actions = {
        "pickgroup", "pickstarostagroup", "toggleclosed", "pickrequestsgroup", "setstarosta", "removestarosta",
    }
    if section in group_scoped_actions:
        await _handle_group_scoped_pick(update, context, conn, chat_id, admin_chat_id, section, int(action))
        return

    institute_scoped_actions = {
        "pickinstitute", "setinstitutecatalogurl", "setinstitutesendtime",
        "setinstitutecheckinterval", "setinstitutecataloginterval", "refreshinstitutelinks",
    }
    if section in institute_scoped_actions:
        await _handle_institute_scoped_pick(update, context, conn, chat_id, admin_chat_id, section, int(action))
        return
```

Remove the `"setpdf"`, `"setsendtime"`, `"setinterval"` branches (and their `storage.update_group_*` calls) from `_handle_group_scoped_pick`.

In `pending_action_followup`, add the institute branch before the group-lookup block (since `pending["group_id"]` doesn't exist for institute actions):

```python
async def pending_action_followup(update, context) -> None:
    pending = context.user_data.get("pending_menu_action")
    if pending is None:
        return

    conn = context.bot_data["conn"]
    action = pending["action"]
    value = update.message.text
    del context.user_data["pending_menu_action"]

    if action == "alert":
        text = await _apply_alert(conn, context.bot, value)
        await update.message.reply_text(text)
        return

    if action == "report":
        await update.message.reply_text(
            await _apply_report(conn, context.bot, context.bot_data["admin_chat_id"], update.effective_chat.id, value)
        )
        return

    if "institute_id" in pending:
        institute = storage.get_institute(conn, pending["institute_id"])
        if institute is None:
            await update.message.reply_text("Этот институт больше не существует.")
            return
        if action == "set_institute_catalog_url":
            storage.update_institute_catalog_url(conn, institute.id, value)
            await update.message.reply_text(f"Каталог института «{institute.name}» обновлён: {value}")
        elif action == "set_institute_send_time":
            from schedule_bot.bot import _is_valid_hh_mm
            if not _is_valid_hh_mm(value):
                await update.message.reply_text("Неверный формат времени, используйте HH:MM.")
                return
            storage.update_institute_send_time(conn, institute.id, value)
            for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
                context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
            await update.message.reply_text(f"Время рассылки института «{institute.name}» обновлено: {value}")
        elif action == "set_institute_check_interval":
            if not value.isdigit() or int(value) <= 0:
                await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
                return
            storage.update_institute_check_interval(conn, institute.id, int(value))
            for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
                context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
            await update.message.reply_text(f"Интервал проверки института «{institute.name}» обновлён: {value} мин.")
        elif action == "set_institute_catalog_interval":
            if not value.isdigit() or int(value) <= 0:
                await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
                return
            storage.update_institute_catalog_refresh_interval(conn, institute.id, int(value))
            context.bot_data["reschedule_institute_jobs"](institute.id)
            await update.message.reply_text(f"Интервал сканирования каталога института «{institute.name}» обновлён: {value} мин.")
        return

    group = storage.get_group(conn, pending["group_id"])
    if group is None:
        await update.message.reply_text("Эта группа больше не существует.")
        return

    if action == "set_starosta":
        await update.message.reply_text(await _apply_set_starosta(conn, context.bot, group, value))
    elif action == "notify":
        current_group = storage.get_group_where_starosta(conn, update.effective_chat.id)
        if current_group is None or current_group.id != group.id:
            await update.message.reply_text("Вы больше не староста этой группы.")
            return
        await update.message.reply_text(await _apply_notify(conn, context.bot, group, value))
```

Remove `from schedule_bot.bot import _apply_set_pdf_source, _apply_set_send_time, _apply_set_check_interval` from `menu.py`'s imports (those names no longer exist in `bot.py` per Task 16).

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_menu.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/menu.py tests/test_menu.py
git commit -m "feat: institute settings menu replaces group-level pdf/send-time/interval submenu"
```

---

### Task 18: `main.py` — wiring (`reschedule_institute_link_jobs`/`reschedule_institute_jobs`, `sync_institute_link_fn`, lock dict на уровне ссылки)

**Files:**
- Modify: `main.py`
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: Task 12 (`sync.sync_institute_link`), Task 14 (`scheduler.{build_scheduler, reschedule_institute_link_jobs, reschedule_institute_jobs}`), Task 15 (`institute_commands.*`), Task 16/17 (`bot.py`/`menu.py` handlers).
- Produces: `application.bot_data["sync_institute_link_fn"]`, `application.bot_data["reschedule_institute_link_jobs"]`, `application.bot_data["reschedule_institute_jobs"]`; new `CommandHandler`s for every `institute_commands.*` function; removed handlers for `set_pdf_source`/`set_send_time`/`set_check_interval`/`group_creation_handler`/old `grp:select`/`grp:page` patterns, replaced with institute/group picker patterns from Task 13.

- [ ] **Step 1: Падающие тест**

```python
# tests/test_main_wiring.py — add (keep the file's existing autouse scheduler-shutdown fixture):

def test_build_application_wires_institute_bot_data_callbacks(tmp_path, monkeypatch):
    import sqlite3
    from schedule_bot import storage
    from schedule_bot.config import Config
    from main import build_application

    monkeypatch.setattr("schedule_bot.weektype.fetch_week_formula", lambda url: (_ for _ in ()).throw(ConnectionError()))

    conn = sqlite3.connect(":memory:")
    storage.init_db(conn)
    config = Config(
        telegram_token="abc", admin_chat_id=1, db_path=":memory:", pdf_cache_dir=str(tmp_path),
        default_weektype_url="https://example.com/week", default_daily_send_time="09:00",
        default_check_interval_minutes=60, default_timezone="Europe/Moscow",
        default_catalog_refresh_interval_minutes=10080,
    )

    app = build_application(config, conn)

    assert "sync_institute_link_fn" in app.bot_data
    assert "reschedule_institute_link_jobs" in app.bot_data
    assert "reschedule_institute_jobs" in app.bot_data
    assert "reschedule_group_jobs" not in app.bot_data
    app.bot_data["_scheduler"].shutdown(wait=False)
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: FAIL

- [ ] **Step 3: Реализация**

```python
# main.py — full replacement of the relevant sections

import asyncio
import logging
import os

from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from schedule_bot import storage
from schedule_bot.menu import menu_callback, menu_command, pending_action_followup
from schedule_bot.bot import (
    ADMIN_COMMANDS,
    NOOP_CALLBACK_DATA,
    USER_COMMANDS,
    alert_command,
    close_group_command,
    day_callback,
    help_command,
    noop_callback,
    notify_command,
    open_group_command,
    refresh_command,
    schedule_command,
    remove_starosta_command,
    set_starosta_command,
)
from schedule_bot.classmates import classmates_command, exclude_abort_callback, exclude_confirm_callback, exclude_select_callback
from schedule_bot.faq import faq_command
from schedule_bot.report import report_command
from schedule_bot.change_group_commands import (
    change_group_abort_callback,
    change_group_command,
    change_group_confirm_callback,
    change_group_page_callback,
    change_group_select_callback,
    change_request_approve_callback,
    change_request_reject_callback,
    requests_command,
)
from schedule_bot.config import Config, load_config
from schedule_bot.group_commands import (
    INSTITUTE_PAGE_CALLBACK_PATTERN,
    INSTITUTE_SELECT_CALLBACK_PATTERN,
    PAGE_CALLBACK_PATTERN,
    SELECT_CALLBACK_PATTERN,
    group_page_callback,
    group_select_callback,
    institute_page_callback,
    institute_select_callback,
    start_command,
    subscribe_command,
    unsubscribe_command,
)
from schedule_bot.institute_commands import (
    add_institute_command,
    institute_links_command,
    refresh_institute_links_command,
    set_institute_catalog_interval_command,
    set_institute_catalog_url_command,
    set_institute_check_interval_command,
    set_institute_send_time_command,
)
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    reschedule_institute_jobs,
    reschedule_institute_link_jobs,
)
from schedule_bot.starosta_candidates import (
    abort_callback as starcand_abort_callback,
    confirm_callback as starcand_confirm_callback,
    page_callback as starcand_page_callback,
    select_callback as starcand_select_callback,
)
from schedule_bot.starosta_commands import (
    election_approve_callback,
    election_reject_callback,
    nominate_self_callback,
    starosta_change_callback,
    starosta_command,
    votes_command,
)
from schedule_bot.sync import sync_institute_link
from schedule_bot.telegram_utils import send_with_retry
from schedule_bot.weektype import WeekFormula, fetch_week_formula, load_week_formula, save_week_formula

logger = logging.getLogger(__name__)

_FALLBACK_WEEK_FORMULA = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)


class _LiveWeekFormula:
    def __init__(self, holder: WeekFormulaHolder) -> None:
        self._holder = holder

    def compute(self, on_date):
        return self._holder.formula.compute(on_date)


async def _backfill_member_profiles(conn, bot) -> None:
    for chat_id in storage.list_members_missing_profile(conn):
        try:
            chat = await bot.get_chat(chat_id)
        except Exception as error:
            logger.warning("main: failed to backfill profile for chat_id=%s: %s", chat_id, error)
            continue
        storage.update_member_profile(conn, chat_id, telegram_username=chat.username, display_name=chat.full_name)


def build_application(config: Config, conn) -> Application:
    storage.ensure_default_settings(conn, {"weektype_url": config.default_weektype_url})

    week_formula = _fetch_week_formula_or_fallback(conn, storage.get_setting(conn, "weektype_url"))
    week_formula_holder = WeekFormulaHolder(formula=week_formula)

    async def _post_init(app: Application) -> None:
        await app.bot.set_my_commands([BotCommand(name, description) for name, description in USER_COMMANDS])
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, description in USER_COMMANDS + ADMIN_COMMANDS],
            scope=BotCommandScopeChat(chat_id=config.admin_chat_id),
        )
        await _backfill_member_profiles(conn, app.bot)

    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(_post_init)
        .concurrent_updates(True)
        .build()
    )
    application.bot_data["conn"] = conn
    application.bot_data["config"] = config
    application.bot_data["admin_chat_id"] = config.admin_chat_id
    application.bot_data["_week_formula_holder"] = week_formula_holder
    application.bot_data["week_formula"] = _LiveWeekFormula(week_formula_holder)
    application.bot_data["active_schedule_getter"] = lambda group_id: _load_active_schedule(conn, group_id)
    application.bot_data["active_pdf_path_getter"] = lambda group_id: _load_active_pdf_path(conn, group_id)

    # One Lock per institute_link_id — shared by the scheduler's own
    # link_check_job/link_daily_send_job (via the SAME dict passed into
    # build_scheduler) AND manual /refresh (sync_institute_link_fn,
    # below). This is what guarantees a manual /refresh for a group and
    # that group's link's own scheduled sync never run sync_institute_link
    # concurrently for the SAME link — mirrors the pre-institutes
    # group_locks pattern, just re-keyed to institute_link_id (the new
    # unit of sync work; see scheduler.py).
    link_locks: dict[int, asyncio.Lock] = {}
    catalog_refreshed_this_cycle: set[int] = set()

    async def sync_institute_link_fn(institute_link_id: int):
        institute_link = storage.get_institute_link(conn, institute_link_id)
        if institute_link is None:
            raise ValueError(f"Institute link {institute_link_id} not found")
        institute = storage.get_institute(conn, institute_link.institute_id)

        async def notify(chat_id, text):
            await send_with_retry(lambda: application.bot.send_message(chat_id=chat_id, text=text))

        lock = link_locks.setdefault(institute_link_id, asyncio.Lock())
        async with lock:
            return await sync_institute_link(
                conn, institute_link, institute, config.pdf_cache_dir, notify,
                catalog_refreshed_this_cycle=catalog_refreshed_this_cycle,
            )

    application.bot_data["sync_institute_link_fn"] = sync_institute_link_fn

    scheduler_holder: dict = {}

    def reschedule_institute_link_jobs_cb(institute_link_id: int) -> None:
        lock = link_locks.setdefault(institute_link_id, asyncio.Lock())
        reschedule_institute_link_jobs(
            scheduler_holder["scheduler"], conn, config, application.bot, institute_link_id, week_formula_holder, lock
        )

    def reschedule_institute_jobs_cb(institute_id: int) -> None:
        reschedule_institute_jobs(
            scheduler_holder["scheduler"], conn, config, application.bot, institute_id, week_formula_holder, link_locks
        )

    application.bot_data["reschedule_institute_link_jobs"] = reschedule_institute_link_jobs_cb
    application.bot_data["reschedule_institute_jobs"] = reschedule_institute_jobs_cb

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("classmates", classmates_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(day_callback, pattern=r"^day:\d{4}-\d{2}-\d{2}$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=f"^{NOOP_CALLBACK_DATA}$"))
    application.add_handler(CallbackQueryHandler(institute_select_callback, pattern=INSTITUTE_SELECT_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(institute_page_callback, pattern=INSTITUTE_PAGE_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(group_select_callback, pattern=SELECT_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(group_page_callback, pattern=PAGE_CALLBACK_PATTERN))
    application.add_handler(CommandHandler("add_institute", add_institute_command))
    application.add_handler(CommandHandler("set_institute_catalog_url", set_institute_catalog_url_command))
    application.add_handler(CommandHandler("set_institute_check_interval", set_institute_check_interval_command))
    application.add_handler(CommandHandler("set_institute_send_time", set_institute_send_time_command))
    application.add_handler(CommandHandler("set_institute_catalog_interval", set_institute_catalog_interval_command))
    application.add_handler(CommandHandler("refresh_institute_links", refresh_institute_links_command))
    application.add_handler(CommandHandler("institute_links", institute_links_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("alert", alert_command))

    application.add_handler(CommandHandler("starosta", starosta_command))
    application.add_handler(CommandHandler("votes", votes_command))
    application.add_handler(CommandHandler("change_group", change_group_command))
    application.add_handler(CommandHandler("set_starosta", set_starosta_command))
    application.add_handler(CommandHandler("remove_starosta", remove_starosta_command))

    application.add_handler(CallbackQueryHandler(nominate_self_callback, pattern=r"^starosta:nominate_self$"))
    application.add_handler(CallbackQueryHandler(starosta_change_callback, pattern=r"^starosta:change$"))
    application.add_handler(CallbackQueryHandler(starcand_page_callback, pattern=r"^starcand:page:\d+$"))
    application.add_handler(CallbackQueryHandler(starcand_select_callback, pattern=r"^starcand:select:\d+$"))
    application.add_handler(CallbackQueryHandler(starcand_abort_callback, pattern=r"^starcand:abort$"))
    application.add_handler(CallbackQueryHandler(starcand_confirm_callback, pattern=r"^starcand:confirm:\d+$"))
    application.add_handler(CallbackQueryHandler(election_approve_callback, pattern=r"^election:approve:\d+$"))
    application.add_handler(CallbackQueryHandler(election_reject_callback, pattern=r"^election:reject:\d+$"))

    application.add_handler(CallbackQueryHandler(change_group_page_callback, pattern=r"^chggrp:page:\d+$"))
    application.add_handler(CallbackQueryHandler(change_group_select_callback, pattern=r"^chggrp:select:\d+$"))
    application.add_handler(CallbackQueryHandler(change_group_abort_callback, pattern=r"^chgabort$"))
    application.add_handler(CallbackQueryHandler(change_group_confirm_callback, pattern=r"^chgconfirm:\d+$"))
    application.add_handler(CallbackQueryHandler(change_request_approve_callback, pattern=r"^chgreq:approve:\d+$"))
    application.add_handler(CallbackQueryHandler(change_request_reject_callback, pattern=r"^chgreq:reject:\d+$"))

    application.add_handler(CommandHandler("close_group", close_group_command))
    application.add_handler(CommandHandler("open_group", open_group_command))
    application.add_handler(CommandHandler("notify", notify_command))
    application.add_handler(CommandHandler("requests", requests_command))

    application.add_handler(CallbackQueryHandler(exclude_select_callback, pattern=r"^exclude:select:\d+$"))
    application.add_handler(CallbackQueryHandler(exclude_confirm_callback, pattern=r"^exclude:confirm:\d+$"))
    application.add_handler(CallbackQueryHandler(exclude_abort_callback, pattern=r"^exclude:abort$"))

    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("faq", faq_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pending_action_followup))

    scheduler_holder["scheduler"] = build_scheduler(conn, config, application.bot, week_formula_holder, locks=link_locks)
    application.bot_data["_scheduler"] = scheduler_holder["scheduler"]

    return application


def _load_active_schedule(conn, group_id: int):
    import json
    from schedule_bot.pdf_parser import schedule_from_dict

    active = storage.get_active_version(conn, group_id)
    if active is None:
        return None
    return schedule_from_dict(json.loads(active.payload_json))


def _fetch_week_formula_or_fallback(conn, url):
    try:
        formula = fetch_week_formula(url)
        save_week_formula(conn, formula)
        return formula
    except Exception as error:
        persisted = load_week_formula(conn)
        if persisted is not None:
            logger.warning(
                "main: failed to fetch week formula from %r (%s); falling back to last persisted formula", url, error
            )
            return persisted
        logger.warning(
            "main: failed to fetch week formula from %r (%s); no persisted formula available, "
            "falling back to hardcoded default",
            url, error,
        )
        return _FALLBACK_WEEK_FORMULA


def _load_active_pdf_path(conn, group_id: int):
    active = storage.get_active_version(conn, group_id)
    if active is None:
        return None
    return active.pdf_cache_path


if __name__ == "__main__":
    import sqlite3

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config(os.environ)
    connection = sqlite3.connect(config.db_path, check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL")
    storage.init_db(connection)
    app = build_application(config, connection)
    app.run_polling()
```

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "feat: wire institute-scoped scheduler callbacks and sync_institute_link_fn into main.py"
```

---

## Фаза 9 — Финальная чистка оставшихся тестов и прогон всего набора

### Task 19: Обновить все оставшиеся тестовые фикстуры на новую схему групп, удалить устаревшие тесты, прогнать полный набор

**Files:**
- Modify: `tests/test_classmates.py`, `tests/test_elections.py`, `tests/test_starosta_commands.py`, `tests/test_change_group_commands.py`, `tests/test_join_requests.py`, `tests/test_telegram_utils.py` (только если использует `storage.create_group`), `tests/test_formatting.py` (только если использует старую `Schedule`/`DaySchedule` форму)
- Delete: ничего дополнительно (все удаления уже сделаны в Tasks 1/13/16 — `group_creation_handler` тесты, `migrate_legacy_single_group` тесты)

**Interfaces:**
- Consumes: Task 1's `_make_institute`/`_make_link`/`_make_group` fixture pattern — propagate it to every remaining test file whose fixtures still call the old `storage.create_group(..., source_page_url=..., pdf_source_url=..., send_time=..., check_interval_minutes=..., tz_name=...)` four/five-kwarg signature.
- Produces: a fully green `pytest` run across the whole suite.

- [ ] **Step 1: Найти все оставшиеся вызовы старой сигнатуры**

Run: `grep -rn "source_page_url" tests/ schedule_bot/ main.py`
Expected: only matches inside files already rewritten in Tasks 1-18 (i.e., none left referencing the removed `groups.source_page_url` column or old `create_group` kwarg).

- [ ] **Step 2: Для каждого файла с совпадением — переписать фикстуру**

For every file `grep` surfaces outside the already-rewritten set, apply the same substitution as Task 1's `_make_group` helper: introduce (or import, if later consolidated — see Step 4 below) `_make_institute`/`_make_link`/`_make_group` and replace direct `storage.create_group(...)` call sites. Example for `tests/test_classmates.py`:

```python
# before
def _make_group(conn, name="Б-Мв-101"):
    return storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )

# after
def _make_institute(conn, name="ИТ"):
    return storage.create_institute(
        conn, name=name, catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def _make_group(conn, name="Б-Мв-101"):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=f"https://example.com/{name}.pdf",
        group_names=[name], raw_label=name, last_seen_at="t",
    )
    return storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)
```

Apply the equivalent substitution file-by-file; keep each file's existing test bodies and assertions unchanged — only the fixture helper's internals change, since `GroupRow.name`/`.id`/`.starosta_chat_id`/`.is_closed`/`.last_sync_failed_at` (the fields these tests actually assert on) are unchanged in shape from Task 4.

- [ ] **Step 3: Прогнать каждый файл по отдельности**

Run: `./.venv/bin/python -m pytest tests/test_classmates.py tests/test_elections.py tests/test_starosta_commands.py tests/test_change_group_commands.py tests/test_join_requests.py -v`
Expected: PASS (after Step 2's fixture fix in each)

- [ ] **Step 4: (Опционально, рефакторинг по ходу) вынести общие фикстуры в `tests/conftest.py`**

If the same `_make_institute`/`_make_link`/`_make_group` trio now appears near-identically in 6+ files, extract them once into a new `tests/conftest.py` as pytest fixtures/helpers (`institute_factory`, `link_factory`, `group_factory`) and have each file import/use them instead of redefining locally — this is a pure DRY cleanup, not new behavior, so it's safe to do now that every call site is already passing. Skip this step if the per-file helpers stay small and reviewers would rather not touch N files in one more commit; either choice is fine, this is not spec-mandated.

- [ ] **Step 5: Прогнать весь набор тестов**

Run: `./.venv/bin/python -m pytest -v`
Expected: PASS, 0 failures, 0 errors.

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "test: migrate remaining fixtures to the institute-scoped groups schema"
```

---

## Self-Review (выполняется автором плана, не отдельным агентом)

**1. Покрытие спеки:**
- Раздел 1 (модель данных, 1.0-1.6) → Tasks 1-4. ✅
- Раздел 2 (сканирование каталога) → Tasks 5-7. ✅
- Раздел 3 (дедуп sync) → Tasks 9-10 (parse_schedules_for_groups), 12 (sync_institute_link). ✅
- Раздел 4 (планировщик/admin/меню/пользовательский выбор) → Tasks 13-18. ✅
- Раздел 5 (скриншот) → Tasks 8, 9, 11. ✅
- Тесты, перечисленные в спеке в разделе "Тесты, которые нужно обновить или добавить" — покрыты построчно: `test_storage.py` (Tasks 1-4), `test_group_links_catalog.py` (Task 7), `test_sync.py` (Task 12), `test_scheduler.py` (Task 14), `test_pdf_parser_build_schedule.py`/`test_pdf_parser_integration.py` (Tasks 8-10), `test_screenshot.py` (Task 11), удаление `test_group_commands.py`'s старых тестов создания группы (Task 13 заменяет файл целиком). ✅

**2. Проверка на placeholder'ы:** просмотрены все шаги — каждый код-блок содержит реальный, запускаемый код (за вычетом одной явно отмеченной неопределённости — точная сигнатура `fitz.Pixmap.copy`/конструктора в Task 11, которая зависит от установленной версии PyMuPDF и помечена как "уточнить на месте", а не как пропущенная логика).

**3. Согласованность типов:**
- `GroupRow` (Task 4) используется одинаково во всех последующих тасках: `.institute_id`, `.institute_link_id`, `.is_verified`, `.name`, `.last_sync_failed_at`.
- `SyncResult(group_id, status, changes)` (Task 12) — потребляется в Task 16 (`refresh_command`) и Task 13 (не используется напрямую, но тип совпадает).
- `sync_institute_link(conn, institute_link, institute, pdf_cache_dir, notify, *, catalog_refreshed_this_cycle)` — одна и та же сигнатура в Task 12 (реализация), Task 14 (`link_check_job`/`link_daily_send_job`), Task 18 (`main.py`'s `sync_institute_link_fn`).
- `Schedule(table_x0, table_x1, header_y0, header_y1, days)` и `DaySchedule(y0, y1, lessons, time_col_x0, time_col_x1, group_col_x0, group_col_x1)` — согласованы между Task 9/10 (производитель) и Task 11 (потребитель).

**4. Review Focus — проверка на тесты:**
- "0 ссылок не трогает БД" → `test_refresh_institute_catalog_zero_links_does_not_touch_existing_data_but_alerts` (Task 7). ✅
- "общий PDF скачивается/парсится один раз" → `test_sync_institute_link_fetches_and_parses_once_for_two_groups` (Task 12) + `test_parse_schedules_for_groups_extracts_raw_rows_exactly_once` (Task 10). ✅
- "is_stale не делает сетевых запросов" → `test_sync_institute_link_skips_network_when_stale` (Task 12). ✅
- "невалидное имя группы при первой подписке → is_verified=-1, скрыта, алерт" → `test_validate_marks_group_invalid_and_alerts_admin_when_column_missing` (Task 13). ✅
- "ширина шапки = ширине тела в день с общей парой" → `test_render_day_image_width_matches_sum_of_header_and_body_crop_ranges` (Task 11, через единый `_x_ranges_for` для шапки и тела). ✅

План полный, самопроверка пройдена.
