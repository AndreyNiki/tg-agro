# Pre-Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every CRITICAL and IMPORTANT finding from the pre-production review (concurrency/deadlocks, data-loss bugs, PDF-parsing correctness, and bot-handler crash scenarios) so the bot can safely handle thousands of concurrent Telegram users.

**Architecture:** No new subsystems — this is a hardening pass on the existing institute/link/group/member model described in `CLAUDE.md`. Fixes fall into six themes, done roughly in dependency order: (1) platform-level safety nets (error handler, edited-message guard, safe inline-keyboard edits) that make every later fix observable instead of silently swallowed; (2) scheduler robustness (misfire grace, blocking render moved off the event loop); (3) catalog-scan data integrity (per-institute lock, two-phase-upsert fix); (4) PDF-parser correctness (day-label drift); (5) storage/migration data-loss fixes; (6) handler-level race conditions (election double-apply, `None`-member crashes, admin input parsing).

**Tech Stack:** Python 3.12, python-telegram-bot 21.6, APScheduler 3.10.4, sqlite3, pytest + pytest-asyncio (existing test suite, run via `./.venv/bin/python -m pytest`).

**Spec:** This plan has no separate spec doc — the "spec" is the pre-production code review conducted in this conversation (three parallel Opus reviews covering concurrency/sync, storage/pdf_parser, and bot handlers/menu) plus `CLAUDE.md`'s documented invariants, which every task below either restores or hardens.

## Global Constraints

- Every new/changed background loop over multiple recipients/groups/links must isolate each iteration in `try/except Exception: logger.exception(...); continue` — this is an existing CLAUDE.md requirement this plan extends, never relaxes.
- Every blocking call (network fetch, PDF parse, PyMuPDF render) added or touched by this plan must run via `asyncio.to_thread`, never directly on the event loop.
- No new tests may leak an `AsyncIOScheduler` — follow the existing `shutdown_scheduler` fixture pattern from `tests/test_scheduler.py`.
- Keep `sqlite3.connect(":memory:", check_same_thread=False)` + `storage.init_db(connection)` as the standard test fixture, matching `tests/test_sync.py`/`tests/test_scheduler.py`.
- Do not change any public function's parameter order for functions already covered by existing passing tests unless this plan's task explicitly says so — grep the test suite before renaming/reordering.
- Run the full suite (`./.venv/bin/python -m pytest -v`) at the end of every task; a task is not done if it broke an unrelated test.

## Review Focus

- A Telegram `BadRequest` ("message is not modified") from a double-tapped navigation button must not crash the handler for the whole chat — Task 3 adds a shared safe-edit helper and Task 2's error handler is the backstop if any call site is missed.
- An edited (not newly sent) `/alert`, `/notify`, or `/report` command must not re-trigger the handler with `update.message is None`, nor silently re-run a side-effecting broadcast — Task 2.
- Two concurrent catalog rescans of the same institute (manual button double-tap, or a button tap racing the sync-fallback rescan) must not corrupt `institute_links`/`groups` rows or wrongly mark live links stale — Task 7.
- A legacy-schema database upgrade must not silently drop every subscriber with no way to recover them — Task 14.
- A PDF whose header has an extra sub-header row before the first real lesson row must not silently shift every day's data by one and drop Saturday — Task 13.

---

### Task 1: Global Application error handler

