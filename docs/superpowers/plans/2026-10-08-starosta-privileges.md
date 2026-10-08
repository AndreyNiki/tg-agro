# Привилегии старосты — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add member exclusion, closed/open groups with approval-gated joining, `/notify` (starosta own-group / admin with `group_id`), and `/requests` (pending `group_change_requests` for starosta's own group / admin with `group_id`).

**Architecture:** A new shared module `schedule_bot/join_requests.py` holds the "does this join need approval?" rule and the notify/create-request plumbing, reused by both `/start`'s group picker (sub-project 1, modified here to route closed/excluded joins through approval instead of instant-attaching) and `/change_group`'s existing always-approval flow (sub-project 2, refactored here to call the same shared notify function instead of its own copy). Exclusion adds a `group_exclusions` table and a button on `/classmates` (sub-project 3). `/close_group`/`/open_group`/`/notify` join the admin commands in `bot.py`; `/requests` joins `change_group_commands.py`.

**Tech Stack:** Same as prior sub-projects.

**Spec:** `docs/superpowers/specs/2026-10-08-starosta-privileges-design.md`

**Depends on:** sub-project 1 (`groups`/`members`, `group_commands.group_select_callback`), sub-project 2 (`groups.starosta_chat_id`, `group_change_requests`, `change_group_commands.py`, `elections.format_candidate_label`), sub-project 3 (`classmates.py`, `format_member_line`).

## Global Constraints

- Self-exclusion is rejected **in the handler**, independent of whether the "Исключить" button happens to be shown for that row — the server-side check (`target_chat_id == caller_chat_id`) is the real guard, not UI omission.
- A join requires approval if the target group `is_closed` **or** a `group_exclusions` row exists for `(group_id, chat_id)` — either condition alone is sufficient, and the exclusion check applies even to an otherwise-open group.
- `group_change_requests.from_group_id` is nullable from this sub-project on — existing rows (sub-project 2, always non-null) must survive the migration unchanged.
- Every admin command that also has a starosta no-arg form (`/close_group`, `/open_group`, `/notify`, `/requests`) checks `is_admin()` **first**: if the caller is admin, a `group_id` argument is mandatory; otherwise it resolves the caller's own group via `starosta_chat_id`. This ordering removes any ambiguity about what a bare numeric first argument means.

## Review Focus

