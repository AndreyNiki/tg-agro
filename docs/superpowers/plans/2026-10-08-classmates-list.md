# Список одногруппников — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single read-only `/classmates` command listing every member attached to the caller's group, formatted as "name (@username)"/"name"/"@username"/"Неизвестный", with the group's starosta marked by a 👑 and a one-line legend.

**Architecture:** One new small module (`schedule_bot/classmates.py`) holding a pure formatting function and the command handler, reusing `elections.format_candidate_label` (sub-project 2) for the name/username priority logic instead of duplicating it.

**Tech Stack:** Same as prior sub-projects.

**Spec:** `docs/superpowers/specs/2026-10-08-classmates-list-design.md`

**Depends on:** sub-project 1 (`storage.get_member`/`get_group`/`list_group_members`), sub-project 2 (`groups.starosta_chat_id`, `elections.format_candidate_label`).

## Global Constraints

- Shows **every** member attached to the group (`subscribed_only=False`), not just active subscribers.
- Sorted alphabetically by the final displayed line, not by `joined_at` or `chat_id`.
- The legend line ("👑 — староста группы") only appears when the group actually has a starosta.

## Review Focus

- A member with neither `display_name` nor `telegram_username` must render as "Неизвестный", not an empty string or crash.
- A group with no starosta yet must show no crown anywhere and no legend line.
- A caller with no attached group must get the `/start` hint, not a crash from `None.group_id`.

---

## Task 1: `format_member_line` — pure formatting

**Files:**
- Create: `schedule_bot/classmates.py`
- Test: `tests/test_classmates.py`

**Interfaces:**
- Consumes: `elections.format_candidate_label` (sub-project 2 Task 4), `storage.MemberRow`.
- Produces: `classmates.format_member_line(member, is_starosta: bool) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_classmates.py`:

```python
from schedule_bot.classmates import format_member_line
from schedule_bot.storage import MemberRow


def _member(**overrides):
    defaults = dict(
        chat_id=111, group_id=1, telegram_username=None, display_name=None, subscribed=True, joined_at="t"
    )
    defaults.update(overrides)
    return MemberRow(**defaults)


def test_format_member_line_with_both_fields():
    member = _member(display_name="Ivan Ivanov", telegram_username="ivan")
    assert format_member_line(member, is_starosta=False) == "Ivan Ivanov (@ivan)"


def test_format_member_line_with_only_display_name():
    member = _member(display_name="Ivan Ivanov")
    assert format_member_line(member, is_starosta=False) == "Ivan Ivanov"


def test_format_member_line_with_only_username():
    member = _member(telegram_username="ivan")
    assert format_member_line(member, is_starosta=False) == "@ivan"


def test_format_member_line_with_neither():
    member = _member()
    assert format_member_line(member, is_starosta=False) == "Неизвестный"


def test_format_member_line_marks_starosta():
    member = _member(display_name="Ivan Ivanov")
    assert format_member_line(member, is_starosta=True) == "👑 Ivan Ivanov"


def test_format_member_line_no_mark_when_not_starosta():
    member = _member(display_name="Ivan Ivanov")
    line = format_member_line(member, is_starosta=False)
    assert "👑" not in line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_classmates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.classmates'`

- [ ] **Step 3: Implement**

Create `schedule_bot/classmates.py`:

```python
from schedule_bot.elections import format_candidate_label


def format_member_line(member, is_starosta: bool) -> str:
    label = format_candidate_label(member)
    return f"👑 {label}" if is_starosta else label
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_classmates.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/classmates.py tests/test_classmates.py
git commit -m "feat: add format_member_line for the classmates list"
```

---

## Task 2: `/classmates` command

**Files:**
- Modify: `schedule_bot/classmates.py`
- Test: `tests/test_classmates.py`

**Interfaces:**
- Consumes: `storage.get_member`/`get_group`/`list_group_members` (sub-project 1), `format_member_line` (Task 1).
- Produces: `classmates.classmates_command`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_classmates.py`:

```python
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.classmates import classmates_command


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def make_update(chat_id):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    return context


@pytest.mark.asyncio
async def test_classmates_command_without_group_prompts_start(conn):
    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    assert "/start" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_classmates_command_lists_sorted_members_with_starosta_mark_and_legend(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.update_member_profile(conn, 222, telegram_username="anna", display_name=None)
    storage.update_group_starosta(conn, group_id, 111)

    update = make_update(222)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    lines = text.split("\n")
    assert lines[0] == "@anna"
    assert lines[1] == "👑 @boris"
    assert "👑 — староста группы" in text


@pytest.mark.asyncio
async def test_classmates_command_no_legend_when_no_starosta(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)

    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "👑" not in text


@pytest.mark.asyncio
async def test_classmates_command_includes_unsubscribed_members(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=True)
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)
    storage.upsert_member(conn, 222, group_id, joined_at="t", subscribed=False)
    storage.update_member_profile(conn, 222, telegram_username="anna", display_name=None)

    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "@anna" in text
    assert "@boris" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_classmates.py -v -k command`
Expected: FAIL with `ImportError: cannot import name 'classmates_command'`

- [ ] **Step 3: Implement**

Append to `schedule_bot/classmates.py` (add `from schedule_bot import storage` to the imports at top, alongside the existing `elections` import):

```python
async def classmates_command(update, context) -> None:
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return

    group = storage.get_group(conn, member.group_id)
    chat_ids = storage.list_group_members(conn, group.id, subscribed_only=False)
    members = [storage.get_member(conn, chat_id) for chat_id in chat_ids]
    lines = sorted(format_member_line(m, m.chat_id == group.starosta_chat_id) for m in members)

    text = "\n".join(lines)
    if group.starosta_chat_id is not None:
        text += "\n\n👑 — староста группы"
    await update.message.reply_text(text)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_classmates.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/classmates.py tests/test_classmates.py
git commit -m "feat: /classmates command"
```

---

## Task 3: `main.py` wiring

**Files:**
- Modify: `main.py`
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: `classmates.classmates_command` (Task 2).
- Produces: `main.build_application` registers `/classmates`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main_wiring.py`:

```python
def test_build_application_registers_classmates_command(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)
    assert "classmates" in command_names

    application.bot_data["_scheduler"].shutdown(wait=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v -k classmates`
Expected: FAIL — `"classmates" not in command_names`.

- [ ] **Step 3: Implement**

In `main.py`, add `from schedule_bot.classmates import classmates_command` to the imports, and add to `build_application`'s handler registration block (next to the other `CommandHandler` registrations):

```python
    application.add_handler(CommandHandler("classmates", classmates_command))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: PASS (full file)

Then run the full suite:

Run: `./.venv/bin/python -m pytest -v`
Expected: PASS (every test in the repository, across sub-projects 1, 2 and 3)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "feat: wire /classmates into main.py"
```