**Files:**
- Modify: `main.py:120-162` (`build_application`)
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Produces: `main._global_error_handler(update, context)` — an `async def` matching PTB's `Application.add_error_handler` signature `(object, ContextTypes.DEFAULT_TYPE) -> None`. No other task depends on its name, but Task 2's edited-message guard relies on the fact that *some* error handler is now registered so a leaked exception doesn't vanish silently.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_wiring.py — add near the other build_application tests
@pytest.mark.asyncio
async def test_global_error_handler_notifies_admin_and_logs(monkeypatch, caplog):
    import logging
    from unittest.mock import AsyncMock
    from telegram.ext import ContextTypes

    from main import build_application
    from schedule_bot.config import Config

    config = Config(
        telegram_token="123:fake", admin_chat_id=999, db_path=":memory:",
        pdf_cache_dir="/tmp/cache", default_weektype_url="https://example.com",
        default_daily_send_time="09:00", default_check_interval_minutes=60,
        default_timezone="Europe/Moscow", default_catalog_refresh_interval_minutes=10080,
    )
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    storage.init_db(connection)
    app = build_application(config, connection)
    try:
        app.bot.send_message = AsyncMock()
        context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        context.bot = app.bot
        context.error = ValueError("boom")
        update = MagicMock()
        update.effective_chat.id = 555

        error_handler = app.error_handlers[0] if hasattr(app, "error_handlers") else None
        # PTB stores handlers in app._error_handlers; call through the public API instead:
        handlers = list(app.error_handlers) if hasattr(app, "error_handlers") else []
        assert handlers, "no error handler registered on Application"

        with caplog.at_level(logging.ERROR):
            await handlers[0](update, context)

        app.bot.send_message.assert_awaited()
        _, kwargs = app.bot.send_message.call_args
        assert kwargs["chat_id"] == 999
        assert "boom" in kwargs["text"] or "ValueError" in kwargs["text"]
    finally:
        app.bot_data["_scheduler"].shutdown(wait=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py::test_global_error_handler_notifies_admin_and_logs -v`
Expected: FAIL — `assert handlers` fails because no error handler is registered yet.

- [ ] **Step 3: Write minimal implementation**

In `main.py`, add the handler function at module scope (near `_backfill_member_profiles`) and register it in `build_application`:

```python
async def _global_error_handler(update, context) -> None:
    logger.exception("main: unhandled exception while processing update %r", update, exc_info=context.error)
    config = context.application.bot_data.get("config")
    if config is None:
        return
    chat_id = update.effective_chat.id if getattr(update, "effective_chat", None) else "?"
    try:
        await send_with_retry(
            lambda: context.application.bot.send_message(
                chat_id=config.admin_chat_id,
                text=f"Необработанная ошибка при обработке апдейта (chat_id={chat_id}): {context.error}",
            )
        )
    except Exception:
        logger.exception("main: failed to notify admin about unhandled exception")

    effective_message = getattr(update, "effective_message", None)
    if effective_message is not None:
        try:
            await effective_message.reply_text("Что-то сломалось при обработке запроса, попробуйте позже.")
        except Exception:
            logger.exception("main: failed to notify user about unhandled exception")
```

Register it right after `application = Application.builder()...build()`:

```python
    application.add_error_handler(_global_error_handler)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py::test_global_error_handler_notifies_admin_and_logs -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "fix: register a global Application error handler so unhandled exceptions notify the admin instead of vanishing"
```

---

### Task 2: Guard every CommandHandler/MessageHandler against edited messages

**Files:**
- Modify: `main.py:234-296` (every `CommandHandler(...)` and the trailing `MessageHandler(...)`)
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new — this only adds a `filters=` keyword to existing registrations, no signature changes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_wiring.py
def test_command_handlers_ignore_edited_messages():
    import sqlite3
    from telegram.ext import CommandHandler, MessageHandler
    from schedule_bot import storage
    from schedule_bot.config import Config
    from main import build_application

    config = Config(
        telegram_token="123:fake", admin_chat_id=999, db_path=":memory:",
        pdf_cache_dir="/tmp/cache", default_weektype_url="https://example.com",
        default_daily_send_time="09:00", default_check_interval_minutes=60,
        default_timezone="Europe/Moscow", default_catalog_refresh_interval_minutes=10080,
    )
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    storage.init_db(connection)
    app = build_application(config, connection)
    try:
        command_handlers = [
            h for group in app.handlers.values() for h in group
            if isinstance(h, (CommandHandler, MessageHandler))
        ]
        assert command_handlers, "expected at least one CommandHandler/MessageHandler"
        for handler in command_handlers:
            # filters.UpdateType.MESSAGE must be part of the composed filter
            # so edited_message updates never reach the handler.
            assert "MESSAGE" in repr(handler.filters) or "message" in repr(handler.filters).lower(), (
                f"{handler.callback.__name__} is not restricted to non-edited messages: {handler.filters!r}"
            )
    finally:
        app.bot_data["_scheduler"].shutdown(wait=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py::test_command_handlers_ignore_edited_messages -v`
Expected: FAIL — `CommandHandler`'s default filter has no "MESSAGE"/"message" in its repr.

- [ ] **Step 3: Write minimal implementation**

In `main.py`, add the filter import already present (`filters` is imported) and append `filters=filters.UpdateType.MESSAGE` to every `CommandHandler(...)` call and combine it into the trailing `MessageHandler`. Example diff shape (apply to all ~28 `CommandHandler` registrations the same way):

```python
    application.add_handler(CommandHandler("start", start_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("subscribe", subscribe_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("schedule", schedule_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("classmates", classmates_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("help", help_command, filters=filters.UpdateType.MESSAGE))
    ...
    application.add_handler(CommandHandler("add_institute", add_institute_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("set_institute_catalog_url", set_institute_catalog_url_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("set_institute_check_interval", set_institute_check_interval_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("set_institute_send_time", set_institute_send_time_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("set_institute_catalog_interval", set_institute_catalog_interval_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("refresh_institute_links", refresh_institute_links_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("institute_links", institute_links_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("refresh", refresh_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("alert", alert_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("starosta", starosta_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("votes", votes_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("change_group", change_group_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("set_starosta", set_starosta_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("remove_starosta", remove_starosta_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("close_group", close_group_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("open_group", open_group_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("notify", notify_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("requests", requests_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("menu", menu_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("report", report_command, filters=filters.UpdateType.MESSAGE))
    application.add_handler(CommandHandler("faq", faq_command, filters=filters.UpdateType.MESSAGE))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, pending_action_followup)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: PASS, and no pre-existing test in `test_main_wiring.py`/`test_bot_user_commands.py`/`test_bot_admin_commands.py` regresses (they all build `Update` objects with `update.message` set, which still satisfies `filters.UpdateType.MESSAGE`).

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "fix: restrict all CommandHandler/MessageHandler registrations to non-edited messages so editing a command never re-triggers it with update.message=None"
```

---

### Task 3: Safe inline-keyboard edit helper, applied everywhere `edit_message_text`/`edit_reply_markup` is called

**Files:**
- Modify: `schedule_bot/telegram_utils.py`
- Modify: `schedule_bot/menu.py` (every `query.edit_message_text(...)` call — lines 101, 121, 157, 170, 284, 291, 298, 301, 306, 310, 314, 339, 344, 350, 367, 401, 606, 613)
- Modify: `schedule_bot/group_commands.py:94, 117` (`query.message.edit_reply_markup`, `query.message.edit_text`)
- Modify: `schedule_bot/change_group_commands.py:85` (`query.message.edit_reply_markup`)
- Modify: `schedule_bot/starosta_candidates.py:47` (`query.message.edit_reply_markup`)
- Test: `tests/test_telegram_utils.py` (new file)

**Interfaces:**
- Produces: `telegram_utils.safe_edit_message_text(query, text, **kwargs) -> None` and `telegram_utils.safe_edit_reply_markup(query, **kwargs) -> None`. Every later task that touches a `query.edit_message_text`/`query.message.edit_reply_markup` call site must route through these instead of calling the raw PTB method.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_utils.py
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from schedule_bot.telegram_utils import safe_edit_message_text, safe_edit_reply_markup


def test_safe_edit_message_text_swallows_not_modified_error():
    query = MagicMock()
    query.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    asyncio.run(safe_edit_message_text(query, "same text"))
    query.edit_message_text.assert_awaited_once()


def test_safe_edit_message_text_reraises_other_bad_request():
    query = MagicMock()
    query.edit_message_text = AsyncMock(side_effect=BadRequest("Chat not found"))
    with pytest.raises(BadRequest):
        asyncio.run(safe_edit_message_text(query, "text"))


def test_safe_edit_reply_markup_swallows_not_modified_error():
    query = MagicMock()
    query.message.edit_reply_markup = AsyncMock(side_effect=BadRequest("Message is not modified"))
    asyncio.run(safe_edit_reply_markup(query, reply_markup=None))
    query.message.edit_reply_markup.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_telegram_utils.py -v`
Expected: FAIL — `ImportError: cannot import name 'safe_edit_message_text'`.

- [ ] **Step 3: Write minimal implementation**

Add to `schedule_bot/telegram_utils.py`:

```python
from telegram.error import BadRequest, RetryAfter, TimedOut

_NOT_MODIFIED_MARKER = "message is not modified"


async def safe_edit_message_text(query, text: str, **kwargs) -> None:
    """Wraps query.edit_message_text, swallowing the extremely common
    "Message is not modified" BadRequest a double-tapped navigation
    button produces (the second tap re-sends identical text+markup).
    Any other BadRequest (stale query, chat not found, ...) propagates
    to the Application's global error handler."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as error:
        if _NOT_MODIFIED_MARKER not in str(error).lower():
            raise


async def safe_edit_reply_markup(query, **kwargs) -> None:
    try:
        await query.message.edit_reply_markup(**kwargs)
    except BadRequest as error:
        if _NOT_MODIFIED_MARKER not in str(error).lower():
            raise
```

Then replace every call site. In `schedule_bot/menu.py`, add the import:

```python
from schedule_bot.telegram_utils import safe_edit_message_text, send_with_retry
```

and change every occurrence of `await query.edit_message_text(X, reply_markup=Y)` to `await safe_edit_message_text(query, X, reply_markup=Y)` (18 call sites listed above — mechanical find/replace, keep each call's exact arguments).

In `schedule_bot/group_commands.py`:

```python
from schedule_bot.telegram_utils import reply, safe_edit_message_text, safe_edit_reply_markup
```

- line 94 (`institute_page_callback`): `await query.message.edit_reply_markup(reply_markup=build_institute_picker_keyboard(conn, page))` → `await safe_edit_reply_markup(query, reply_markup=build_institute_picker_keyboard(conn, page))`
- line 106-109 (`institute_select_callback`): `await query.message.edit_text(...)` → `await safe_edit_message_text(query, f"Институт «{institute.name}». Выберите группу:", reply_markup=build_group_picker_keyboard(conn, institute_id))`
- line 117 (`group_page_callback`): same pattern as institute_page_callback.

In `schedule_bot/change_group_commands.py`:

```python
from schedule_bot.telegram_utils import reply, safe_edit_reply_markup, send_with_retry
```

- line 85: `await query.message.edit_reply_markup(reply_markup=build_change_group_keyboard(conn, member.group_id, page))` → `await safe_edit_reply_markup(query, reply_markup=build_change_group_keyboard(conn, member.group_id, page))`

In `schedule_bot/starosta_candidates.py`:

```python
from schedule_bot.telegram_utils import safe_edit_reply_markup
```

- line 47: `await query.message.edit_reply_markup(reply_markup=build_candidate_keyboard(conn, member.group_id, page))` → `await safe_edit_reply_markup(query, reply_markup=build_candidate_keyboard(conn, member.group_id, page))`

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_telegram_utils.py tests/test_menu.py tests/test_group_commands.py tests/test_change_group_commands.py tests/test_starosta_candidates.py -v`
Expected: PASS (existing tests assert on the mocked `edit_message_text`/`edit_reply_markup` calls themselves and are unaffected by the wrapper, since the wrapper calls through with identical arguments on the success path).

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/telegram_utils.py schedule_bot/menu.py schedule_bot/group_commands.py schedule_bot/change_group_commands.py schedule_bot/starosta_candidates.py tests/test_telegram_utils.py
git commit -m "fix: swallow Telegram's \"message is not modified\" BadRequest on every inline-keyboard edit so a double-tapped button never crashes the handler"
```

---

### Task 4: Pending menu action gets a TTL and an explicit cancel path

**Files:**
- Modify: `schedule_bot/menu.py` (`_handle_admin_section`, `_handle_starosta_section`, `_handle_group_scoped_pick`, `_handle_institute_scoped_pick`, `menu_command`, `pending_action_followup`, `menu_callback`)
- Test: `tests/test_menu.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: every `context.user_data["pending_menu_action"] = {...}` write now includes a `"started_at"` key (ISO timestamp, `datetime.now(timezone.utc).isoformat()`). `pending_action_followup` reads it and discards expired entries instead of acting on them.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_menu.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot.menu import PENDING_ACTION_TTL_SECONDS, pending_action_followup


@pytest.mark.asyncio
async def test_pending_action_followup_discards_expired_alert_instead_of_broadcasting(conn):
    stale_started_at = (datetime.now(timezone.utc) - timedelta(seconds=PENDING_ACTION_TTL_SECONDS + 1)).isoformat()
    context = MagicMock()
    context.bot_data = {"conn": conn, "admin_chat_id": 999}
    context.user_data = {"pending_menu_action": {"action": "alert", "started_at": stale_started_at}}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()

    update = MagicMock()
    update.message.text = "это случайный текст, а не объявление"
    update.effective_chat.id = 123
    update.message.reply_text = AsyncMock()

    await pending_action_followup(update, context)

    context.bot.send_message.assert_not_awaited()
    update.message.reply_text.assert_awaited_once()
    assert "истекло" in update.message.reply_text.call_args.args[0].lower()
    assert "pending_menu_action" not in context.user_data
```

Add the shared `conn` fixture at the top of `tests/test_menu.py` if one isn't already defined (check the existing file first — it very likely already has one; reuse it, do not redefine).

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_menu.py::test_pending_action_followup_discards_expired_alert_instead_of_broadcasting -v`
Expected: FAIL — `ImportError: cannot import name 'PENDING_ACTION_TTL_SECONDS'`.

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/menu.py`, add near the top (after `ROOT_CALLBACK_DATA`):

```python
PENDING_ACTION_TTL_SECONDS = 300  # 5 minutes — long enough to type a message, short enough that an
# abandoned "Отправьте текст..." prompt can't be reactivated by an unrelated later message.


def _pending_action(action: str, **extra) -> dict:
    return {"action": action, "started_at": datetime.now(timezone.utc).isoformat(), **extra}
```

Replace every `context.user_data["pending_menu_action"] = {"action": "X", ...}` literal in `menu.py` with `context.user_data["pending_menu_action"] = _pending_action("X", ...)`. Concretely:

- `_handle_starosta_section`: `{"action": "notify", "group_id": group.id}` → `_pending_action("notify", group_id=group.id)`
- `_handle_admin_section`: `{"action": "alert"}` → `_pending_action("alert")`; `{"action": "create_institute_name"}` → `_pending_action("create_institute_name")`
- `_handle_group_scoped_pick`: `{"action": "set_starosta", "group_id": group_id}` → `_pending_action("set_starosta", group_id=group_id)`; `{"action": "notify_admin", "group_id": group_id}` → `_pending_action("notify_admin", group_id=group_id)`
- `_handle_institute_scoped_pick`: the four `set_institute_*` actions, each → `_pending_action("set_institute_catalog_url", institute_id=institute_id)` etc.
- `menu_callback`: `{"action": "report"}` → `_pending_action("report")`
- `group_commands.py`'s `institute_request_start_callback`/`group_request_start_callback` also set `pending_menu_action` directly — update those two call sites the same way, importing `_pending_action` from `schedule_bot.menu`.
- `pending_action_followup`'s own re-prompts (`create_institute_url`, `new_institute_url`) also need `_pending_action(...)`.

Then change `pending_action_followup`'s entry (right after `pending = context.user_data.get("pending_menu_action")`):

```python
async def pending_action_followup(update, context) -> None:
    pending = context.user_data.get("pending_menu_action")
    if pending is None:
        return

    started_at = pending.get("started_at")
    if started_at is not None:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(started_at)
        if age.total_seconds() > PENDING_ACTION_TTL_SECONDS:
            del context.user_data["pending_menu_action"]
            await update.message.reply_text(
                "Время ожидания истекло, действие отменено. Откройте /menu заново, если оно ещё нужно."
            )
            return

    conn = context.bot_data["conn"]
    action = pending["action"]
    value = update.message.text
    del context.user_data["pending_menu_action"]
    # ... rest unchanged
```

Finally, clear any stale pending action on `/menu` itself (belt-and-suspenders for the case where the TTL hasn't elapsed yet but the admin has clearly navigated away):

```python
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_menu_action", None)
    conn = context.bot_data["conn"]
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_menu.py -v`
Expected: PASS — including every existing `pending_menu_action` test, since `_pending_action(...)` still produces a dict containing `"action"` and whatever keys those tests assert on (they don't assert the dict's exact `==`, only field access; if any does assert exact equality, update that assertion to also expect a `"started_at"` key — grep `pending_menu_action` ==" in the test file and adjust).

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/menu.py schedule_bot/group_commands.py tests/test_menu.py
git commit -m "fix: expire pending_menu_action after 5 minutes so an abandoned admin prompt (alert/notify/set_*) can't be accidentally triggered by an unrelated later message"
```

---

### Task 5: APScheduler job_defaults — stop silently dropping missed daily sends

**Files:**
- Modify: `schedule_bot/scheduler.py:332-341` (`build_scheduler`)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Produces: no signature change — `build_scheduler(...)` still returns an `AsyncIOScheduler`, just constructed with explicit `job_defaults`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler.py
def test_build_scheduler_sets_generous_misfire_grace_time(conn, shutdown_scheduler):
    bot = MagicMock()
    week_formula_holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, _config(), bot, week_formula_holder)
    shutdown_scheduler(scheduler)

    assert scheduler._job_defaults["misfire_grace_time"] >= 3600
    assert scheduler._job_defaults["coalesce"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py::test_build_scheduler_sets_generous_misfire_grace_time -v`
Expected: FAIL — default APScheduler `misfire_grace_time` is 1.

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/scheduler.py`:

```python
    scheduler = AsyncIOScheduler(
        timezone=config.default_timezone,
        job_defaults={"misfire_grace_time": 3600, "coalesce": True, "max_instances": 1},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/scheduler.py tests/test_scheduler.py
git commit -m "fix: give the scheduler a 1-hour misfire grace period so a daily send delayed by event-loop contention isn't dropped for the whole day"
```

---

### Task 6: Move `render_day_image` off the event loop

**Files:**
- Modify: `schedule_bot/bot.py:142-181` (`day_callback`)
- Modify: `schedule_bot/scheduler.py:118-156` (`link_daily_send_job`)
- Test: `tests/test_bot_user_commands.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `screenshot.render_day_image(pdf_path, schedule, day_name) -> bytes | None` (unchanged signature).
- Produces: nothing new — both call sites now `await asyncio.to_thread(render_day_image, ...)` instead of calling it directly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bot_user_commands.py
@pytest.mark.asyncio
async def test_day_callback_renders_image_off_event_loop(monkeypatch):
    import threading
    from schedule_bot import bot as bot_module

    render_thread_name = {}

    def fake_render(pdf_path, schedule, day):
        render_thread_name["name"] = threading.current_thread().name
        return b"fake-png"

    monkeypatch.setattr(bot_module, "render_day_image", fake_render)

    # ... build update/context exactly as the existing day_callback tests do,
    # with a member row, an active_schedule_getter returning a real Schedule,
    # and active_pdf_path_getter returning a dummy path.
    await bot_module.day_callback(update, context)

    assert render_thread_name["name"] != threading.main_thread().name
```

(Reuse whichever fixture/update-builder helper `tests/test_bot_user_commands.py` already defines for `day_callback` tests — do not duplicate it; find the existing `test_day_callback_*` tests in that file and copy their setup verbatim, only swapping in `fake_render` via monkeypatch and the thread-name assertion.)

```python
# tests/test_scheduler.py
@pytest.mark.asyncio
async def test_link_daily_send_job_renders_image_off_event_loop(conn, monkeypatch):
    import threading
    from schedule_bot import scheduler as scheduler_module

    render_thread_name = {}

    def fake_render(pdf_path, schedule, day):
        render_thread_name["name"] = threading.current_thread().name
        return b"fake-png"

    monkeypatch.setattr(scheduler_module, "render_day_image", fake_render)

    institute_id = _make_institute(conn)
    link_id, group_id = _make_link_and_group(conn, institute_id)
    _seed_active_version(conn, group_id)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()

    with monkeypatch.context() as m:
        m.setattr("schedule_bot.scheduler.date", FixedMonday)
        await link_daily_send_job(
            conn, _config(), bot, link_id, WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)),
        )

    assert render_thread_name["name"] != threading.main_thread().name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_bot_user_commands.py::test_day_callback_renders_image_off_event_loop tests/test_scheduler.py::test_link_daily_send_job_renders_image_off_event_loop -v`
Expected: FAIL — the fake render runs on the main thread (same thread as the test's event loop).

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/bot.py`, `day_callback`:

```python
    try:
        image_bytes = await asyncio.to_thread(render_day_image, pdf_path, schedule, day_name)
    except Exception as error:
        logger.warning("bot: failed to render day image for %r: %s", pdf_path, error)
        image_bytes = None
```

In `schedule_bot/scheduler.py`, `link_daily_send_job`:

```python
        image_bytes = None
        try:
            image_bytes = await asyncio.to_thread(render_day_image, active.pdf_cache_path, schedule, day_name)
        except Exception as error:
            ...  # unchanged body
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_bot_user_commands.py tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/bot.py schedule_bot/scheduler.py tests/test_bot_user_commands.py tests/test_scheduler.py
git commit -m "fix: run the PyMuPDF screenshot render in a worker thread so it can't stall the event loop (and the scheduler's own timer) for every other user"
```

---

### Task 7: Per-institute lock around `refresh_institute_catalog`

**Files:**
- Modify: `main.py` (new `catalog_locks` dict, `sync_institute_link_fn`'s rescan path doesn't need it directly — see Task 8 — but the manual/menu/command paths do)
- Modify: `schedule_bot/group_links.py` (`refresh_institute_catalog` gains an optional `lock` parameter is NOT the right shape here — locking must wrap the *caller's* `asyncio.to_thread` call, not the sync function itself, since the function is plain blocking code with no `await`. Lock at the call sites instead.)
- Modify: `schedule_bot/scheduler.py:159-169` (`institute_catalog_refresh_job`)
- Modify: `schedule_bot/menu.py:424-439` (`refreshinstitutelinks` branch of `_handle_institute_scoped_pick`)
- Modify: `schedule_bot/institute_commands.py:21-55, 168-191` (`create_institute_and_scan`, `refresh_institute_links_command`)
- Modify: `schedule_bot/legacy_migration.py:44-51`
- Modify: `schedule_bot/sync.py:160-188` (the 404-fallback rescan)
- Test: `tests/test_group_links_catalog.py`

**Interfaces:**
- Produces: a new helper `schedule_bot.sync.catalog_lock_for(catalog_locks: dict[int, asyncio.Lock], institute_id: int) -> asyncio.Lock`, used by every caller below so there is exactly one lock-acquisition idiom instead of six slightly different ones. One dict (`catalog_locks`), created once in `main.py` exactly like `link_locks`, threaded through every scheduler/menu/command/sync call site the same way `link_locks` already is.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_group_links_catalog.py
import asyncio
import sqlite3

import pytest

from schedule_bot import storage
from schedule_bot.sync import catalog_lock_for


def test_catalog_lock_for_returns_same_lock_for_same_institute():
    locks: dict[int, asyncio.Lock] = {}
    lock_a = catalog_lock_for(locks, 7)
    lock_b = catalog_lock_for(locks, 7)
    assert lock_a is lock_b


@pytest.mark.asyncio
async def test_two_concurrent_catalog_scans_of_same_institute_serialize(monkeypatch, tmp_path):
    """Two 'scans' that each append to a shared list while holding the
    lock must never interleave — this is the mechanism the real
    refresh_institute_catalog relies on to avoid the two-scan corruption
    described in the review (duplicate pdf_url INSERTs / wrong
    stale-marking)."""
    import schedule_bot.main_scan_harness as harness  # see Step 3 note: this module doesn't need to
    # exist for real; the test below exercises the lock directly instead,
    # since locking the actual refresh_institute_catalog call is the
    # caller's responsibility (main.py/scheduler.py/menu.py), not
    # group_links.py's.

    locks: dict[int, asyncio.Lock] = {}
    events: list[str] = []

    async def scan(tag: str):
        lock = catalog_lock_for(locks, 1)
        async with lock:
            events.append(f"{tag}:start")
            await asyncio.sleep(0.01)
            events.append(f"{tag}:end")

    await asyncio.gather(scan("A"), scan("B"))
    # Serialized: one scan's start+end must be contiguous, never interleaved.
    assert events in (["A:start", "A:end", "B:start", "B:end"], ["B:start", "B:end", "A:start", "A:end"])
```

Drop the bogus `import schedule_bot.main_scan_harness` line from the second test before running — it was a stray artifact of drafting; the test only needs `catalog_lock_for` and `asyncio`. Final test body for `test_two_concurrent_catalog_scans_of_same_institute_serialize` has no such import.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_group_links_catalog.py::test_catalog_lock_for_returns_same_lock_for_same_institute -v`
Expected: FAIL — `ImportError: cannot import name 'catalog_lock_for'`.

- [ ] **Step 3: Write minimal implementation**

Add to `schedule_bot/sync.py` (near `_catalog_rescan_allowed`):

```python
def catalog_lock_for(catalog_locks: dict[int, asyncio.Lock], institute_id: int) -> asyncio.Lock:
    """One asyncio.Lock per institute_id, shared by every caller that can
    trigger refresh_institute_catalog for that institute: the manual
    menu button, /add_institute, /refresh_institute_links, the
    background institute_catalog_refresh_job, the legacy migration's
    one-shot scan, and sync.py's own 404-fallback rescan. Two concurrent
    scans of the same institute would otherwise both read the same
    pre-scan snapshot of institute_links and independently mutate the
    DB from it — see _resolve_link_identity's docstring for why that
    snapshot assumption breaks under concurrency."""
    return catalog_locks.setdefault(institute_id, asyncio.Lock())
```

Now thread `catalog_locks` through every call site, mirroring exactly how `link_locks` already flows:

In `main.py`, alongside `link_locks`/`catalog_rescan_state`:

```python
    catalog_locks: dict[int, asyncio.Lock] = {}
```

Pass it into `sync_institute_link_fn`'s closure is NOT needed (that function doesn't call `refresh_institute_catalog` directly — `sync_institute_link` does, internally, via its own `catalog_rescan_state` gate; Task 7 instead wires `catalog_locks` into `sync_institute_link`'s signature so its internal fallback rescan also serializes against the manual paths):

```python
    async def sync_institute_link_fn(institute_link_id: int):
        ...
        lock = link_locks.setdefault(institute_link_id, asyncio.Lock())
        async with lock:
            return await sync_institute_link(
                conn, institute_link, institute, config.pdf_cache_dir, notify,
                catalog_rescan_state=catalog_rescan_state,
                notify_admin=notify_admin,
                catalog_locks=catalog_locks,
            )
```

Expose it for the menu/command handlers and the scheduler:

```python
    application.bot_data["_catalog_locks"] = catalog_locks
```

and pass `catalog_locks` into `build_scheduler(..., catalog_locks=catalog_locks)`.

In `schedule_bot/sync.py`, change `sync_institute_link`'s signature and its fallback-rescan block:

```python
async def sync_institute_link(
    conn, institute_link, institute, pdf_cache_dir: str, notify: Notifier,
    *, catalog_rescan_state: dict[int, float],
    notify_admin: AdminNotifier | None = None,
    catalog_locks: dict[int, asyncio.Lock] | None = None,
) -> list[SyncResult]:
    ...
    if pdf_bytes is None:
        if _catalog_rescan_allowed(catalog_rescan_state, institute):
            catalog_alerts: list[str] = []
            lock = catalog_lock_for(catalog_locks, institute.id) if catalog_locks is not None else None
            try:
                if lock is None:
                    await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=catalog_alerts.append)
                else:
                    async with lock:
                        await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=catalog_alerts.append)
            except Exception as refresh_error:
                ...  # unchanged
```

`catalog_locks=None` is kept as an accepted default so every existing direct unit-test call to `sync_institute_link` (which doesn't pass `catalog_locks`) keeps working unlocked, matching how `notify_admin=None` already behaves.

In `schedule_bot/scheduler.py`, `institute_catalog_refresh_job` gains a `lock: asyncio.Lock | None = None` parameter used the same way as `link_check_job`'s:

```python
async def institute_catalog_refresh_job(
    conn, config, bot, institute_id: int, reschedule_links=None, lock: asyncio.Lock | None = None,
) -> None:
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        return

    alerts: list[str] = []
    try:
        async def _do_scan():
            await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)

        if lock is None:
            await _do_scan()
        else:
            async with lock:
                await _do_scan()
    except Exception as error:
        logger.warning("scheduler: institute_catalog_refresh_job failed for institute %r: %s", institute.name, error)
        return
    ...  # unchanged
```

`schedule_institute_catalog_job` and `reschedule_institute_jobs`/`build_scheduler` need `catalog_locks: dict` threaded down to pass as the `lock` arg, exactly parallel to how `locks: dict` (the per-link locks) is already threaded:

```python
def schedule_institute_catalog_job(
    scheduler, conn, config, bot, institute, reschedule_links=None, catalog_locks: dict | None = None,
) -> None:
    interval_minutes = _safe_interval_minutes(
        institute.catalog_refresh_interval_minutes, config.default_catalog_refresh_interval_minutes
    )
    lock = catalog_lock_for(catalog_locks, institute.id) if catalog_locks is not None else None
    scheduler.add_job(
        institute_catalog_refresh_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot, institute.id, reschedule_links, lock],
        id=f"institute_catalog_refresh_{institute.id}",
        replace_existing=True,
    )
```

(Add `from schedule_bot.sync import catalog_lock_for` to `scheduler.py`'s imports.) Update `make_links_rescheduler`, `reschedule_institute_jobs`, and `build_scheduler` to accept and forward `catalog_locks: dict | None = None`, defaulting to `{}` exactly like `locks`/`catalog_rescan_state` already do, and pass it down to every `schedule_institute_catalog_job(...)` call.

In `main.py`'s `reschedule_institute_jobs_cb`, forward `catalog_locks` the same way `link_locks`/`catalog_rescan_state` are forwarded.

In `schedule_bot/menu.py`'s `_handle_institute_scoped_pick`, `refreshinstitutelinks` branch:

```python
    if action == "refreshinstitutelinks":
        from schedule_bot.sync import catalog_lock_for

        catalog_locks = context.bot_data["_catalog_locks"]
        lock = catalog_lock_for(catalog_locks, institute_id)
        alerts = []
        try:
            async with lock:
                await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
        except Exception as error:
            await query.message.reply_text(f"Сканирование каталога не удалось: {error}")
            return
        ...  # unchanged
```

In `schedule_bot/institute_commands.py`, `create_institute_and_scan` and `refresh_institute_links_command` take the same `catalog_locks` dict via `context.bot_data["_catalog_locks"]` at the call site (command handlers have `context`; `create_institute_and_scan` itself gets a new optional `lock: asyncio.Lock | None = None` parameter wrapping its `asyncio.to_thread` call):

```python
async def create_institute_and_scan(conn, config, name: str, catalog_url: str, notify_admin, lock: asyncio.Lock | None = None) -> tuple[int, str]:
    institute_id = storage.create_institute(...)
    try:
        async def _do_scan():
            await asyncio.to_thread(
                refresh_institute_catalog, conn, storage.get_institute(conn, institute_id), notify_admin=notify_admin
            )
        if lock is None:
            await _do_scan()
        else:
            async with lock:
                await _do_scan()
        ...  # unchanged
```

(A brand-new institute has no pre-existing lock contention risk, but passing the lock keeps the idiom uniform and protects against the admin creating the same institute twice in a race.) Update its two callers (`add_institute_command` in `institute_commands.py`, and `menu.py`'s `create_institute_url` branch in `pending_action_followup`) to pass `context.bot_data["_catalog_locks"]`-derived locks — for `add_institute_command` the institute doesn't exist yet at lock-acquisition time, so lock by name is not meaningful; pass `lock=None` there (no pre-existing institute_id to key on) and rely on the fact that two admins racing to create "the same" institute twice is a vastly rarer, lower-stakes scenario than racing scans of an *existing* institute — out of scope for this task.

`refresh_institute_links_command`:

```python
    from schedule_bot.sync import catalog_lock_for

    lock = catalog_lock_for(context.bot_data["_catalog_locks"], institute.id)
    alerts = []
    try:
        async with lock:
            await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
    except Exception as error:
        await update.message.reply_text(f"Сканирование каталога не удалось: {error}")
        return
    ...  # unchanged
```

In `schedule_bot/legacy_migration.py`, `run_legacy_migration_if_configured` gains a `catalog_locks: dict | None = None` parameter forwarded as `lock` around its own `asyncio.to_thread(refresh_institute_catalog, ...)` call, and `main.py`'s `_post_init` passes `catalog_locks` through.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_group_links_catalog.py tests/test_sync.py tests/test_scheduler.py tests/test_menu.py tests/test_institute_commands.py tests/test_legacy_migration.py tests/test_main_wiring.py -v`
Expected: PASS. Fix any call site this task's mechanical signature changes broke in the existing tests (most will need an extra `lock=None`/`catalog_locks=None` default-filled argument, which is backward compatible since every new parameter above has a default).

- [ ] **Step 5: Commit**

```bash
git add main.py schedule_bot/sync.py schedule_bot/scheduler.py schedule_bot/menu.py schedule_bot/institute_commands.py schedule_bot/legacy_migration.py tests/test_group_links_catalog.py
git commit -m "fix: serialize all catalog-rescan entry points for the same institute behind one per-institute asyncio.Lock, preventing two concurrent scans from corrupting institute_links/groups or wrongly marking live links stale"
```

---

### Task 8: Re-read `institute_link`/`institute` inside the per-link lock, not before it

**Files:**
- Modify: `schedule_bot/scheduler.py:33-43, 83-89` (`link_check_job`, `link_daily_send_job`)
- Modify: `main.py:192-212` (`sync_institute_link_fn`)
- Test: `tests/test_scheduler.py`, `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: `storage.get_institute_link(conn, link_id)`, `storage.get_institute(conn, institute_id)` (unchanged).
- Produces: no new public interface — this only moves two existing calls inside the `async with lock:` block in three places.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler.py
@pytest.mark.asyncio
async def test_link_check_job_sees_pdf_url_change_that_happened_while_waiting_for_the_lock(conn, monkeypatch):
    """If another holder of the lock changes institute_links.pdf_url while
    this job is waiting to acquire the lock, the job must sync against
    the NEW pdf_url, not a stale snapshot taken before it queued."""
    institute_id = _make_institute(conn)
    link_id, group_id = _make_link_and_group(conn, institute_id, pdf_url="/old.pdf")
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    lock = asyncio.Lock()
    seen_urls = []

    async def fake_sync_institute_link(conn_, institute_link, institute, *args, **kwargs):
        seen_urls.append(institute_link.pdf_url)
        return []

    monkeypatch.setattr("schedule_bot.scheduler.sync_institute_link", fake_sync_institute_link)

    async def hold_lock_then_mutate():
        async with lock:
            storage.update_institute_link_pdf_url(conn, link_id, "/new.pdf")
            await asyncio.sleep(0.01)

    await asyncio.gather(
        hold_lock_then_mutate(),
        link_check_job(conn, _config(), MagicMock(), link_id, lock=lock, catalog_rescan_state={}),
    )

    assert seen_urls == ["/new.pdf"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest "tests/test_scheduler.py::test_link_check_job_sees_pdf_url_change_that_happened_while_waiting_for_the_lock" -v`
Expected: FAIL — `seen_urls == ["/old.pdf"]` because `link_check_job` currently reads `institute_link` before `async with lock`.

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/scheduler.py`, `link_check_job`:

```python
async def link_check_job(
    conn, config, bot, institute_link_id: int, lock: asyncio.Lock | None = None,
    catalog_rescan_state: dict | None = None,
) -> None:
    rescan_state = catalog_rescan_state if catalog_rescan_state is not None else {}

    async def _do_sync():
        institute_link = storage.get_institute_link(conn, institute_link_id)
        if institute_link is None:
            logger.warning("scheduler: link_check_job skipped, institute_link_id=%s no longer exists", institute_link_id)
            return
        institute = storage.get_institute(conn, institute_link.institute_id)
        if institute is None:
            return
        return await sync_institute_link(
            conn, institute_link, institute, config.pdf_cache_dir,
            lambda chat_id, text: _notify(bot, chat_id, text),
            catalog_rescan_state=rescan_state,
            notify_admin=lambda text: _notify(bot, config.admin_chat_id, text),
        )

    logger.info("scheduler: link_check_job starting for link id=%s", institute_link_id)
    try:
        if lock is None:
            await _do_sync()
        else:
            async with lock:
                await _do_sync()
    except Exception as error:
        ...  # unchanged
```

Apply the identical restructuring to `link_daily_send_job` (move the `institute_link`/`institute` lookups and the "if None: return" guards from the top of the function into the top of its own `_do_sync` closure — the rest of the function, the per-group rendering/sending loop, stays where it is and keeps reading `institute_link_id`/the freshly-fetched `institute` from the closure's return, so declare `institute_link`/`institute` as `nonlocal`-captured results):

```python
async def link_daily_send_job(
    conn, config, bot, institute_link_id: int, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None,
    catalog_rescan_state: dict | None = None,
) -> None:
    rescan_state = catalog_rescan_state if catalog_rescan_state is not None else {}
    state: dict = {}

    async def _do_sync():
        institute_link = storage.get_institute_link(conn, institute_link_id)
        if institute_link is None:
            logger.warning("scheduler: link_daily_send_job skipped, institute_link_id=%s no longer exists", institute_link_id)
            return
        institute = storage.get_institute(conn, institute_link.institute_id)
        if institute is None:
            return
        state["institute"] = institute
        await sync_institute_link(
            conn, institute_link, institute, config.pdf_cache_dir,
            lambda chat_id, text: _notify(bot, chat_id, text),
            catalog_rescan_state=rescan_state,
            notify_admin=lambda text: _notify(bot, config.admin_chat_id, text),
        )

    try:
        if lock is None:
            await _do_sync()
        else:
            async with lock:
                await _do_sync()
    except Exception as error:
        logger.warning("scheduler: link_daily_send_job sync failed for link id=%s: %s", institute_link_id, error)

    institute = state.get("institute") or storage.get_institute(
        conn, (storage.get_institute_link(conn, institute_link_id) or storage.InstituteLinkRow(0, 0, "", "[]", "", False, "", "")).institute_id
    )
    if institute is None:
        return
    today = today_in_configured_timezone(institute.timezone)
    ...  # unchanged from here
```

(The fallback re-fetch after the `try` block handles the case where `_do_sync` raised before setting `state["institute"]` — e.g. the sync itself failed — while still needing `institute.timezone` for the send-time logic below; this mirrors the function's pre-existing behavior of continuing to the send loop even when sync failed.)

In `main.py`, `sync_institute_link_fn`:

```python
    async def sync_institute_link_fn(institute_link_id: int):
        async def notify(chat_id, text):
            await send_with_retry(lambda: application.bot.send_message(chat_id=chat_id, text=text))

        async def notify_admin(text):
            await send_with_retry(
                lambda: application.bot.send_message(chat_id=config.admin_chat_id, text=text)
            )

        lock = link_locks.setdefault(institute_link_id, asyncio.Lock())
        async with lock:
            institute_link = storage.get_institute_link(conn, institute_link_id)
            if institute_link is None:
                raise ValueError(f"Institute link {institute_link_id} not found")
            institute = storage.get_institute(conn, institute_link.institute_id)
            return await sync_institute_link(
                conn, institute_link, institute, config.pdf_cache_dir, notify,
                catalog_rescan_state=catalog_rescan_state,
                notify_admin=notify_admin,
                catalog_locks=catalog_locks,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py tests/test_main_wiring.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/scheduler.py main.py tests/test_scheduler.py
git commit -m "fix: re-read institute_link/institute inside the per-link lock instead of before acquiring it, closing a TOCTOU window where a concurrent catalog rescan or /refresh could race a stale snapshot"
```

---

### Task 9: Isolate per-group failures inside `sync_institute_link` (parse errors included)

**Files:**
- Modify: `schedule_bot/sync.py:70-94, 196-249`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `pdf_parser.parse_schedules_for_groups` (unchanged).
- Produces: `sync_institute_link`'s per-group loop body is now wrapped so a `json.loads`/`schedule_from_dict`/parse exception for one group no longer aborts the groups after it in the same link; `_fetch_and_parse_blocking` now catches `parse_schedules_for_groups` raising (e.g. `PDFSyntaxError`, `IndexError` from `page.find_tables()[0]`) and reports it the same way a fetch failure is reported.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sync.py
def test_sync_institute_link_treats_unparsable_pdf_like_a_fetch_failure(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, "/not-a-real.pdf", ["Б-Мв-101"])
    storage.upsert_member(conn, 111, group_ids[0], joined_at="t")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", lambda source: b"not a pdf at all")

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    assert [r.status for r in results] == ["error"]
    group = storage.get_group(conn, group_ids[0])
    assert group.last_sync_failed_at  # must be set, same contract as a fetch failure


def test_sync_institute_link_continues_to_next_group_after_one_groups_insert_fails(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(
        conn, institute_id, FIXTURE_PATH, ["Б-Мв-101", "Б-Мв-102"]
    )
    for gid in group_ids:
        storage.upsert_member(conn, 1000 + gid, gid, joined_at="t")

    real_insert = storage.insert_new_version
    call_count = {"n": 0}

    def flaky_insert(conn_, group_id, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_insert(conn_, group_id, *args, **kwargs)

    monkeypatch.setattr(storage, "insert_new_version", flaky_insert)

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    assert len(results) == 2
    assert {r.group_id for r in results} == set(group_ids)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_sync.py::test_sync_institute_link_treats_unparsable_pdf_like_a_fetch_failure tests/test_sync.py::test_sync_institute_link_continues_to_next_group_after_one_groups_insert_fails -v`
Expected: FAIL — the first test raises out of `_load_pdf_bytes`'s wrapper (since `parse_schedules_for_groups` on garbage bytes raises an unhandled exception, which propagates out of `sync_institute_link` instead of producing `"error"` results); the second test only gets 1 result back (the exception from `flaky_insert` propagates and aborts the loop for the second group).

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/sync.py`, wrap the parse call in `_fetch_and_parse_blocking`:

```python
def _fetch_and_parse_blocking(
    pdf_source: str, group_names: list[str]
) -> tuple[bytes | None, dict[str, object | None], Exception | None]:
    pdf_bytes, error = _fetch_pdf_bytes_with_retries(pdf_source)
    if pdf_bytes is None:
        return None, {}, error

    tmp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp_file.write(pdf_bytes)
        tmp_file.close()
        try:
            schedules = parse_schedules_for_groups(tmp_file.name, group_names)
        except Exception as parse_error:
            logger.warning("sync: failed to parse PDF from %r: %s", pdf_source, parse_error)
            return None, {}, parse_error
        return pdf_bytes, schedules, None
    finally:
        try:
            os.unlink(tmp_file.name)
        except OSError:
            pass
```

This alone makes `pdf_bytes is None` true for an unparsable PDF too, so it already falls into the existing "fetch failed" branch (404-fallback rescan, `last_sync_failed_at` set on every active group, `status="error"` results) — no further change needed for the first test.

For the per-group isolation, wrap the body of the `for group in active_groups:` loop:

```python
    results: list[SyncResult] = []
    for group in active_groups:
        try:
            new_schedule = schedules.get(group.name)
            if new_schedule is None:
                storage.set_group_last_sync_failed_at(conn, group.id, datetime.now(timezone.utc).isoformat())
                results.append(SyncResult(group_id=group.id, status="error"))
                continue

            storage.set_group_last_sync_failed_at(conn, group.id, "")
            active = storage.get_active_version(conn, group.id)
            old_schedule = schedule_from_dict(json.loads(active.payload_json)) if active else None
            changes = compute_diff(old_schedule, new_schedule)

            if active is not None and not changes:
                results.append(SyncResult(group_id=group.id, status="no_changes"))
                continue

            cache_path = _write_cache(pdf_bytes, pdf_cache_dir, institute_link.id, group.name)
            parsed_at = datetime.now(timezone.utc).isoformat()
            try:
                storage.insert_new_version(conn, group.id, json.dumps(schedule_to_dict(new_schedule)), cache_path, parsed_at)
            except Exception:
                Path(cache_path).unlink(missing_ok=True)
                raise

            if active is not None and active.pdf_cache_path:
                try:
                    Path(active.pdf_cache_path).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("sync: failed to delete superseded cache file %r: %s", active.pdf_cache_path, exc)

            if active is None:
                results.append(SyncResult(group_id=group.id, status="initial"))
                continue

            message = "Расписание изменилось. Актуальную версию смотрите на сайте"
            weektype_url = storage.get_setting(conn, "weektype_url")
            if weektype_url:
                message += ":\n" + weektype_url
            for chat_id in storage.list_group_members(conn, group.id, subscribed_only=True):
                try:
                    await notify(chat_id, message)
                except Exception as exc:
                    logger.warning("sync: failed to notify chat_id=%s for group %r: %s", chat_id, group.name, exc)
                await asyncio.sleep(SEND_PACING_SECONDS)

            results.append(SyncResult(group_id=group.id, status="applied", changes=changes))
        except Exception as error:
            logger.exception("sync: failed to apply sync result for group %r (id=%s): %s", group.name, group.id, error)
            results.append(SyncResult(group_id=group.id, status="error"))

    return results
```

(This also folds in Task 11's cache-file-leak fix — writing the file, then deleting it if the following `insert_new_version` raises — since both fixes touch the exact same lines; doing them together avoids a second edit to the same block. Task 11 below is kept as a separate task only for traceability of the review's numbering, but its code lands here.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_sync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/sync.py tests/test_sync.py
git commit -m "fix: isolate per-group failures (parse errors, DB write errors) inside sync_institute_link so one bad group never aborts the rest of its link's sync, and clean up an orphaned cache file if insert_new_version fails after it's written"
```

---

### Task 10: Isolate per-group failures inside `link_daily_send_job`

**Files:**
- Modify: `schedule_bot/scheduler.py:118-156` (after Task 8's restructuring)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new — wraps the existing `for group in storage.list_groups_for_link(...)` loop body in try/except.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler.py
@pytest.mark.asyncio
async def test_link_daily_send_job_sends_to_second_group_even_if_first_groups_payload_is_corrupt(conn, monkeypatch):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="/does-not-matter.pdf",
        group_names=["Группа-1", "Группа-2"], raw_label="x", last_seen_at="t",
    )
    group1 = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Группа-1")
    group2 = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Группа-2")
    storage.insert_new_version(conn, group1, "not valid json {{{", "/tmp/x1.pdf", "t")
    _seed_active_version(conn, group2)
    storage.upsert_member(conn, 1111, group1, joined_at="t")
    storage.upsert_member(conn, 2222, group2, joined_at="t")

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    monkeypatch.setattr("schedule_bot.scheduler.date", FixedMonday)

    await link_daily_send_job(
        conn, _config(), bot, link_id,
        WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)),
    )

    sent_chat_ids = {call.kwargs["chat_id"] for call in bot.send_message.await_args_list}
    assert 2222 in sent_chat_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest "tests/test_scheduler.py::test_link_daily_send_job_sends_to_second_group_even_if_first_groups_payload_is_corrupt" -v`
Expected: FAIL — `json.loads("not valid json {{{")` raises and aborts the loop before group2 is ever reached.

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/scheduler.py`, wrap the body of the `for group in storage.list_groups_for_link(conn, institute_link_id):` loop:

```python
    for group in storage.list_groups_for_link(conn, institute_link_id):
        try:
            active = storage.get_active_version(conn, group.id)
            if active is None:
                continue
            schedule = schedule_from_dict(json.loads(active.payload_json))

            text = format_day_schedule(day_name, today, week_type, schedule)
            group = storage.get_group(conn, group.id)
            if group is None:
                continue
            if group.last_sync_failed_at:
                text = STALE_SCHEDULE_WARNING + text

            image_bytes = None
            try:
                image_bytes = await asyncio.to_thread(render_day_image, active.pdf_cache_path, schedule, day_name)
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
        except Exception as error:
            logger.exception("scheduler: failed to process group id=%s for link id=%s: %s", group.id, institute_link_id, error)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/scheduler.py tests/test_scheduler.py
git commit -m "fix: isolate per-group failures in link_daily_send_job so a corrupt payload for one group doesn't cancel the daily send for every group after it on the same link"
```

---

### Task 11: (folded into Task 9 — see its commit)

This task is intentionally empty; the cache-file-leak fix it covers was implemented as part of Task 9's edit to the same code block, to avoid two separate edits to one `try/except` region. No action needed here.

---

### Task 12: Fix two-phase link-identity upsert losing a link when a new URL appears with the same group-name set

**Files:**
- Modify: `schedule_bot/group_links.py:159-185, 262-301` (`_resolve_link_identity`, `refresh_institute_catalog`)
- Test: `tests/test_group_links_catalog.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_resolve_link_identity(snapshot_links, pdf_url, group_names, already_matched_ids)` gains a fourth required parameter — every call site in `refresh_institute_catalog` is updated in the same task, so no caller outside this file needs to change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_group_links_catalog.py
def test_second_pdf_with_same_group_names_does_not_steal_the_first_links_row(conn):
    from schedule_bot.group_links import refresh_institute_catalog

    institute_id = _make_institute(conn)  # reuse this file's existing helper
    html_scan_1 = """
    <a href="/forma/ochnaya">форма обучения очная</a>
    """
    # Simplify by calling refresh_institute_catalog twice with monkeypatched
    # fetch_source_page_html, matching this test file's existing pattern for
    # catalog-scan tests (check the top of tests/test_group_links_catalog.py
    # for the exact monkeypatch helper already used by sibling tests and
    # reuse it rather than re-inventing a fetch mock here).
    ...
```

Because this file already has an established pattern for mocking `fetch_source_page_html` per scan (used by the existing stale-marking/partial-scan tests), write this test by copying that pattern exactly, with two scans:

```python
def test_second_pdf_with_same_group_names_does_not_steal_the_first_links_row(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute = storage.get_institute(conn, _make_institute(conn))

    responses = {
        institute.catalog_url: '<a href="/forma">форма обучения очная</a>',
        "https://example.com/forma": '<a href="/a.pdf">Б-А-301</a>',
    }

    def fake_fetch(url):
        return responses[url]

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", fake_fetch)
    refresh_institute_catalog(conn, institute, notify_admin=lambda text: None)

    links_after_scan_1 = storage.list_institute_links(conn, institute.id)
    assert {l.pdf_url for l in links_after_scan_1} == {"https://example.com/a.pdf"}

    responses["https://example.com/forma"] = (
        '<a href="/a.pdf">Б-А-301</a><a href="/a_s_12_10_2026.pdf">Б-А-301</a>'
    )
    refresh_institute_catalog(conn, institute, notify_admin=lambda text: None)

    links_after_scan_2 = storage.list_institute_links(conn, institute.id)
    urls_after_scan_2 = {l.pdf_url for l in links_after_scan_2 if not l.is_stale}
    assert "https://example.com/a.pdf" in urls_after_scan_2
    assert "https://example.com/a_s_12_10_2026.pdf" in urls_after_scan_2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest "tests/test_group_links_catalog.py::test_second_pdf_with_same_group_names_does_not_steal_the_first_links_row" -v`
Expected: FAIL — after scan 2, only one live link exists (`a_s_12_10_2026.pdf`, having "stolen" the original link row via phase-2 name-set matching), and `a.pdf` has no row at all.

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/group_links.py`:

```python
def _resolve_link_identity(snapshot_links, pdf_url: str, group_names: list[str], already_matched_ids: set[int]):
    """Two-phase match per design spec section 1.2. `already_matched_ids`
    holds the ids of links already resolved earlier in THIS scan's loop —
    phase 2 (name-set match) must never re-match one of them, or two
    distinct PDF urls appearing in the same scan with an identical
    group-name set (e.g. a dated reupload of the same file) collapse
    into a single institute_links row, silently losing the first one."""
    for link in snapshot_links:
        if link.pdf_url == pdf_url:
            return link

    candidate_names = set(group_names)
    if not candidate_names:
        return None
    for link in snapshot_links:
        if link.id in already_matched_ids:
            continue
        if set(json.loads(link.group_names_json)) == candidate_names:
            return link
    return None
```

In `refresh_institute_catalog`, track matched ids as the loop progresses and pass them in:

```python
    confirmed_link_ids: list[int] = []
    already_matched_ids: set[int] = set()
    now = datetime.now(timezone.utc).isoformat()
    seen_group_ids_this_scan: set[int] = set()
    pre_scan_links = storage.list_institute_links(conn, institute.id)

    for pdf_url, raw_labels in labels_by_url.items():
        combined_raw_label = ", ".join(raw_labels)
        expanded = [
            name for raw_label in raw_labels for name in expand_group_names(raw_label) if looks_like_group_name(name)
        ]

        matched = _resolve_link_identity(pre_scan_links, pdf_url, expanded, already_matched_ids)
        if matched is None:
            link_id = storage.create_institute_link(
                conn, institute_id=institute.id, pdf_url=pdf_url,
                group_names=expanded, raw_label=combined_raw_label, last_seen_at=now,
            )
        elif matched.pdf_url != pdf_url:
            storage.update_institute_link_pdf_url(conn, matched.id, pdf_url)
            storage.update_institute_link_group_names(
                conn, matched.id, group_names=expanded, raw_label=combined_raw_label, last_seen_at=now
            )
            link_id = matched.id
            already_matched_ids.add(matched.id)
        else:
            storage.update_institute_link_group_names(
                conn, matched.id, group_names=expanded, raw_label=combined_raw_label, last_seen_at=now
            )
            link_id = matched.id
            already_matched_ids.add(matched.id)

        confirmed_link_ids.append(link_id)
        _upsert_groups_for_link(conn, institute.id, link_id, expanded, seen_group_ids_this_scan)
```

(The brand-new-link branch doesn't need `already_matched_ids.add(...)` for correctness — a freshly created link can't collide with itself in the same scan's remaining iterations unless a THIRD pdf_url also shares its name set, which is the exact case this fix also protects against if you add it defensively: add `already_matched_ids.add(link_id)` there too, for symmetry and to guard that third-link case.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_group_links_catalog.py -v`
Expected: PASS, and no existing "two distinct new links with same group name must stay distinct" test regresses (that test is phase-2-vs-empty-snapshot, unaffected by `already_matched_ids` since both are brand-new in the same scan and phase 2 is reached with an empty `already_matched_ids` for the first of them either way — verify by running the full file, not just the new test).

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_links.py tests/test_group_links_catalog.py
git commit -m "fix: exclude links already matched earlier in the same catalog scan from two-phase name-set matching, so a re-uploaded PDF with an identical group list can't silently steal another link's row"
```

---

### Task 13: Fix `build_schedule` day-label drift on an extra header row

**Files:**
- Modify: `schedule_bot/pdf_parser.py:183-239` (`build_schedule`)
- Test: `tests/test_pdf_parser_build_schedule.py`

**Interfaces:**
- Consumes: `RawRow`/`Cell` (unchanged dataclasses).
- Produces: `build_schedule`'s day-detection now matches `row.cells[0].text` against `DAY_ORDER` by prefix instead of blindly consuming `next(day_names)` on every `day_marker` row — callers (`parse_schedule`, `parse_schedules_for_groups`) are unaffected; both already pass `col_idx`/`table_x1` positionally and don't touch day-assignment internals.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pdf_parser_build_schedule.py — add near the other build_schedule tests
def test_build_schedule_ignores_a_sub_header_row_before_the_first_real_day():
    # Mirrors this file's existing RawRow-construction style: a day_marker
    # row whose cells[0] text is NOT an actual day name (a stray sub-header
    # like "Часы/1 подгр/2 подгр" that also happens to have a non-None
    # cells[0]) must NOT consume a slot from DAY_ORDER.
    rows = [
        RawRow(
            day_marker=True, time_label=None, y0=0.0, y1=5.0,
            cells=[Cell("Часы", 0, 10), Cell("1 подгр", 10, 20), Cell("2 подгр", 20, 30)],
        ),
        RawRow(
            day_marker=True, time_label="8.30-10.00", y0=5.0, y1=15.0,
            cells=[Cell("Понедельник", 0, 10), Cell("8.30-10.00", 10, 20), Cell("Лекция", 20, 30)],
        ),
        RawRow(
            day_marker=True, time_label="8.30-10.00", y0=15.0, y1=25.0,
            cells=[Cell("Вторник", 0, 10), Cell("8.30-10.00", 10, 20), Cell("Семинар", 20, 30)],
        ),
    ]
    result = build_schedule(rows, group_column_index=2, table_x1=30.0, time_column_index=1)
    assert "Понедельник" in result
    assert result["Понедельник"].lessons[0].upper == "Лекция"
    assert "Вторник" in result
    assert result["Вторник"].lessons[0].upper == "Семинар"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest "tests/test_pdf_parser_build_schedule.py::test_build_schedule_ignores_a_sub_header_row_before_the_first_real_day" -v`
Expected: FAIL — the sub-header row consumes `"Понедельник"` from the `day_names` iterator, so the row actually labeled "Понедельник" gets assigned `"Вторник"`, and the row labeled "Вторник" gets nothing (`StopIteration` → `current_day=None`, lesson dropped); `result["Понедельник"]` doesn't exist.

- [ ] **Step 3: Write minimal implementation**

Replace `build_schedule`'s day-tracking with a text match against `DAY_ORDER` instead of positional `next()`:

```python
def build_schedule(
    rows: list[RawRow],
    group_column_index: int,
    table_x1: float,
    time_column_index: int = 1,
    epsilon: float = 2.0,
) -> dict[str, DaySchedule]:
    current_day: str | None = None
    current_time: str | None = None
    accumulators: dict[str, _DayAccumulator] = {}

    for row in rows:
        if row.day_marker:
            day_cell = row.cells[0] if row.cells else None
            day_text = (day_cell.text or "").strip() if day_cell is not None else ""
            matched_day = next((day for day in DAY_ORDER if day_text.startswith(day)), None)
            if matched_day is not None:
                current_day = matched_day
                current_time = None
                if current_day not in accumulators:
                    accumulators[current_day] = _DayAccumulator()
            # A day_marker row whose cells[0] text doesn't match any known
            # day name (a stray sub-header, e.g. "Часы/1 подгр/2 подгр") is
            # NOT a day transition — fall through and keep accumulating
            # into whichever day is already current (or skip, if none yet).

        if current_day is None:
            continue

        accumulators[current_day].touch_bounds(row.y0, row.y1)

        if row.time_label is not None:
            current_time = row.time_label

        if current_time is None:
            continue

        time_cell = row.cells[time_column_index] if time_column_index < len(row.cells) else None
        if time_cell is not None:
            accumulators[current_day].touch_time_col_bounds(time_cell.x0, time_cell.x1)

        cell = row.cells[group_column_index] if group_column_index < len(row.cells) else None

        if cell is not None:
            text = cell.text or None
            accumulators[current_day].touch_group_col_bounds(cell.x0, cell.x1)
        else:
            text = None
            found = False
            for other in row.cells[:group_column_index]:
                if other is not None and other.text and other.x1 >= table_x1 - epsilon:
                    text = other.text
                    accumulators[current_day].touch_group_col_bounds(other.x0, other.x1)
                    found = True
                    break
            if not found:
                continue

        accumulators[current_day].add_value(current_time, text)

    return {day: acc.finalize() for day, acc in accumulators.items()}
```

This removes the `day_names = iter(DAY_ORDER)` line entirely (no longer needed) — delete it from the top of the function.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_pdf_parser_build_schedule.py tests/test_pdf_parser_integration.py -v`
Expected: PASS — including the real-fixture integration test and the existing synthetic test `test_stops_after_sixth_day_marker_ignoring_footer_row` (a trailing day_marker row whose text is neither empty nor a real day name, appearing AFTER Saturday, must still be ignored — verify this explicitly still passes since it's the test this fix must not regress).

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/pdf_parser.py tests/test_pdf_parser_build_schedule.py
git commit -m "fix: match a day_marker row's cells[0] text against DAY_ORDER by name instead of positional next(), so an extra sub-header row before the first real day no longer shifts every day's data and drops Saturday"
```

---

### Task 14: Preserve legacy `members` across the destructive schema rebuild, and don't clobber freshly-chosen groups during a delayed migration

**Files:**
- Modify: `schedule_bot/storage.py:7-30` (`_rebuild_groups_members_schedule_versions`, `init_db`)
- Modify: `schedule_bot/legacy_migration.py:94-103`
- Test: `tests/test_storage.py`, `tests/test_legacy_migration.py`

**Interfaces:**
- Produces: `storage.list_legacy_member_chat_ids_backup(conn) -> list[int]` — reads a new `legacy_members_backup` table populated once, at rebuild time, with every `chat_id` that was in `members` right before it got dropped. `legacy_migration.run_legacy_migration_if_configured` consumes this instead of `storage.list_all_member_chat_ids(conn)` when deciding which chat_ids to reassign.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage.py
def test_init_db_backs_up_member_chat_ids_before_dropping_legacy_schema(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy.db"
    old_conn = sqlite3.connect(str(db_path))
    old_conn.executescript(
        """
        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE members (chat_id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES groups(id));
        INSERT INTO groups (id, name) VALUES (1, 'ОлдГруппа');
        INSERT INTO members (chat_id, group_id) VALUES (111, 1), (222, 1);
        """
    )
    old_conn.commit()
    old_conn.close()

    new_conn = sqlite3.connect(str(db_path))
    storage.init_db(new_conn)

    backed_up = sorted(storage.list_legacy_member_chat_ids_backup(new_conn))
    assert backed_up == [111, 222]
    # The live members table is empty right after rebuild (expected — this
    # is the destructive part being guarded, not undone):
    assert storage.list_all_member_chat_ids(new_conn) == []
```

```python
# tests/test_legacy_migration.py
@pytest.mark.asyncio
async def test_legacy_migration_only_migrates_backed_up_chat_ids_not_already_resubscribed(conn, monkeypatch):
    """A chat_id that re-subscribed via /start (its own fresh members row
    exists) after the legacy wipe must NOT be silently reassigned onto the
    legacy target group — only genuinely still-unmigrated legacy users
    should move."""
    config = _make_config(conn)  # reuse this file's existing config-building helper
    # Seed the backup table as if init_db had just run against an old DB
    # with three legacy users:
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS legacy_members_backup (chat_id INTEGER PRIMARY KEY);"
        "INSERT INTO legacy_members_backup (chat_id) VALUES (111), (222), (333);"
    )
    conn.commit()

    institute_id = storage.create_institute(
        conn, name=config.legacy_migration_institute_name, catalog_url=config.legacy_migration_catalog_url,
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="/x.pdf",
        group_names=[config.legacy_migration_group_name], raw_label="x", last_seen_at="t",
    )
    group_id = storage.create_group(
        conn, institute_id=institute_id, institute_link_id=link_id, name=config.legacy_migration_group_name,
    )
    storage.update_group_is_verified(conn, group_id, 1)

    # 222 already re-subscribed to some OTHER group after the wipe:
    other_group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="ДругаяГруппа")
    storage.upsert_member(conn, 222, other_group_id, joined_at="t")

    notify_admin = AsyncMock()
    sync_institute_link_fn = AsyncMock()
    reschedule_institute_jobs = MagicMock()

    monkeypatch.setattr(
        "schedule_bot.legacy_migration.refresh_institute_catalog", lambda conn_, institute, notify_admin: None
    )
    monkeypatch.setattr(
        "schedule_bot.legacy_migration.validate_and_sync_group_on_first_subscribe",
        AsyncMock(return_value=True),
    )

    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_institute_link_fn, reschedule_institute_jobs)

    assert storage.get_member(conn, 111).group_id == group_id
    assert storage.get_member(conn, 333).group_id == group_id
    assert storage.get_member(conn, 222).group_id == other_group_id  # untouched
```

(Adjust the exact mocking mechanics — `_make_config`, module path for `refresh_institute_catalog`/`validate_and_sync_group_on_first_subscribe` — to match whatever helper/monkeypatch style `tests/test_legacy_migration.py` already uses; the file already has passing tests exercising this same function, so copy its established setup rather than inventing a new one.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_storage.py::test_init_db_backs_up_member_chat_ids_before_dropping_legacy_schema tests/test_legacy_migration.py::test_legacy_migration_only_migrates_backed_up_chat_ids_not_already_resubscribed -v`
Expected: FAIL — `ImportError`/`AttributeError: list_legacy_member_chat_ids_backup` on the first test, and on the second, `storage.get_member(conn, 111)` is `None` (nothing to migrate since `list_all_member_chat_ids` is empty right after the wipe, in the real-world ordering) — or, as currently written with `reassign_member_group`, the migration silently no-ops on `111`/`333` because their `members` rows don't exist to UPDATE.

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/storage.py`:

```python
def _backup_legacy_member_chat_ids(conn: sqlite3.Connection) -> None:
    info = conn.execute("PRAGMA table_info(members)").fetchall()
    if not info:
        return  # no members table existed yet in the old schema — nothing to back up
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS legacy_members_backup (chat_id INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO legacy_members_backup (chat_id) SELECT chat_id FROM members;
        """
    )
    conn.commit()