- Excluding someone must delete their `members` row (not just flag it) so `/start` immediately re-offers the group picker to them.
- Re-selecting the same group after exclusion must go through `group_change_requests`, not re-attach instantly, even though the group itself may otherwise be open.
- Attempting to exclude yourself via a directly crafted `exclude:confirm:<own_chat_id>` callback (bypassing the UI's self-omission) must still be rejected.
- `/notify`/`/close_group`/`/open_group`/`/requests` called by a plain member (not starosta, not admin) must be rejected, not silently resolve to someone else's group.
- The `group_change_requests` table rebuild (nullable `from_group_id`) must preserve existing rows' data exactly, including already-decided ones.

---

## Task 1: `group_exclusions` table and CRUD

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `storage.create_group_exclusion(conn, group_id, chat_id, excluded_at, excluded_by_chat_id) -> None`, `storage.has_group_exclusion(conn, group_id, chat_id) -> bool`, `storage.remove_member(conn, chat_id) -> None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def test_has_group_exclusion_false_when_none_recorded(conn):
    group_id = _make_group(conn)
    assert storage.has_group_exclusion(conn, group_id, 111) is False


def test_create_group_exclusion_then_has_exclusion_true(conn):
    group_id = _make_group(conn)
    storage.create_group_exclusion(conn, group_id, 111, "t", 999)
    assert storage.has_group_exclusion(conn, group_id, 111) is True


def test_group_exclusion_is_scoped_by_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_exclusion(conn, group_a, 111, "t", 999)
    assert storage.has_group_exclusion(conn, group_b, 111) is False


def test_remove_member_deletes_the_row(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.remove_member(conn, 111)
    assert storage.get_member(conn, 111) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k exclusion`
Expected: FAIL with `AttributeError: module 'schedule_bot.storage' has no attribute 'create_group_exclusion'`

- [ ] **Step 3: Implement**

Add to `init_db`'s `executescript`:

```python
        CREATE TABLE IF NOT EXISTS group_exclusions (
            group_id INTEGER NOT NULL REFERENCES groups(id),
            chat_id INTEGER NOT NULL,
            excluded_at TEXT NOT NULL,
            excluded_by_chat_id INTEGER NOT NULL
        );
```

Append to `storage.py`:

```python
def create_group_exclusion(
    conn: sqlite3.Connection, group_id: int, chat_id: int, excluded_at: str, excluded_by_chat_id: int
) -> None:
    conn.execute(
        "INSERT INTO group_exclusions (group_id, chat_id, excluded_at, excluded_by_chat_id) VALUES (?, ?, ?, ?)",
        (group_id, chat_id, excluded_at, excluded_by_chat_id),
    )
    conn.commit()


def has_group_exclusion(conn: sqlite3.Connection, group_id: int, chat_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM group_exclusions WHERE group_id = ? AND chat_id = ? LIMIT 1", (group_id, chat_id)
    ).fetchone()
    return row is not None


def remove_member(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM members WHERE chat_id = ?", (chat_id,))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k exclusion`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add group_exclusions table and remove_member"
```

---

## Task 2: `groups.is_closed` + `get_group_where_starosta`

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `GroupRow` gains `is_closed: bool` (appended field); `storage.update_group_closed(conn, group_id, is_closed: bool) -> None`; `storage.get_group_where_starosta(conn, chat_id) -> GroupRow | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def test_create_group_defaults_to_open(conn):
    group_id = _make_group(conn)
    assert storage.get_group(conn, group_id).is_closed is False


def test_update_group_closed_toggles(conn):
    group_id = _make_group(conn)
    storage.update_group_closed(conn, group_id, True)
    assert storage.get_group(conn, group_id).is_closed is True
    storage.update_group_closed(conn, group_id, False)
    assert storage.get_group(conn, group_id).is_closed is False


def test_get_group_where_starosta_returns_none_when_nobody_is(conn):
    _make_group(conn)
    assert storage.get_group_where_starosta(conn, 111) is None


def test_get_group_where_starosta_finds_the_right_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.update_group_starosta(conn, group_b, 111)
    found = storage.get_group_where_starosta(conn, 111)
    assert found.id == group_b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k "is_closed or where_starosta"`
Expected: FAIL — `GroupRow` has no `is_closed` field yet.

- [ ] **Step 3: Implement**

In `init_db`, add alongside the other `_ensure_column` calls from sub-project 2's Task 1:

```python
    _ensure_column(conn, "groups", "is_closed", "INTEGER NOT NULL DEFAULT 0")
```

Update `GroupRow` (append the new field at the end):

```python
@dataclass
class GroupRow:
    id: int
    name: str
    source_page_url: str
    pdf_source_url: str
    send_time: str
    check_interval_minutes: int
    timezone: str
    last_sync_failed_at: str | None
    created_at: str
    starosta_chat_id: int | None
    last_starosta_change_at: str | None
    is_closed: bool
```

Update `_GROUP_COLUMNS`:

```python
_GROUP_COLUMNS = (
    "id, name, source_page_url, pdf_source_url, send_time, "
    "check_interval_minutes, timezone, last_sync_failed_at, created_at, "
    "starosta_chat_id, last_starosta_change_at, is_closed"
)
```

Introduce a row-conversion helper and switch every place that currently does `GroupRow(*row)` to use it instead (so the `is_closed` integer from SQLite becomes a real `bool`). Replace every occurrence of `GroupRow(*row) if row else None` and `[GroupRow(*row) for row in rows]` in `get_group`, `get_group_by_name`, `list_groups`, `list_all_groups` with:

```python
def _group_row_from_tuple(row) -> GroupRow:
    *rest, is_closed = row
    return GroupRow(*rest, bool(is_closed))
```

— i.e. `get_group` becomes:

```python
def get_group(conn: sqlite3.Connection, group_id: int) -> GroupRow | None:
    row = conn.execute(f"SELECT {_GROUP_COLUMNS} FROM groups WHERE id = ?", (group_id,)).fetchone()
    return _group_row_from_tuple(row) if row else None
```

(and the same substitution pattern for `get_group_by_name`, `list_groups`, `list_all_groups`).

Append:

```python
def update_group_closed(conn: sqlite3.Connection, group_id: int, is_closed: bool) -> None:
    conn.execute("UPDATE groups SET is_closed = ? WHERE id = ?", (1 if is_closed else 0, group_id))
    conn.commit()


def get_group_where_starosta(conn: sqlite3.Connection, chat_id: int) -> GroupRow | None:
    row = conn.execute(f"SELECT {_GROUP_COLUMNS} FROM groups WHERE starosta_chat_id = ?", (chat_id,)).fetchone()
    return _group_row_from_tuple(row) if row else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: PASS (full file — this also re-validates every existing `GroupRow`-returning function now goes through `_group_row_from_tuple`)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add groups.is_closed and get_group_where_starosta"
```

---

## Task 3: `group_change_requests.from_group_id` becomes nullable

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `storage.create_group_change_request` (sub-project 2 Task 3).
- Produces: `storage.create_group_change_request(conn, chat_id, from_group_id: int | None, to_group_id, requested_at) -> int` (signature widened); existing rows survive a schema rebuild on upgrade.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def test_create_group_change_request_accepts_null_from_group(conn):
    group_b = _make_group(conn, "B")
    request_id = storage.create_group_change_request(conn, 111, None, group_b, "t")
    request = storage.get_group_change_request(conn, request_id)
    assert request.from_group_id is None
    assert request.to_group_id == group_b


def test_migrate_group_change_requests_preserves_existing_rows(conn):
    """Simulates an upgrade from sub-project 2's NOT NULL schema: an
    existing row with a real from_group_id must survive the rebuild
    unchanged, including an already-decided one."""
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    conn.execute("DROP TABLE group_change_requests")
    conn.execute(
        "CREATE TABLE group_change_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
        "from_group_id INTEGER NOT NULL REFERENCES groups(id), to_group_id INTEGER NOT NULL REFERENCES groups(id), "
        "requested_at TEXT NOT NULL, status TEXT NOT NULL, decided_at TEXT)"
    )
    conn.execute(
        "INSERT INTO group_change_requests (chat_id, from_group_id, to_group_id, requested_at, status, decided_at) "
        "VALUES (111, ?, ?, 't', 'approved', 't2')",
        (group_a, group_b),
    )
    conn.commit()

    storage._migrate_group_change_requests_nullable_from_group(conn)

    rows = conn.execute("SELECT chat_id, from_group_id, to_group_id, status, decided_at FROM group_change_requests").fetchall()
    assert rows == [(111, group_a, group_b, "approved", "t2")]
    # and the column is now nullable:
    new_request_id = storage.create_group_change_request(conn, 222, None, group_b, "t3")
    assert storage.get_group_change_request(conn, new_request_id).from_group_id is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k "null_from_group or migrate_group_change_requests"`
Expected: FAIL — `create_group_change_request(conn, 111, None, ...)` raises `sqlite3.IntegrityError` (NOT NULL constraint), and `_migrate_group_change_requests_nullable_from_group` doesn't exist.

- [ ] **Step 3: Implement**

Change the `group_change_requests` `CREATE TABLE` statement in `init_db` (sub-project 2 Task 3) from:

```python
        CREATE TABLE IF NOT EXISTS group_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            from_group_id INTEGER NOT NULL REFERENCES groups(id),
            to_group_id INTEGER NOT NULL REFERENCES groups(id),
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_at TEXT
        );
```

to:

```python
        CREATE TABLE IF NOT EXISTS group_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            from_group_id INTEGER REFERENCES groups(id),
            to_group_id INTEGER NOT NULL REFERENCES groups(id),
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_at TEXT
        );
```

(this alone makes every *fresh* database correct; the function below fixes up a database that already ran sub-project 2's NOT NULL version). Add the migration function and call it from `init_db` right after the `executescript(...)` call:

```python
def _migrate_group_change_requests_nullable_from_group(conn: sqlite3.Connection) -> None:
    info = conn.execute("PRAGMA table_info(group_change_requests)").fetchall()
    from_group_column = next((row for row in info if row[1] == "from_group_id"), None)
    if from_group_column is None or from_group_column[3] == 0:
        return  # table doesn't exist yet, or is already nullable
    conn.executescript(
        """
        ALTER TABLE group_change_requests RENAME TO group_change_requests_old;
        CREATE TABLE group_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            from_group_id INTEGER REFERENCES groups(id),
            to_group_id INTEGER NOT NULL REFERENCES groups(id),
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_at TEXT
        );
        INSERT INTO group_change_requests
            (id, chat_id, from_group_id, to_group_id, requested_at, status, decided_at)
            SELECT id, chat_id, from_group_id, to_group_id, requested_at, status, decided_at
            FROM group_change_requests_old;
        DROP TABLE group_change_requests_old;
        """
    )
    conn.commit()
```

(`PRAGMA table_info` returns rows as `(cid, name, type, notnull, dflt_value, pk)` — index `3` is the `notnull` flag, `1` if the column currently forbids `NULL`.)

Call it in `init_db`, after the existing `executescript(...)` and `_ensure_column(...)` calls:

```python
    _migrate_group_change_requests_nullable_from_group(conn)
```

Update `create_group_change_request`'s signature (the body is unchanged — `sqlite3` already accepts `None` as a bind parameter once the column allows it):

```python
def create_group_change_request(
    conn: sqlite3.Connection, chat_id: int, from_group_id: int | None, to_group_id: int, requested_at: str
) -> int:
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: make group_change_requests.from_group_id nullable for first-time joins"
```

---

## Task 4: `schedule_bot/join_requests.py` — shared approval-gate logic

**Files:**
- Create: `schedule_bot/join_requests.py`
- Test: `tests/test_join_requests.py`

**Interfaces:**
- Consumes: `storage.has_group_exclusion`/`create_group_change_request`/`get_member` (Tasks 1-3), `telegram_utils.send_with_retry`.
- Produces: `join_requests.requires_approval(conn, group, chat_id) -> bool`, `join_requests.notify_approvers(bot, conn, target_group, request_id, requester_chat_id, admin_chat_id) -> None`, `join_requests.create_join_request_and_notify(bot, conn, chat_id, from_group_id, target_group, admin_chat_id) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_join_requests.py`:

```python
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.join_requests import create_join_request_and_notify, notify_approvers, requires_approval

ADMIN_CHAT_ID = 999


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_group(conn, name="Б-Мв-101"):
    return storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )


def test_requires_approval_false_for_open_group_without_exclusion(conn):
    group_id = _make_group(conn)
    group = storage.get_group(conn, group_id)
    assert requires_approval(conn, group, 111) is False


def test_requires_approval_true_for_closed_group(conn):
    group_id = _make_group(conn)
    storage.update_group_closed(conn, group_id, True)
    group = storage.get_group(conn, group_id)
    assert requires_approval(conn, group, 111) is True


def test_requires_approval_true_for_excluded_chat_even_if_group_open(conn):
    group_id = _make_group(conn)
    storage.create_group_exclusion(conn, group_id, 111, "t", ADMIN_CHAT_ID)
    group = storage.get_group(conn, group_id)
    assert requires_approval(conn, group, 111) is True


@pytest.mark.asyncio
async def test_notify_approvers_reaches_admin_and_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 555, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 555)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    request_id = storage.create_group_change_request(conn, 111, None, group_id, "t")
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await notify_approvers(bot, conn, group, request_id, 111, ADMIN_CHAT_ID)

    notified = {call.kwargs["chat_id"] for call in bot.send_message.await_args_list}
    assert notified == {ADMIN_CHAT_ID, 555}


@pytest.mark.asyncio
async def test_notify_approvers_admin_only_when_no_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    request_id = storage.create_group_change_request(conn, 111, None, group_id, "t")
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await notify_approvers(bot, conn, group, request_id, 111, ADMIN_CHAT_ID)

    notified = {call.kwargs["chat_id"] for call in bot.send_message.await_args_list}
    assert notified == {ADMIN_CHAT_ID}


@pytest.mark.asyncio
async def test_create_join_request_and_notify_creates_pending_row(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    request_id = await create_join_request_and_notify(bot, conn, 222, None, group, ADMIN_CHAT_ID)

    request = storage.get_group_change_request(conn, request_id)
    assert request.status == "pending"
    assert request.from_group_id is None
    assert request.to_group_id == group_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_join_requests.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.join_requests'`

- [ ] **Step 3: Implement**

Create `schedule_bot/join_requests.py`:

```python
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot import storage
from schedule_bot.telegram_utils import send_with_retry

logger = logging.getLogger(__name__)


def requires_approval(conn, group, chat_id: int) -> bool:
    return bool(group.is_closed) or storage.has_group_exclusion(conn, group.id, chat_id)


async def notify_approvers(bot, conn, target_group, request_id: int, requester_chat_id: int, admin_chat_id: int) -> None:
    requester = storage.get_member(conn, requester_chat_id)
    label = "Неизвестный"
    if requester is not None:
        label = requester.telegram_username or requester.display_name or "Неизвестный"
    text = f"Заявка на переход в группу «{target_group.name}» от {label}. Одобрить / Отклонить"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"chgreq:approve:{request_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"chgreq:reject:{request_id}"),
            ]
        ]
    )
    recipients = {admin_chat_id}
    if target_group.starosta_chat_id is not None:
        recipients.add(target_group.starosta_chat_id)
    for recipient_chat_id in recipients:
        try:
            await send_with_retry(
                lambda recipient_chat_id=recipient_chat_id: bot.send_message(
                    chat_id=recipient_chat_id, text=text, reply_markup=keyboard
                )
            )
        except Exception as error:
            logger.warning("join_requests: failed to notify chat_id=%s: %s", recipient_chat_id, error)


async def create_join_request_and_notify(
    bot, conn, chat_id: int, from_group_id: int | None, target_group, admin_chat_id: int
) -> int:
    request_id = storage.create_group_change_request(
        conn, chat_id, from_group_id, target_group.id, datetime.now(timezone.utc).isoformat()
    )
    await notify_approvers(bot, conn, target_group, request_id, chat_id, admin_chat_id)
    return request_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_join_requests.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/join_requests.py tests/test_join_requests.py
git commit -m "feat: shared closed-group/exclusion approval gate"
```

---

## Task 5: Gate `/start`'s group-select on closed/excluded

**Files:**
- Modify: `schedule_bot/group_commands.py`
- Test: `tests/test_group_commands.py`

**Interfaces:**
- Consumes: `join_requests.requires_approval`/`create_join_request_and_notify` (Task 4), `storage.get_pending_request_for_chat` (sub-project 2 Task 3).
- Produces: `group_commands.group_select_callback` now routes through an approval request instead of instant-attaching when the target group is closed or the caller was excluded from it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_group_commands.py`:

```python
@pytest.mark.asyncio
async def test_group_select_callback_routes_closed_group_through_request(conn):
    _make_groups(conn, 1)
    group_id = storage.list_all_groups(conn)[0].id
    storage.update_group_closed(conn, group_id, True)
    update = make_callback_update(111, f"grp:select:{group_id}")
    context = make_context(conn)
    context.bot_data["admin_chat_id"] = 999
    context.bot.send_message = AsyncMock()

    await group_select_callback(update, context)

    assert storage.get_member(conn, 111) is None
    assert storage.get_pending_request_for_chat(conn, 111) is not None
    context.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_select_callback_routes_excluded_chat_through_request_even_if_open(conn):
    _make_groups(conn, 1)
    group_id = storage.list_all_groups(conn)[0].id
    storage.create_group_exclusion(conn, group_id, 111, "t", 999)
    update = make_callback_update(111, f"grp:select:{group_id}")
    context = make_context(conn)
    context.bot_data["admin_chat_id"] = 999
    context.bot.send_message = AsyncMock()

    await group_select_callback(update, context)

    assert storage.get_member(conn, 111) is None
    assert storage.get_pending_request_for_chat(conn, 111) is not None


@pytest.mark.asyncio
async def test_group_select_callback_refuses_second_pending_request(conn):
    _make_groups(conn, 1)
    group_id = storage.list_all_groups(conn)[0].id
    storage.update_group_closed(conn, group_id, True)
    storage.create_group_change_request(conn, 111, None, group_id, "t")
    update = make_callback_update(111, f"grp:select:{group_id}")
    context = make_context(conn)
    context.bot_data["admin_chat_id"] = 999
    context.bot.send_message = AsyncMock()

    await group_select_callback(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_select_callback_still_instant_for_open_unrestricted_group(conn):
    _make_groups(conn, 1)
    group_id = storage.list_all_groups(conn)[0].id
    update = make_callback_update(111, f"grp:select:{group_id}")
    context = make_context(conn)
    context.bot_data["admin_chat_id"] = 999

    await group_select_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_id
```

(Check `make_context` in `tests/test_group_commands.py` already sets `context.bot_data = {"conn": conn}` — add `"admin_chat_id": 999` inside the fixture/helper itself instead of per-test if that's simpler; either approach is fine as long as `context.bot_data["admin_chat_id"]` is present for every test that reaches the approval-request branch.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v -k closed`
Expected: FAIL — the current `group_select_callback` always instant-attaches, ignoring `is_closed`/exclusions.

- [ ] **Step 3: Implement**

In `schedule_bot/group_commands.py`, add the import `from schedule_bot.join_requests import create_join_request_and_notify, requires_approval`, and replace `group_select_callback`'s body:

```python
async def group_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.removeprefix(SELECT_PREFIX))
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
        await create_join_request_and_notify(context.bot, conn, chat_id, None, group, context.bot_data["admin_chat_id"])
        await query.message.reply_text("Эта группа закрыта или ограничена для вас — заявка отправлена, ожидайте решения.")
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_commands.py tests/test_group_commands.py
git commit -m "feat: route closed-group/excluded joins through approval, not instant-attach"
```

---

## Task 6: `change_group_commands.py` — DRY the notifier + `/requests`

**Files:**
- Modify: `schedule_bot/change_group_commands.py`
- Test: `tests/test_change_group_commands.py`

**Interfaces:**
- Consumes: `join_requests.notify_approvers` (Task 4), `storage.list_pending_requests_for_group`/`get_group_where_starosta` (sub-project 2 Task 3, Task 2), `elections.format_candidate_label` (sub-project 2 Task 4), `bot._resolve_admin_group`/`is_admin` (sub-project 1 Task 10).
- Produces: `change_group_commands.requests_command`; `change_group_commands._notify_approvers` is removed, callers use `join_requests.notify_approvers` instead.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_change_group_commands.py`:

```python
from schedule_bot.change_group_commands import requests_command


@pytest.mark.asyncio
async def test_requests_command_starosta_sees_own_group_pending_requests(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")

    update = make_message_update(555)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    update.message.reply_text.assert_awaited_once()
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_requests_command_reports_empty_list(conn):
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)

    update = make_message_update(555)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    assert "Нет заявок" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_requests_command_rejects_plain_member(conn):
    group_a = _make_group(conn, "A")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_message_update(111)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    assert "не староста" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_requests_command_admin_requires_group_id(conn):
    update = make_message_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    assert "Использование" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_requests_command_admin_with_group_id_sees_requests(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")

    update = make_message_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_b)]

    await requests_command(update, context)

    update.message.reply_text.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_change_group_commands.py -v -k requests`
Expected: FAIL with `ImportError: cannot import name 'requests_command'`

- [ ] **Step 3: Implement**

In `schedule_bot/change_group_commands.py`:

1. Remove the module's own `_notify_approvers` function entirely.
2. Add the import `from schedule_bot.join_requests import notify_approvers` and `from schedule_bot import elections`, `from schedule_bot.bot import _resolve_admin_group, is_admin`.
3. In `change_group_confirm_callback`, replace the call `await _notify_approvers(context.bot, conn, target_group, request_id, chat_id, context.bot_data["admin_chat_id"])` with `await notify_approvers(context.bot, conn, target_group, request_id, chat_id, context.bot_data["admin_chat_id"])`.
4. Append:

```python
async def requests_command(update, context) -> None:
    conn = context.bot_data["conn"]
    if is_admin(update, context):
        if not context.args:
            await update.message.reply_text("Использование: /requests <group_id>")
            return
        group = _resolve_admin_group(conn, context.args[0])
        if group is None:
            await update.message.reply_text("Группа с таким id не найдена.")
            return
    else:
        group = storage.get_group_where_starosta(conn, update.effective_chat.id)
        if group is None:
            await update.message.reply_text("Вы не староста ни одной группы.")
            return

    pending = storage.list_pending_requests_for_group(conn, group.id)
    if not pending:
        await update.message.reply_text("Нет заявок на рассмотрении.")
        return

    for request in pending:
        requester = storage.get_member(conn, request.chat_id)
        label = elections.format_candidate_label(requester) if requester is not None else "Неизвестный"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Одобрить", callback_data=f"chgreq:approve:{request.id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"chgreq:reject:{request.id}"),
                ]
            ]
        )
        await update.message.reply_text(
            f"Заявка от {label} на переход в группу «{group.name}».", reply_markup=keyboard
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_change_group_commands.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/change_group_commands.py tests/test_change_group_commands.py
git commit -m "feat: /requests lists pending join/change requests; dedupe notify logic"
```

---

## Task 7: `/close_group`, `/open_group`, `/notify`

**Files:**
- Modify: `schedule_bot/bot.py`
- Test: `tests/test_bot_admin_commands.py`

**Interfaces:**
- Consumes: `storage.get_group_where_starosta`/`update_group_closed`/`list_group_members` (Tasks 2 and sub-project 1/2), `bot._resolve_admin_group`/`is_admin` (sub-project 1 Task 10).
- Produces: `bot.close_group_command`, `bot.open_group_command`, `bot.notify_command`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bot_admin_commands.py`:

```python
from schedule_bot.bot import close_group_command, notify_command, open_group_command


@pytest.mark.asyncio
async def test_close_group_by_starosta_without_group_id(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    update = make_update(111)
    context = make_context(conn)
    context.args = []

    await close_group_command(update, context)

    assert storage.get_group(conn, group_id).is_closed is True


@pytest.mark.asyncio
async def test_close_group_rejects_plain_member(conn):
    group_id = _make_group(conn)
    update = make_update(111)
    context = make_context(conn)
    context.args = []

    await close_group_command(update, context)

    assert storage.get_group(conn, group_id).is_closed is False
    assert "не староста" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_open_group_by_admin_with_group_id(conn):
    group_id = _make_group(conn)
    storage.update_group_closed(conn, group_id, True)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id)]

    await open_group_command(update, context)

    assert storage.get_group(conn, group_id).is_closed is False


@pytest.mark.asyncio
async def test_open_group_admin_without_group_id_asks_for_usage(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = []

    await open_group_command(update, context)

    assert "Использование" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_notify_command_by_starosta_broadcasts_to_whole_group(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t", subscribed=False)
    update = make_update(111)
    context = make_context(conn)
    context.args = ["Внимание"]
    context.bot.send_message = AsyncMock()

    await notify_command(update, context)

    assert context.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notify_command_rejects_plain_member(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_update(111)
    context = make_context(conn)
    context.args = ["Внимание"]
    context.bot.send_message = AsyncMock()

    await notify_command(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_command_by_admin_requires_group_id(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = ["текст", "без", "group_id"]
    context.bot.send_message = AsyncMock()

    await notify_command(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_command_by_admin_with_group_id_broadcasts(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "Внимание"]
    context.bot.send_message = AsyncMock()

    await notify_command(update, context)

    context.bot.send_message.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_bot_admin_commands.py -v -k "close_group or open_group or notify_command"`
Expected: FAIL with `ImportError: cannot import name 'close_group_command'`

- [ ] **Step 3: Implement**

Append to `schedule_bot/bot.py`:

```python
async def _toggle_group_closed(update: Update, context: ContextTypes.DEFAULT_TYPE, is_closed: bool) -> None:
    conn = context.bot_data["conn"]
    if is_admin(update, context):
        if not context.args:
            command_name = "close_group" if is_closed else "open_group"
            await update.message.reply_text(f"Использование: /{command_name} <group_id>")
            return
        group = _resolve_admin_group(conn, context.args[0])
        if group is None:
            await update.message.reply_text("Группа с таким id не найдена.")
            return
    else:
        group = storage.get_group_where_starosta(conn, update.effective_chat.id)
        if group is None:
            await update.message.reply_text("Вы не староста ни одной группы.")
            return

    storage.update_group_closed(conn, group.id, is_closed)
    state = "закрыта" if is_closed else "открыта"
    await update.message.reply_text(f"Группа «{group.name}» теперь {state}.")


async def close_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /close_group from chat_id=%s args=%s", update.effective_chat.id, context.args)
    await _toggle_group_closed(update, context, True)


async def open_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /open_group from chat_id=%s args=%s", update.effective_chat.id, context.args)
    await _toggle_group_closed(update, context, False)


async def notify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /notify from chat_id=%s args=%s", update.effective_chat.id, context.args)
    conn = context.bot_data["conn"]

    if is_admin(update, context):
        if len(context.args) < 2:
            await update.message.reply_text("Использование: /notify <group_id> <текст>")
            return
        group = _resolve_admin_group(conn, context.args[0])
        if group is None:
            await update.message.reply_text("Группа с таким id не найдена.")
            return
        text = " ".join(context.args[1:])
    else:
        group = storage.get_group_where_starosta(conn, update.effective_chat.id)
        if group is None:
            await update.message.reply_text("Вы не староста ни одной группы.")
            return
        if not context.args:
            await update.message.reply_text("Использование: /notify <текст>")
            return
        text = " ".join(context.args)

    members = storage.list_group_members(conn, group.id, subscribed_only=False)
    for member_chat_id in members:
        try:
            await send_with_retry(
                lambda member_chat_id=member_chat_id: context.bot.send_message(chat_id=member_chat_id, text=text)
            )
        except Exception as error:
            logger.warning("bot: failed to notify chat_id=%s: %s", member_chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)
    await update.message.reply_text(f"Оповещение отправлено {len(members)} участникам группы «{group.name}».")
```

Add the three to `ADMIN_COMMANDS`:

```python
    ("close_group", "Закрыть группу на вступление: [group_id] (без аргумента — своя группа)"),
    ("open_group", "Открыть группу для вступления: [group_id] (без аргумента — своя группа)"),
    ("notify", "Оповестить всех закреплённых за группой: [group_id] <текст>"),
```

(these three are usable by both admin and starosta, but `ADMIN_COMMANDS`/`USER_COMMANDS` only control what `/help` shows to which role — list them in `ADMIN_COMMANDS` since the admin branch is what needs documenting there; a starosta discovers them via `/starosta`'s own guidance text, not `/help`, matching how `/votes`/`/requests` aren't in either list.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_bot_admin_commands.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/bot.py tests/test_bot_admin_commands.py
git commit -m "feat: /close_group, /open_group, /notify"
```

---

## Task 8: Exclusion — `/classmates` button + confirmation

**Files:**
- Modify: `schedule_bot/classmates.py`
- Test: `tests/test_classmates.py`

**Interfaces:**
- Consumes: `storage.get_group`/`create_group_exclusion`/`remove_member` (Tasks 1, sub-project 1), `elections.format_candidate_label` (sub-project 2 Task 4).
- Produces: `classmates.classmates_command` gains an exclude button for starosta/admin callers; `classmates.exclude_select_callback`, `classmates.exclude_abort_callback`, `classmates.exclude_confirm_callback`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_classmates.py`:

```python
from schedule_bot.classmates import exclude_confirm_callback, exclude_select_callback

ADMIN_CHAT_ID = 999


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def make_context_with_admin(conn, admin_chat_id=ADMIN_CHAT_ID):
    context = MagicMock()
    context.bot_data = {"conn": conn, "admin_chat_id": admin_chat_id}
    context.bot.send_message = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_classmates_command_shows_exclude_buttons_for_starosta(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    update = make_update(111)
    context = make_context_with_admin(conn)

    await classmates_command(update, context)

    _, kwargs = update.message.reply_text.call_args
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data == "exclude:select:222" for b in buttons)
    assert not any(b.callback_data == "exclude:select:111" for b in buttons)  # no self-exclude button


@pytest.mark.asyncio
async def test_classmates_command_no_exclude_buttons_for_plain_member(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    update = make_update(222)
    context = make_context_with_admin(conn)

    await classmates_command(update, context)

    call_kwargs = update.message.reply_text.call_args[1]
    assert "reply_markup" not in call_kwargs


@pytest.mark.asyncio
async def test_exclude_confirm_callback_removes_member_and_records_exclusion(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    update = make_callback_update(111, "exclude:confirm:222")
    context = make_context_with_admin(conn)

    await exclude_confirm_callback(update, context)

    assert storage.get_member(conn, 222) is None
    assert storage.has_group_exclusion(conn, group_id, 222) is True
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 222


@pytest.mark.asyncio
async def test_exclude_confirm_callback_rejects_self_exclusion_even_if_called_directly(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)

    update = make_callback_update(111, "exclude:confirm:111")
    context = make_context_with_admin(conn)

    await exclude_confirm_callback(update, context)

    assert storage.get_member(conn, 111) is not None
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_exclude_confirm_callback_rejects_unauthorized_caller(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.upsert_member(conn, 333, group_id, joined_at="t")

    update = make_callback_update(333, "exclude:confirm:222")  # 333 is neither starosta nor admin
    context = make_context_with_admin(conn)

    await exclude_confirm_callback(update, context)

    assert storage.get_member(conn, 222) is not None


@pytest.mark.asyncio
async def test_exclude_select_callback_shows_confirmation(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    update = make_callback_update(111, "exclude:select:222")
    context = make_context_with_admin(conn)

    await exclude_select_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_exclude_select_callback_rejects_self_selection(conn):
    update = make_callback_update(111, "exclude:select:111")
    context = make_context_with_admin(conn)

    await exclude_select_callback(update, context)

    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "самого себя" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_classmates.py -v -k exclude`
Expected: FAIL with `ImportError: cannot import name 'exclude_select_callback'`

- [ ] **Step 3: Implement**

In `schedule_bot/classmates.py`, add imports (`logging`, `datetime`/`timezone`, `InlineKeyboardButton`/`InlineKeyboardMarkup` from `telegram`, `send_with_retry` from `schedule_bot.telegram_utils`), then replace `classmates_command` and append the three callbacks:

```python
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot import storage
from schedule_bot.elections import format_candidate_label
from schedule_bot.telegram_utils import send_with_retry

logger = logging.getLogger(__name__)


def format_member_line(member, is_starosta: bool) -> str:
    label = format_candidate_label(member)
    return f"👑 {label}" if is_starosta else label


async def classmates_command(update, context) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return

    group = storage.get_group(conn, member.group_id)
    chat_ids = storage.list_group_members(conn, group.id, subscribed_only=False)
    members = [storage.get_member(conn, cid) for cid in chat_ids]
    lines = sorted(format_member_line(m, m.chat_id == group.starosta_chat_id) for m in members)

    text = "\n".join(lines)
    if group.starosta_chat_id is not None:
        text += "\n\n👑 — староста группы"

    can_exclude = group.starosta_chat_id == chat_id or chat_id == context.bot_data["admin_chat_id"]
    if can_exclude:
        rows = []
        for m in members:
            if m.chat_id == chat_id:
                continue
            label = format_member_line(m, m.chat_id == group.starosta_chat_id)
            rows.append([InlineKeyboardButton(f"❌ Исключить {label}", callback_data=f"exclude:select:{m.chat_id}")])
        if rows:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
            return

    await update.message.reply_text(text)


async def exclude_select_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    target_chat_id = int(query.data.split(":")[2])
    caller_chat_id = update.effective_chat.id
    if target_chat_id == caller_chat_id:
        await query.message.reply_text("Нельзя исключить самого себя.")
        return
    conn = context.bot_data["conn"]
    target = storage.get_member(conn, target_chat_id)
    if target is None:
        await query.message.reply_text("Этот участник уже не в группе.")
        return
    label = format_candidate_label(target)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"exclude:confirm:{target_chat_id}"),
                InlineKeyboardButton("✖️ Отмена", callback_data="exclude:abort"),
            ]
        ]
    )
    await query.message.reply_text(f"Исключить {label} из группы?", reply_markup=keyboard)


async def exclude_abort_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Отменено.")


async def exclude_confirm_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    target_chat_id = int(query.data.split(":")[2])
    caller_chat_id = update.effective_chat.id
    if target_chat_id == caller_chat_id:
        await query.message.reply_text("Нельзя исключить самого себя.")
        return

    conn = context.bot_data["conn"]
    target = storage.get_member(conn, target_chat_id)
    if target is None:
        await query.message.reply_text("Этот участник уже не в группе.")
        return
    group = storage.get_group(conn, target.group_id)
    caller_is_admin = caller_chat_id == context.bot_data["admin_chat_id"]
    caller_is_starosta = group.starosta_chat_id == caller_chat_id
    if not (caller_is_admin or caller_is_starosta):
        await query.message.reply_text("Недостаточно прав.")
        return

    now = datetime.now(timezone.utc).isoformat()
    storage.create_group_exclusion(conn, group.id, target_chat_id, now, caller_chat_id)
    storage.remove_member(conn, target_chat_id)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=target_chat_id, text=f"Вы были исключены из группы «{group.name}»."
            )
        )
    except Exception as error:
        logger.warning("classmates: failed to notify excluded chat_id=%s: %s", target_chat_id, error)
    await query.message.reply_text("Участник исключён.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_classmates.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/classmates.py tests/test_classmates.py
git commit -m "feat: exclude-member button and confirmation on /classmates"
```

---

## Task 9: `main.py` wiring

**Files:**
- Modify: `main.py`
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: everything from Tasks 1-8.
- Produces: `main.build_application` registers `/close_group`, `/open_group`, `/notify`, `/requests`, and the `exclude:*` callback handlers.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main_wiring.py`:

```python
def test_build_application_registers_privilege_handlers(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)
    assert {"close_group", "open_group", "notify", "requests"}.issubset(command_names)

    callback_patterns = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "pattern") and handler.pattern is not None:
                callback_patterns.add(handler.pattern.pattern)
    assert r"^exclude:select:\d+$" in callback_patterns
    assert r"^exclude:confirm:\d+$" in callback_patterns
    assert r"^exclude:abort$" in callback_patterns

    application.bot_data["_scheduler"].shutdown(wait=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v -k privilege`
Expected: FAIL — `"close_group" not in command_names`.

- [ ] **Step 3: Implement**

In `main.py`, add imports:

```python
from schedule_bot.bot import close_group_command, notify_command, open_group_command
from schedule_bot.change_group_commands import requests_command
from schedule_bot.classmates import exclude_abort_callback, exclude_confirm_callback, exclude_select_callback
```

In `build_application`'s handler registration block, add:

```python
    application.add_handler(CommandHandler("close_group", close_group_command))
    application.add_handler(CommandHandler("open_group", open_group_command))
    application.add_handler(CommandHandler("notify", notify_command))
    application.add_handler(CommandHandler("requests", requests_command))

    application.add_handler(CallbackQueryHandler(exclude_select_callback, pattern=r"^exclude:select:\d+$"))
    application.add_handler(CallbackQueryHandler(exclude_confirm_callback, pattern=r"^exclude:confirm:\d+$"))
    application.add_handler(CallbackQueryHandler(exclude_abort_callback, pattern=r"^exclude:abort$"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: PASS (full file)

Then run the full suite:

Run: `./.venv/bin/python -m pytest -v`
Expected: PASS (every test in the repository, across sub-projects 1-4)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "feat: wire starosta-privilege handlers into main.py"
```