def _rebuild_groups_members_schedule_versions(conn: sqlite3.Connection) -> None:
    # FK-safe drop order: schedule_versions/members reference groups(id).
    # Every existing members.chat_id is preserved in legacy_members_backup
    # BEFORE the drop, specifically so run_legacy_migration_if_configured
    # (which otherwise runs much later, inside post_init, long after this
    # has already executed) has something to migrate from — without this,
    # every subscriber from a pre-institutes-schema database is silently
    # and permanently lost the moment this rebuild runs.
    _backup_legacy_member_chat_ids(conn)
    conn.executescript(
        """
        DROP TABLE IF EXISTS schedule_versions;
        DROP TABLE IF EXISTS members;
        DROP TABLE IF EXISTS groups;
        """
    )
    conn.commit()


def list_legacy_member_chat_ids_backup(conn: sqlite3.Connection) -> list[int]:
    info = conn.execute("PRAGMA table_info(legacy_members_backup)").fetchall()
    if not info:
        return []
    rows = conn.execute("SELECT chat_id FROM legacy_members_backup").fetchall()
    return [row[0] for row in rows]
```

In `schedule_bot/legacy_migration.py`, replace the reassignment block:

```python
    backed_up_chat_ids = set(storage.list_legacy_member_chat_ids_backup(conn))
    already_subscribed_chat_ids = set(storage.list_all_member_chat_ids(conn))
    chat_ids = backed_up_chat_ids - already_subscribed_chat_ids
    now = datetime.now(timezone.utc).isoformat()
    for chat_id in chat_ids:
        storage.upsert_member(conn, chat_id, group.id, joined_at=now, subscribed=True)
```

(replacing the previous `chat_ids = storage.list_all_member_chat_ids(conn)` + `storage.reassign_member_group(...)` loop — `upsert_member` is required instead of `reassign_member_group` because these chat_ids have no `members` row at all after the wipe; `reassign_member_group`'s `UPDATE ... WHERE chat_id = ?` would silently affect zero rows for every one of them.) Update the final admin notification's count to use `len(chat_ids)` (already does, just now reflects the correct set).

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_storage.py tests/test_legacy_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py schedule_bot/legacy_migration.py tests/test_storage.py tests/test_legacy_migration.py
git commit -m "fix: back up members.chat_id before the destructive institute-schema rebuild drops the table, and have legacy migration upsert from that backup (skipping anyone who already re-subscribed) instead of silently losing every pre-upgrade subscriber"
```

---

### Task 15: Make election approval atomic — no double-apply on a simultaneous second vote

**Files:**
- Modify: `schedule_bot/storage.py` (new `close_election_if_open`)
- Modify: `schedule_bot/starosta_commands.py:179-205` (`election_approve_callback`)
- Test: `tests/test_storage.py`, `tests/test_starosta_commands.py`

**Interfaces:**
- Produces: `storage.close_election_if_open(conn, election_id, status, decided_at) -> bool` — `True` if this call actually transitioned the row from `open`, `False` if it was already closed by someone else. `starosta_commands._apply_election_result` no longer calls `storage.close_election` directly; `election_approve_callback` calls `close_election_if_open` itself and only proceeds to apply effects (welcome message, `update_group_starosta`, etc.) on `True`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage.py
def test_close_election_if_open_only_succeeds_once(conn):
    institute_id = storage.create_institute(
        conn, name="X", catalog_url="y", check_interval_minutes=60, send_time="09:00",
        tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="z", group_names=["G"], raw_label="G", last_seen_at="t")
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="G")
    election_id = storage.create_election(conn, group_id, "initial", 1, 1, "t")

    first = storage.close_election_if_open(conn, election_id, "approved", "t2")
    second = storage.close_election_if_open(conn, election_id, "approved", "t3")

    assert first is True
    assert second is False
    assert storage.get_election(conn, election_id).decided_at == "t2"
```

```python
# tests/test_starosta_commands.py
@pytest.mark.asyncio
async def test_two_simultaneous_approvals_only_apply_the_election_result_once(conn):
    # Build a group with 3 members and an open "initial" election for one
    # candidate, matching this file's existing election-test setup helpers.
    ...
    context = _build_context(conn)  # reuse existing helper from this file
    update_a = _build_update(chat_id=members[1])
    update_b = _build_update(chat_id=members[2])

    storage.add_election_approval(conn, election_id, members[1], "t")
    # Simulate both callbacks reaching count>=2 before either closes the row:
    results = await asyncio.gather(
        election_approve_callback(update_a, context),
        election_approve_callback(update_b, context),
    )

    group = storage.get_group(conn, group_id)
    assert group.starosta_chat_id == candidate_chat_id
    # Only ONE welcome message to the candidate, not two:
    welcome_sends = [c for c in context.bot.send_message.await_args_list if c.kwargs.get("chat_id") == candidate_chat_id]
    assert len(welcome_sends) == 1
```

(Match this file's existing fixtures/builders for `context`/`update` exactly — `tests/test_starosta_commands.py` already has election tests to copy from.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_storage.py::test_close_election_if_open_only_succeeds_once -v`
Expected: FAIL — `AttributeError: module 'storage' has no attribute 'close_election_if_open'`.

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/storage.py`, add next to `close_election`:

```python
def close_election_if_open(conn: sqlite3.Connection, election_id: int, status: str, decided_at: str) -> bool:
    """Atomic version of close_election: only transitions the row if it
    is still 'open' at the moment of this UPDATE, and reports whether
    THIS call was the one that closed it. Used to guard against two
    concurrent approvals both reaching the "enough votes" threshold and
    both trying to apply the election result."""
    cursor = conn.execute(
        "UPDATE elections SET status = ?, decided_at = ? WHERE id = ? AND status = 'open'",
        (status, decided_at, election_id),
    )
    conn.commit()
    return cursor.rowcount > 0
```

In `schedule_bot/starosta_commands.py`, split `_apply_election_result` so the atomic close happens first and gates everything else:

```python
async def _apply_election_result(bot, conn, election: "storage.ElectionRow") -> bool:
    now = datetime.now(timezone.utc).isoformat()
    if not storage.close_election_if_open(conn, election.id, "approved", now):
        return False  # another concurrent approval already applied this result

    group = storage.get_group(conn, election.group_id)
    old_starosta_chat_id = group.starosta_chat_id
    storage.update_group_starosta(conn, group.id, election.candidate_chat_id)
    storage.set_group_last_starosta_change_at(conn, group.id, now)

    welcome = elections.format_starosta_welcome(group.name, "election")
    try:
        await send_with_retry(lambda: bot.send_message(chat_id=election.candidate_chat_id, text=welcome))
    except Exception as error:
        logger.warning("starosta_commands: failed to send welcome to chat_id=%s: %s", election.candidate_chat_id, error)

    if election.kind == "change" and old_starosta_chat_id and old_starosta_chat_id != election.candidate_chat_id:
        try:
            await send_with_retry(
                lambda: bot.send_message(
                    chat_id=old_starosta_chat_id, text=f"Старосту группы «{group.name}» сменили."
                )
            )
        except Exception as error:
            logger.warning(
                "starosta_commands: failed to notify old starosta chat_id=%s: %s", old_starosta_chat_id, error
            )
    return True
```

And in `election_approve_callback`:

```python
    storage.add_election_approval(conn, election_id, chat_id, datetime.now(timezone.utc).isoformat())
    count = storage.count_election_approvals(conn, election_id)
    if count >= 2:
        applied = await _apply_election_result(context.bot, conn, election)
        if not applied:
            await query.message.reply_text("Это голосование уже было применено другим одобрением.")
    else:
        await query.message.reply_text(f"Голос засчитан ({count}/2).")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_storage.py tests/test_starosta_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py schedule_bot/starosta_commands.py tests/test_storage.py tests/test_starosta_commands.py
git commit -m "fix: make election approval atomic (UPDATE ... WHERE status='open') so two simultaneous approving votes can't both apply the result and double-send the welcome/old-starosta notifications"
```

---

### Task 16: `format_candidate_label(None)` safe, and guard every `None`-member pagination/callback path

**Files:**
- Modify: `schedule_bot/elections.py:22-29` (`format_candidate_label`)
- Modify: `schedule_bot/change_group_commands.py:84-85` (`change_group_page_callback`)
- Modify: `schedule_bot/starosta_candidates.py:46-56` (`page_callback`, `select_callback`)
- Modify: `schedule_bot/starosta_commands.py:225-226` (`votes_command`'s per-election loop)
- Test: `tests/test_elections.py`, `tests/test_change_group_commands.py`, `tests/test_starosta_candidates.py`, `tests/test_starosta_commands.py`

**Interfaces:**
- Produces: `elections.format_candidate_label(member) -> str` now accepts `member=None` and returns `"Неизвестный"` directly, so every caller that was doing `format_candidate_label(x) if x is not None else "Неизвестный"` can keep doing that (harmless now-redundant check) or be simplified — this task simplifies the ones that currently *don't* guard at all and would crash.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_elections.py
def test_format_candidate_label_handles_none():
    from schedule_bot.elections import format_candidate_label
    assert format_candidate_label(None) == "Неизвестный"
```

```python
# tests/test_change_group_commands.py
@pytest.mark.asyncio
async def test_change_group_page_callback_tolerates_excluded_chat(conn):
    # chat_id has no `members` row (e.g. was just excluded from their
    # group) but still taps "Вперёд »" on a change-group picker page they
    # opened before being excluded.
    context = _build_context(conn)  # reuse this file's existing helper
    update = _build_callback_update(chat_id=99999, data="chggrp:page:1")  # 99999 has no member row
    await change_group_page_callback(update, context)
    update.callback_query.message.reply_text.assert_awaited()  # graceful message, not a crash
```

```python
# tests/test_starosta_candidates.py
@pytest.mark.asyncio
async def test_page_callback_tolerates_excluded_chat(conn):
    context = _build_context(conn)
    update = _build_callback_update(chat_id=99999, data="starcand:page:1")
    await page_callback(update, context)  # must not raise
    update.callback_query.message.reply_text.assert_awaited()
```

(Match each file's existing context/update builder helpers.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_elections.py::test_format_candidate_label_handles_none -v`
Expected: FAIL — `AttributeError: 'NoneType' object has no attribute 'display_name'`.

- [ ] **Step 3: Write minimal implementation**

In `schedule_bot/elections.py`:

```python
def format_candidate_label(member) -> str:
    if member is None:
        return "Неизвестный"
    if member.display_name and member.telegram_username:
        return f"{member.display_name} (@{member.telegram_username})"
    if member.display_name:
        return member.display_name
    if member.telegram_username:
        return f"@{member.telegram_username}"
    return "Неизвестный"
```

In `schedule_bot/change_group_commands.py`, `change_group_page_callback`:

```python
async def change_group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await query.message.reply_text("Вы больше не в какой-либо группе — используйте /start.")
        return
    await safe_edit_reply_markup(query, reply_markup=build_change_group_keyboard(conn, member.group_id, page))
```

In `schedule_bot/starosta_candidates.py`, `page_callback`:

```python
async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await query.message.reply_text("Вы больше не в какой-либо группе — используйте /start.")
        return
    await safe_edit_reply_markup(query, reply_markup=build_candidate_keyboard(conn, member.group_id, page))
```

(`select_callback` in the same file already tolerates `candidate is None` once `format_candidate_label` is `None`-safe from this task's first change — no further edit needed there beyond the shared helper fix.)

In `schedule_bot/starosta_commands.py`, `votes_command`'s loop already calls `storage.get_member(conn, election.candidate_chat_id)` and passes the possibly-`None` result straight to `elections.format_candidate_label(candidate)` — this is now safe automatically once `format_candidate_label` accepts `None`; no code change needed there beyond Step 3's `elections.py` edit, only a new regression test:

```python
# tests/test_starosta_commands.py
@pytest.mark.asyncio
async def test_votes_command_tolerates_excluded_candidate(conn):
    # Build a group + open election whose candidate_chat_id was since
    # excluded (storage.remove_member called on them, members row gone).
    ...
    await votes_command(update, context)  # must not raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_elections.py tests/test_change_group_commands.py tests/test_starosta_candidates.py tests/test_starosta_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/elections.py schedule_bot/change_group_commands.py schedule_bot/starosta_candidates.py tests/test_elections.py tests/test_change_group_commands.py tests/test_starosta_candidates.py tests/test_starosta_commands.py
git commit -m "fix: make format_candidate_label tolerate a None member, and guard the change-group/starosta-candidate pagination callbacks against a chat that was excluded between opening the picker and tapping a page button"
```

---

### Task 17: Robust integer parsing for every admin-input path (`isdigit()` rejects valid/unicode input oddly and throws on superscript digits)

**Files:**
- Modify: `schedule_bot/bot.py:218-221, 283-286` (`_resolve_admin_group`, `_apply_set_starosta`)
- Modify: `schedule_bot/institute_commands.py:15-18` (`_resolve_admin_institute`)
- Modify: `schedule_bot/menu.py:530, 538` (`set_institute_check_interval`/`set_institute_catalog_interval` branches in `pending_action_followup`)
- Test: `tests/test_bot_admin_commands.py`, `tests/test_institute_commands.py`, `tests/test_menu.py`

**Interfaces:**
- Produces: a shared helper `schedule_bot.telegram_utils.parse_positive_int(value: str) -> int | None` (returns `None` on anything that isn't a plain base-10 positive integer, including the Unicode digit-like characters `str.isdigit()` accepts but `int()` rejects). Every call site below replaces its own `value.isdigit()` + `int(value)` pair with one call to this helper.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_telegram_utils.py
from schedule_bot.telegram_utils import parse_positive_int


def test_parse_positive_int_rejects_unicode_digit_lookalikes():
    assert parse_positive_int("²") is None  # str.isdigit() is True, int() raises ValueError
    assert parse_positive_int("-5") is None
    assert parse_positive_int("0") is None
    assert parse_positive_int("") is None
    assert parse_positive_int("42") == 42
```

```python
# tests/test_bot_admin_commands.py
@pytest.mark.asyncio
async def test_refresh_command_rejects_unicode_digit_lookalike_group_id(conn):
    context = _build_admin_context(conn)
    update = _build_message_update(text="/refresh ²", args=["²"])
    await refresh_command(update, context)
    update.message.reply_text.assert_awaited_with("Группа с таким id не найдена.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_telegram_utils.py::test_parse_positive_int_rejects_unicode_digit_lookalikes -v`
Expected: FAIL — `ImportError: cannot import name 'parse_positive_int'`.

- [ ] **Step 3: Write minimal implementation**

Add to `schedule_bot/telegram_utils.py`:

```python
def parse_positive_int(value: str) -> int | None:
    """Stricter than str.isdigit(): some Unicode characters (superscript
    digits, fullwidth digits, etc.) satisfy isdigit() but make int(value)
    raise ValueError. isdecimal() narrows that to ASCII-compatible decimal
    digit characters, which int() always accepts; this additionally
    rejects non-positive values so every admin-id/interval parsing call
    site gets one consistent, exception-free check."""
    if not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None
```

In `schedule_bot/bot.py`:

```python
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, parse_positive_int, reply, send_with_retry

...

def _resolve_admin_group(conn, group_id_str: str):
    group_id = parse_positive_int(group_id_str)
    if group_id is None:
        return None
    return storage.get_group(conn, group_id)
```

```python
async def _apply_set_starosta(conn, bot_client, group, chat_id_str: str) -> str:
    new_starosta_chat_id = parse_positive_int(chat_id_str)
    if new_starosta_chat_id is None:
        return "chat_id должен быть положительным целым числом."
    now = datetime.now(timezone.utc).isoformat()
    ...  # unchanged, using new_starosta_chat_id
```

In `schedule_bot/institute_commands.py`:

```python
from schedule_bot.telegram_utils import parse_positive_int

def _resolve_admin_institute(conn, institute_id_str: str):
    institute_id = parse_positive_int(institute_id_str)
    if institute_id is None:
        return None
    return storage.get_institute(conn, institute_id)
```

In `schedule_bot/menu.py`'s `pending_action_followup`, the two interval branches:

```python
        elif action == "set_institute_check_interval":
            minutes = parse_positive_int(value)
            if minutes is None:
                await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
                return
            storage.update_institute_check_interval(conn, institute.id, minutes)
            for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
                context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
            await update.message.reply_text(f"Интервал проверки института «{institute.name}» обновлён: {minutes} мин.")
        elif action == "set_institute_catalog_interval":
            minutes = parse_positive_int(value)
            if minutes is None:
                await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
                return
            storage.update_institute_catalog_refresh_interval(conn, institute.id, minutes)
            context.bot_data["reschedule_institute_jobs"](institute.id)
            await update.message.reply_text(
                f"Интервал сканирования каталога института «{institute.name}» обновлён: {minutes} мин."
            )
```

(add `from schedule_bot.telegram_utils import parse_positive_int` to `menu.py`'s imports.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_telegram_utils.py tests/test_bot_admin_commands.py tests/test_institute_commands.py tests/test_menu.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/telegram_utils.py schedule_bot/bot.py schedule_bot/institute_commands.py schedule_bot/menu.py tests/test_telegram_utils.py tests/test_bot_admin_commands.py tests/test_institute_commands.py tests/test_menu.py
git commit -m "fix: replace str.isdigit()+int() with a strict parse_positive_int() helper across every admin id/interval input path, since isdigit() accepts Unicode digit lookalikes that make int() raise"
```

---

### Task 18: `/add_institute` must not treat a multi-word institute name as a single token

**Files:**
- Modify: `schedule_bot/institute_commands.py:58-75` (`add_institute_command`)
- Test: `tests/test_institute_commands.py`

**Interfaces:**
- Consumes: `create_institute_and_scan(conn, config, name, catalog_url, notify_admin)` (unchanged signature from Task 7's edit, which only added an optional `lock` parameter at the end).
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_institute_commands.py
@pytest.mark.asyncio
async def test_add_institute_command_accepts_multi_word_name(conn, monkeypatch):
    monkeypatch.setattr(
        "schedule_bot.institute_commands.refresh_institute_catalog", lambda conn_, institute, notify_admin: None
    )
    context = _build_admin_context(conn)  # reuse this file's existing helper
    update = _build_message_update(
        args=["Аграрный", "институт", "https://example.com/catalog"]
    )
    await add_institute_command(update, context)

    institutes = storage.list_institutes(conn)
    assert len(institutes) == 1
    assert institutes[0].name == "Аграрный институт"
    assert institutes[0].catalog_url == "https://example.com/catalog"


@pytest.mark.asyncio
async def test_add_institute_command_rejects_url_not_starting_with_http(conn):
    context = _build_admin_context(conn)
    update = _build_message_update(args=["Институт", "not-a-url"])
    await add_institute_command(update, context)
    assert storage.list_institutes(conn) == []
    update.message.reply_text.assert_awaited_with(
        "Использование: /add_institute <название> <url_каталога> — url должен начинаться с http:// или https://"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_institute_commands.py::test_add_institute_command_accepts_multi_word_name tests/test_institute_commands.py::test_add_institute_command_rejects_url_not_starting_with_http -v`
Expected: FAIL — the first test creates an institute named `"Аграрный"` with `catalog_url="институт"` (the current `context.args[0]`/`context.args[1]` split); the second test currently has no URL-shape validation at all and would create an institute named `"Институт"` with `catalog_url="not-a-url"`.

- [ ] **Step 3: Write minimal implementation**

```python
async def add_institute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /add_institute from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /add_institute <название> <url_каталога>")
        return
    catalog_url = context.args[-1]
    name = " ".join(context.args[:-1])
    if not catalog_url.startswith(("http://", "https://")):
        await update.message.reply_text(
            "Использование: /add_institute <название> <url_каталога> — url должен начинаться с http:// или https://"
        )
        return
    conn = context.bot_data["conn"]
    config = context.bot_data["config"]
    alerts: list[str] = []
    institute_id, scan_status = await create_institute_and_scan(conn, config, name, catalog_url, alerts.append)
    context.bot_data["reschedule_institute_jobs"](institute_id)
    for text in alerts:
        await update.message.reply_text(text)
    await update.message.reply_text(f"Институт «{name}» создан (id={institute_id}), {scan_status}.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_institute_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/institute_commands.py tests/test_institute_commands.py
git commit -m "fix: /add_institute now takes the URL as the last argument and joins everything before it as the (possibly multi-word) institute name, and rejects a catalog_url that isn't http(s)"
```

---

## Final verification

- [ ] **Step 1: Run the entire suite once, end to end**

Run: `./.venv/bin/python -m pytest -v`
Expected: every test passes, including every pre-existing test untouched by this plan.

- [ ] **Step 2: Grep for any remaining direct `edit_message_text`/`edit_reply_markup` call that bypassed Task 3's helper**

Run: `grep -rn "\.edit_message_text(\|\.edit_reply_markup(\|\.edit_text(" schedule_bot/ | grep -v telegram_utils.py`
Expected: no output (every call site now goes through `safe_edit_message_text`/`safe_edit_reply_markup`), or any remaining hit is reviewed and consciously left alone with a comment explaining why (e.g. a one-off call whose text is guaranteed to always differ).

- [ ] **Step 3: Grep for any remaining `.isdigit()` admin-input check this plan should have replaced**

Run: `grep -rn "\.isdigit()" schedule_bot/`
Expected: no output.

## Out of scope for this plan (tracked, not fixed here — lower severity / larger redesign)

- Message-length/keyboard-row-count limits for `/classmates` and admin "all requests" views on groups with 100+ members (Telegram's 4096-char / keyboard-size ceilings) — needs pagination or chunking, a larger UI change than a hardening pass.
- `curl_cffi.Session` never explicitly closed in `browser_client.py` (slow fd leak, bounded by GC).
- Docker healthcheck only checks `schedule.db` exists, not that the event loop/scheduler are alive — needs a heartbeat mechanism.
- Per-institute/global rate limiting of outbound Telegram sends across *multiple simultaneous* daily-send jobs (Task 5/9/10 fix correctness and isolation, not aggregate throughput across institutes) — needs either `AIORateLimiter` or a global semaphore, a larger behavioral change best done as its own reviewed piece of work.
- IntervalTrigger `jitter` for per-institute link jobs (avoiding every link of one institute polling the university site in the same second) — cosmetic/politeness improvement, not a correctness fix.
