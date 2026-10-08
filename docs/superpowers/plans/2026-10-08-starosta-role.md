# Роль старосты — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the starosta role — first-time nomination and change-of-starosta voting (2-approval, 2-week cooldown, min-3-member gate), an admin override, a pull-based `/votes` view of open elections, and the group-change-request approval flow deferred from sub-project 1.

**Architecture:** Two new storage-backed concepts — `elections`/`election_approvals` (voting) and `group_change_requests` (approval-gated group moves) — plus two new `groups` columns (`starosta_chat_id`, `last_starosta_change_at`). A pure module `schedule_bot/elections.py` holds cooldown/min-size rules and label formatting, used by two new handler modules: `schedule_bot/starosta_commands.py` (nomination, change-vote, `/votes`) and `schedule_bot/change_group_commands.py` (`/change_group` + its approval flow). Admin overrides (`/set_starosta`, `/remove_starosta`) join the existing admin commands in `bot.py`.

**Tech Stack:** Same as sub-project 1 (python-telegram-bot `CallbackQueryHandler`, SQLite, pytest/pytest-asyncio).

**Spec:** `docs/superpowers/specs/2026-10-08-starosta-role-design.md`

**Depends on:** sub-project 1 (`groups`, `members`, `storage.get_member`/`get_group`/`list_group_members`/`list_all_groups`/`upsert_member`, `bot._resolve_admin_group`, `group_commands.GROUPS_PER_PAGE`, `main.py`'s handler registration shape).

## Global Constraints

- A group needs **≥3** members (`list_group_members(subscribed_only=False)`) before any election (first nomination or change) can start.
- Only one `status='open'` election per group at a time.
- "Отклонить" never writes to storage and never blocks — only the approval count decides.
- The 2-week change cooldown is measured from `groups.last_starosta_change_at`, which is set on every successful change: election-approved, first nomination, **and** admin override.
- An admin override (`/set_starosta`/`/remove_starosta`) always cancels any open election for that group and resets the cooldown clock.
- Every new broadcast loop (election announcement, change-request notification) isolates per-recipient failures with try/except, matching the rest of the codebase.

## Review Focus

- Nominating/changing starosta in a group with fewer than 3 members must be blocked with the "invite more people" message, not silently create an unwinnable election.
- The same `chat_id` clicking "Одобрить" twice on one election must not double-count (idempotent `election_approvals` insert).
- Starting a change-vote while the 2-week cooldown is still active must be blocked with the retry date, even via the menu-independent direct callback path.
- An admin override while an election is open must cancel that election (not leave it dangling) and reset the cooldown.
- `/change_group` while the caller already has a pending request must be rejected, both from the command and from the confirm-callback path (race between opening the picker and confirming).

---

## Task 1: `groups.starosta_chat_id`/`last_starosta_change_at` + CRUD

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `storage._column_exists` (sub-project 1 Task 4), `storage.GroupRow`/`create_group`/`get_group` (sub-project 1 Task 1).
- Produces: `GroupRow` gains `starosta_chat_id: int | None`, `last_starosta_change_at: str | None` (appended fields); `storage.update_group_starosta(conn, group_id, chat_id: int | None) -> None`; `storage.set_group_last_starosta_change_at(conn, group_id, value: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def test_create_group_starosta_fields_default_to_none(conn):
    group_id = _make_group(conn)
    group = storage.get_group(conn, group_id)
    assert group.starosta_chat_id is None
    assert group.last_starosta_change_at is None


def test_update_group_starosta_sets_and_clears(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    assert storage.get_group(conn, group_id).starosta_chat_id == 111
    storage.update_group_starosta(conn, group_id, None)
    assert storage.get_group(conn, group_id).starosta_chat_id is None


def test_set_group_last_starosta_change_at(conn):
    group_id = _make_group(conn)
    storage.set_group_last_starosta_change_at(conn, group_id, "2026-10-08T00:00:00+00:00")
    assert storage.get_group(conn, group_id).last_starosta_change_at == "2026-10-08T00:00:00+00:00"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k starosta_field`
Expected: FAIL — `GroupRow` has no `starosta_chat_id` field yet (positional-arg mismatch or `AttributeError`).

- [ ] **Step 3: Implement**

In `schedule_bot/storage.py`'s `init_db`, right after the `groups`/`members`/`schedule_versions` table block (and after the existing `_ensure_column`/`_column_exists`-style migration calls from sub-project 1's Task 4, if `init_db` calls them there — otherwise add these two calls at the end of `init_db`, after the `executescript(...)` call):

```python
    _ensure_column(conn, "groups", "starosta_chat_id", "INTEGER REFERENCES members(chat_id)")
    _ensure_column(conn, "groups", "last_starosta_change_at", "TEXT")
```

Add the small helper next to `_column_exists` (defined in sub-project 1 Task 4) if it doesn't already exist:

```python
def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        conn.commit()
```

Update `GroupRow` (append the two new fields at the end, so existing positional construction via `GroupRow(*row)` still works as long as the column list is updated to match):

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
```

Update `_GROUP_COLUMNS`:

```python
_GROUP_COLUMNS = (
    "id, name, source_page_url, pdf_source_url, send_time, "
    "check_interval_minutes, timezone, last_sync_failed_at, created_at, "
    "starosta_chat_id, last_starosta_change_at"
)
```

Append two new functions:

```python
def update_group_starosta(conn: sqlite3.Connection, group_id: int, chat_id: int | None) -> None:
    conn.execute("UPDATE groups SET starosta_chat_id = ? WHERE id = ?", (chat_id, group_id))
    conn.commit()


def set_group_last_starosta_change_at(conn: sqlite3.Connection, group_id: int, value: str) -> None:
    conn.execute("UPDATE groups SET last_starosta_change_at = ? WHERE id = ?", (value, group_id))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: PASS (full file — this also catches any other test relying on `GroupRow`'s exact field count, e.g. anything built with `_make_group`)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add starosta_chat_id/last_starosta_change_at to groups"
```

---

## Task 2: `elections`/`election_approvals` tables and CRUD

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `storage.ElectionRow` (dataclass: `id, group_id, kind, initiated_by_chat_id, candidate_chat_id, status, started_at, decided_at`), `storage.create_election(conn, group_id, kind, initiated_by_chat_id, candidate_chat_id, started_at) -> int`, `storage.get_election(conn, election_id) -> ElectionRow | None`, `storage.get_open_election(conn, group_id) -> ElectionRow | None`, `storage.list_open_elections(conn, group_id) -> list[ElectionRow]`, `storage.close_election(conn, election_id, status, decided_at) -> None`, `storage.cancel_open_elections_for_group(conn, group_id, decided_at) -> None`, `storage.add_election_approval(conn, election_id, chat_id, approved_at) -> bool` (True if newly inserted), `storage.count_election_approvals(conn, election_id) -> int`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def test_create_election_defaults_to_open(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "2026-10-08T00:00:00")
    election = storage.get_election(conn, election_id)
    assert election.status == "open"
    assert election.kind == "initial"
    assert election.candidate_chat_id == 111


def test_get_open_election_returns_none_when_absent(conn):
    group_id = _make_group(conn)
    assert storage.get_open_election(conn, group_id) is None


def test_get_open_election_finds_the_open_one(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.close_election(conn, election_id, "cancelled", "t2")
    second_id = storage.create_election(conn, group_id, "change", 222, 333, "t3")
    open_election = storage.get_open_election(conn, group_id)
    assert open_election.id == second_id


def test_list_open_elections_is_scoped_by_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_election(conn, group_a, "initial", 111, 111, "t")
    storage.create_election(conn, group_b, "initial", 222, 222, "t")
    assert len(storage.list_open_elections(conn, group_a)) == 1


def test_close_election_updates_status_and_decided_at(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.close_election(conn, election_id, "approved", "2026-10-08T01:00:00")
    election = storage.get_election(conn, election_id)
    assert election.status == "approved"
    assert election.decided_at == "2026-10-08T01:00:00"


def test_cancel_open_elections_for_group_only_affects_open_ones(conn):
    group_id = _make_group(conn)
    open_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.cancel_open_elections_for_group(conn, group_id, "t2")
    assert storage.get_election(conn, open_id).status == "cancelled"
    assert storage.get_open_election(conn, group_id) is None


def test_add_election_approval_is_idempotent_per_chat_id(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    first = storage.add_election_approval(conn, election_id, 222, "t")
    second = storage.add_election_approval(conn, election_id, 222, "t2")
    assert first is True
    assert second is False
    assert storage.count_election_approvals(conn, election_id) == 1


def test_count_election_approvals_reaches_two_on_distinct_voters(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.add_election_approval(conn, election_id, 222, "t")
    storage.add_election_approval(conn, election_id, 333, "t")
    assert storage.count_election_approvals(conn, election_id) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k election`
Expected: FAIL with `AttributeError: module 'schedule_bot.storage' has no attribute 'create_election'`

- [ ] **Step 3: Implement**

Add to `init_db`'s `executescript` (after the `members`/`schedule_versions` block):

```python
        CREATE TABLE IF NOT EXISTS elections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            kind TEXT NOT NULL,
            initiated_by_chat_id INTEGER NOT NULL,
            candidate_chat_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS election_approvals (
            election_id INTEGER NOT NULL REFERENCES elections(id),
            chat_id INTEGER NOT NULL,
            approved_at TEXT NOT NULL,
            UNIQUE(election_id, chat_id)
        );
```

Append to `storage.py`:

```python
@dataclass
class ElectionRow:
    id: int
    group_id: int
    kind: str
    initiated_by_chat_id: int
    candidate_chat_id: int
    status: str
    started_at: str
    decided_at: str | None


_ELECTION_COLUMNS = (
    "id, group_id, kind, initiated_by_chat_id, candidate_chat_id, status, started_at, decided_at"
)


def create_election(
    conn: sqlite3.Connection, group_id: int, kind: str, initiated_by_chat_id: int,
    candidate_chat_id: int, started_at: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO elections (group_id, kind, initiated_by_chat_id, candidate_chat_id, status, started_at) "
        "VALUES (?, ?, ?, ?, 'open', ?)",
        (group_id, kind, initiated_by_chat_id, candidate_chat_id, started_at),
    )
    conn.commit()
    return cursor.lastrowid


def get_election(conn: sqlite3.Connection, election_id: int) -> ElectionRow | None:
    row = conn.execute(f"SELECT {_ELECTION_COLUMNS} FROM elections WHERE id = ?", (election_id,)).fetchone()
    return ElectionRow(*row) if row else None


def get_open_election(conn: sqlite3.Connection, group_id: int) -> ElectionRow | None:
    row = conn.execute(
        f"SELECT {_ELECTION_COLUMNS} FROM elections WHERE group_id = ? AND status = 'open'", (group_id,)
    ).fetchone()
    return ElectionRow(*row) if row else None


def list_open_elections(conn: sqlite3.Connection, group_id: int) -> list[ElectionRow]:
    rows = conn.execute(
        f"SELECT {_ELECTION_COLUMNS} FROM elections WHERE group_id = ? AND status = 'open'", (group_id,)
    ).fetchall()
    return [ElectionRow(*row) for row in rows]


def close_election(conn: sqlite3.Connection, election_id: int, status: str, decided_at: str) -> None:
    conn.execute("UPDATE elections SET status = ?, decided_at = ? WHERE id = ?", (status, decided_at, election_id))
    conn.commit()


def cancel_open_elections_for_group(conn: sqlite3.Connection, group_id: int, decided_at: str) -> None:
    conn.execute(
        "UPDATE elections SET status = 'cancelled', decided_at = ? WHERE group_id = ? AND status = 'open'",
        (decided_at, group_id),
    )
    conn.commit()


def add_election_approval(conn: sqlite3.Connection, election_id: int, chat_id: int, approved_at: str) -> bool:
    cursor = conn.execute(
        "INSERT OR IGNORE INTO election_approvals (election_id, chat_id, approved_at) VALUES (?, ?, ?)",
        (election_id, chat_id, approved_at),
    )
    conn.commit()
    return cursor.rowcount > 0


def count_election_approvals(conn: sqlite3.Connection, election_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM election_approvals WHERE election_id = ?", (election_id,)
    ).fetchone()[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k election`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add elections/election_approvals tables and CRUD"
```

---

## Task 3: `group_change_requests` table and CRUD

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `storage.GroupChangeRequestRow` (dataclass: `id, chat_id, from_group_id, to_group_id, requested_at, status, decided_at`), `storage.create_group_change_request(conn, chat_id, from_group_id, to_group_id, requested_at) -> int`, `storage.get_pending_request_for_chat(conn, chat_id) -> GroupChangeRequestRow | None`, `storage.get_group_change_request(conn, request_id) -> GroupChangeRequestRow | None`, `storage.decide_group_change_request(conn, request_id, status, decided_at) -> None`, `storage.list_pending_requests_for_group(conn, group_id) -> list[GroupChangeRequestRow]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def test_create_group_change_request_defaults_to_pending(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    request = storage.get_group_change_request(conn, request_id)
    assert request.status == "pending"
    assert request.from_group_id == group_a
    assert request.to_group_id == group_b


def test_get_pending_request_for_chat_returns_none_when_absent(conn):
    assert storage.get_pending_request_for_chat(conn, 111) is None


def test_get_pending_request_for_chat_ignores_decided_requests(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    storage.decide_group_change_request(conn, request_id, "approved", "t2")
    assert storage.get_pending_request_for_chat(conn, 111) is None


def test_decide_group_change_request_updates_status(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    storage.decide_group_change_request(conn, request_id, "rejected", "t2")
    request = storage.get_group_change_request(conn, request_id)
    assert request.status == "rejected"
    assert request.decided_at == "t2"


def test_list_pending_requests_for_group_scoped_by_target(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t1")
    storage.create_group_change_request(conn, 222, group_b, group_a, "t2")
    pending_for_b = storage.list_pending_requests_for_group(conn, group_b)
    assert [r.chat_id for r in pending_for_b] == [111]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k change_request`
Expected: FAIL with `AttributeError: module 'schedule_bot.storage' has no attribute 'create_group_change_request'`

- [ ] **Step 3: Implement**

Add to `init_db`'s `executescript`:

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

Append to `storage.py`:

```python
@dataclass
class GroupChangeRequestRow:
    id: int
    chat_id: int
    from_group_id: int
    to_group_id: int
    requested_at: str
    status: str
    decided_at: str | None


_REQUEST_COLUMNS = "id, chat_id, from_group_id, to_group_id, requested_at, status, decided_at"


def create_group_change_request(
    conn: sqlite3.Connection, chat_id: int, from_group_id: int, to_group_id: int, requested_at: str
) -> int:
    cursor = conn.execute(
        "INSERT INTO group_change_requests (chat_id, from_group_id, to_group_id, requested_at, status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        (chat_id, from_group_id, to_group_id, requested_at),
    )
    conn.commit()
    return cursor.lastrowid


def get_pending_request_for_chat(conn: sqlite3.Connection, chat_id: int) -> GroupChangeRequestRow | None:
    row = conn.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM group_change_requests WHERE chat_id = ? AND status = 'pending'",
        (chat_id,),
    ).fetchone()
    return GroupChangeRequestRow(*row) if row else None


def get_group_change_request(conn: sqlite3.Connection, request_id: int) -> GroupChangeRequestRow | None:
    row = conn.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM group_change_requests WHERE id = ?", (request_id,)
    ).fetchone()
    return GroupChangeRequestRow(*row) if row else None


def decide_group_change_request(conn: sqlite3.Connection, request_id: int, status: str, decided_at: str) -> None:
    conn.execute(
        "UPDATE group_change_requests SET status = ?, decided_at = ? WHERE id = ?",
        (status, decided_at, request_id),
    )
    conn.commit()


def list_pending_requests_for_group(conn: sqlite3.Connection, group_id: int) -> list[GroupChangeRequestRow]:
    rows = conn.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM group_change_requests "
        "WHERE to_group_id = ? AND status = 'pending' ORDER BY requested_at",
        (group_id,),
    ).fetchall()
    return [GroupChangeRequestRow(*row) for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k change_request`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add group_change_requests table and CRUD"
```

---

## Task 4: `schedule_bot/elections.py` — pure rules and formatting

**Files:**
- Create: `schedule_bot/elections.py`
- Test: `tests/test_elections.py`

**Interfaces:**
- Produces: `elections.MIN_GROUP_SIZE_FOR_ELECTION = 3`, `elections.STAROSTA_CHANGE_COOLDOWN_DAYS = 14`, `elections.has_enough_members(member_count: int) -> bool`, `elections.cooldown_remaining(last_change_at: str | None, now: datetime) -> timedelta | None`, `elections.format_candidate_label(member: "storage.MemberRow") -> str`, `elections.format_starosta_welcome(group_name: str, assigned_by: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_elections.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from schedule_bot.elections import (
    cooldown_remaining,
    format_candidate_label,
    format_starosta_welcome,
    has_enough_members,
)
from schedule_bot.storage import MemberRow


def test_has_enough_members_requires_at_least_three():
    assert has_enough_members(1) is False
    assert has_enough_members(2) is False
    assert has_enough_members(3) is True
    assert has_enough_members(10) is True


def test_cooldown_remaining_none_when_never_changed():
    assert cooldown_remaining(None, datetime.now(timezone.utc)) is None


def test_cooldown_remaining_blocks_within_14_days():
    now = datetime.now(timezone.utc)
    last_change = (now - timedelta(days=5)).isoformat()
    remaining = cooldown_remaining(last_change, now)
    assert remaining is not None
    assert remaining > timedelta(days=0)


def test_cooldown_remaining_allows_after_14_days():
    now = datetime.now(timezone.utc)
    last_change = (now - timedelta(days=15)).isoformat()
    assert cooldown_remaining(last_change, now) is None


def test_cooldown_remaining_allows_exactly_at_boundary():
    now = datetime.now(timezone.utc)
    last_change = (now - timedelta(days=14, seconds=1)).isoformat()
    assert cooldown_remaining(last_change, now) is None


def _member(**overrides):
    defaults = dict(
        chat_id=111, group_id=1, telegram_username=None, display_name=None, subscribed=True, joined_at="t"
    )
    defaults.update(overrides)
    return MemberRow(**defaults)


def test_format_candidate_label_with_both_fields():
    member = _member(display_name="Ivan Ivanov", telegram_username="ivan")
    assert format_candidate_label(member) == "Ivan Ivanov (@ivan)"


def test_format_candidate_label_with_only_display_name():
    member = _member(display_name="Ivan Ivanov")
    assert format_candidate_label(member) == "Ivan Ivanov"


def test_format_candidate_label_with_only_username():
    member = _member(telegram_username="ivan")
    assert format_candidate_label(member) == "@ivan"


def test_format_candidate_label_with_neither():
    member = _member()
    assert format_candidate_label(member) == "Неизвестный"


def test_format_starosta_welcome_mentions_election():
    text = format_starosta_welcome("Б-Мв-101", "election")
    assert "Б-Мв-101" in text
    assert "голосования" in text


def test_format_starosta_welcome_mentions_admin():
    text = format_starosta_welcome("Б-Мв-101", "admin")
    assert "администратора" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_elections.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.elections'`

- [ ] **Step 3: Implement**

Create `schedule_bot/elections.py`:

```python
from datetime import datetime, timedelta

MIN_GROUP_SIZE_FOR_ELECTION = 3
STAROSTA_CHANGE_COOLDOWN_DAYS = 14


def has_enough_members(member_count: int) -> bool:
    return member_count >= MIN_GROUP_SIZE_FOR_ELECTION


def cooldown_remaining(last_change_at: str | None, now: datetime) -> timedelta | None:
    """Remaining cooldown before a new change-vote can start, or None if
    one can start right now (never changed before, or 14+ days have
    passed since last_change_at)."""
    if not last_change_at:
        return None
    last_change = datetime.fromisoformat(last_change_at)
    remaining = timedelta(days=STAROSTA_CHANGE_COOLDOWN_DAYS) - (now - last_change)
    return remaining if remaining > timedelta(0) else None


def format_candidate_label(member) -> str:
    if member.display_name and member.telegram_username:
        return f"{member.display_name} (@{member.telegram_username})"
    if member.display_name:
        return member.display_name
    if member.telegram_username:
        return f"@{member.telegram_username}"
    return "Неизвестный"


def format_starosta_welcome(group_name: str, assigned_by: str) -> str:
    reason = "по результатам голосования группы" if assigned_by == "election" else "решением администратора"
    return (
        f"🎉 Вы назначены старостой группы «{group_name}» ({reason}).\n\n"
        "Доступные вам команды:\n"
        "/starosta — статус и управление\n"
        "/votes — открытые голосования группы\n"
        "(дополнительные привилегии старосты появятся позже)"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_elections.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/elections.py tests/test_elections.py
git commit -m "feat: add starosta election rules (min size, cooldown, labels)"
```

---

## Task 5: `/starosta` + first-nomination flow

**Files:**
- Create: `schedule_bot/starosta_commands.py`
- Test: `tests/test_starosta_commands.py`

**Interfaces:**
- Consumes: `storage.get_member`/`get_group`/`list_group_members`/`get_open_election`/`create_election`/`get_election`/`add_election_approval`/`count_election_approvals`/`close_election`/`update_group_starosta`/`set_group_last_starosta_change_at` (Tasks 1-3), `elections.has_enough_members`/`cooldown_remaining`/`format_candidate_label`/`format_starosta_welcome` (Task 4), `telegram_utils.send_with_retry`/`SEND_PACING_SECONDS` (existing).
- Produces: `starosta_commands.starosta_command`, `starosta_commands.nominate_self_callback`, `starosta_commands.starosta_change_callback`, `starosta_commands.election_approve_callback`, `starosta_commands.election_reject_callback`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_starosta_commands.py`:

```python
import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.starosta_commands import (
    election_approve_callback,
    election_reject_callback,
    nominate_self_callback,
    starosta_command,
)


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


def _add_members(conn, group_id, chat_ids):
    for chat_id in chat_ids:
        storage.upsert_member(conn, chat_id, group_id, joined_at="t")


def make_message_update(chat_id):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    context.bot.send_message = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_starosta_command_offers_nomination_button_when_none_and_enough_members(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    update = make_message_update(111)
    context = make_context(conn)

    await starosta_command(update, context)

    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_starosta_command_refuses_nomination_with_too_few_members(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222])
    update = make_message_update(111)
    context = make_context(conn)

    await starosta_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "недостаточно участников" in text


@pytest.mark.asyncio
async def test_nominate_self_callback_creates_election_and_broadcasts(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    update = make_callback_update(111, "starosta:nominate_self")
    context = make_context(conn)

    await nominate_self_callback(update, context)

    election = storage.get_open_election(conn, group_id)
    assert election is not None
    assert election.kind == "initial"
    assert election.candidate_chat_id == 111
    assert context.bot.send_message.await_count == 3  # broadcast to all 3 members


@pytest.mark.asyncio
async def test_nominate_self_callback_refuses_when_already_open(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    storage.create_election(conn, group_id, "initial", 222, 222, "t")
    update = make_callback_update(111, "starosta:nominate_self")
    context = make_context(conn)

    await nominate_self_callback(update, context)

    assert context.bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_election_approve_second_vote_applies_result(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.add_election_approval(conn, election_id, 222, "t")

    update = make_callback_update(333, f"election:approve:{election_id}")
    context = make_context(conn)

    await election_approve_callback(update, context)

    group = storage.get_group(conn, group_id)
    assert group.starosta_chat_id == 111
    assert storage.get_election(conn, election_id).status == "approved"
    context.bot.send_message.assert_awaited_once()  # welcome message to the new starosta
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_election_approve_first_vote_does_not_apply_yet(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")

    update = make_callback_update(222, f"election:approve:{election_id}")
    context = make_context(conn)

    await election_approve_callback(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id is None
    assert storage.get_election(conn, election_id).status == "open"
    assert "1/2" in update.callback_query.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_election_approve_same_chat_id_twice_does_not_double_count(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")

    update = make_callback_update(222, f"election:approve:{election_id}")
    context = make_context(conn)

    await election_approve_callback(update, context)
    await election_approve_callback(update, context)

    assert storage.count_election_approvals(conn, election_id) == 1
    assert storage.get_group(conn, group_id).starosta_chat_id is None


@pytest.mark.asyncio
async def test_election_reject_does_not_write_anything(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")

    update = make_callback_update(222, f"election:reject:{election_id}")
    update.callback_query.answer = AsyncMock()
    context = make_context(conn)

    await election_reject_callback(update, context)

    assert storage.count_election_approvals(conn, election_id) == 0
    assert storage.get_election(conn, election_id).status == "open"
    update.callback_query.answer.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_starosta_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.starosta_commands'`

- [ ] **Step 3: Implement**

Create `schedule_bot/starosta_commands.py`:

```python
import asyncio
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import elections, storage
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, send_with_retry

logger = logging.getLogger(__name__)


async def starosta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)

    if storage.get_open_election(conn, group.id) is not None:
        await update.message.reply_text("По вашей группе уже идёт голосование. Посмотреть его можно командой /votes.")
        return

    if group.starosta_chat_id is None:
        member_count = len(storage.list_group_members(conn, group.id, subscribed_only=False))
        if not elections.has_enough_members(member_count):
            await update.message.reply_text(
                "В группе недостаточно участников для голосования, пригласите несколько человек и попробуйте снова."
            )
            return
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🙋 Я староста", callback_data="starosta:nominate_self")]]
        )
        await update.message.reply_text("В группе ещё нет старосты.", reply_markup=keyboard)
        return

    if group.starosta_chat_id == chat_id:
        await update.message.reply_text(f"Вы староста группы «{group.name}».")
        return

    remaining = elections.cooldown_remaining(group.last_starosta_change_at, datetime.now(timezone.utc))
    if remaining is not None:
        next_date = (datetime.now(timezone.utc) + remaining).date().isoformat()
        await update.message.reply_text(f"Старосту можно сменить не раньше {next_date}.")
        return

    member_count = len(storage.list_group_members(conn, group.id, subscribed_only=False))
    if not elections.has_enough_members(member_count):
        await update.message.reply_text(
            "В группе недостаточно участников для голосования, пригласите несколько человек и попробуйте снова."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔁 Староста изменился", callback_data="starosta:change")]]
    )
    await update.message.reply_text("В группе есть староста.", reply_markup=keyboard)


async def _broadcast_election(bot, conn, group, election_id: int, candidate_chat_id: int, kind: str) -> None:
    candidate = storage.get_member(conn, candidate_chat_id)
    label = elections.format_candidate_label(candidate)
    verb = "предлагает себя старостой" if kind == "initial" else "выдвинут кандидатом в старосты"
    text = f"{label} {verb}. Одобрить / Отклонить"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"election:approve:{election_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"election:reject:{election_id}"),
            ]
        ]
    )
    for member_chat_id in storage.list_group_members(conn, group.id, subscribed_only=False):
        try:
            await send_with_retry(
                lambda member_chat_id=member_chat_id: bot.send_message(
                    chat_id=member_chat_id, text=text, reply_markup=keyboard
                )
            )
        except Exception as error:
            logger.warning("starosta_commands: failed to notify chat_id=%s: %s", member_chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)


async def nominate_self_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)

    if group.starosta_chat_id is not None:
        await query.message.reply_text("В группе уже есть староста.")
        return
    if storage.get_open_election(conn, group.id) is not None:
        await query.message.reply_text("Голосование уже идёт.")
        return
    member_count = len(storage.list_group_members(conn, group.id, subscribed_only=False))
    if not elections.has_enough_members(member_count):
        await query.message.reply_text(
            "В группе недостаточно участников для голосования, пригласите несколько человек и попробуйте снова."
        )
        return

    election_id = storage.create_election(
        conn, group.id, "initial", chat_id, chat_id, datetime.now(timezone.utc).isoformat()
    )
    await _broadcast_election(context.bot, conn, group, election_id, chat_id, "initial")
    await query.message.reply_text("Голосование запущено.")


async def starosta_change_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Candidate picker + confirmation implemented in Task 6.
    query = update.callback_query
    await query.answer()
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)
    if group.starosta_chat_id is None:
        await query.message.reply_text("В группе больше нет старосты — используйте /starosta.")
        return
    if storage.get_open_election(conn, group.id) is not None:
        await query.message.reply_text("Голосование уже идёт.")
        return
    remaining = elections.cooldown_remaining(group.last_starosta_change_at, datetime.now(timezone.utc))
    if remaining is not None:
        await query.message.reply_text("Пока нельзя — кулдаун ещё не закончился.")
        return
    member_count = len(storage.list_group_members(conn, group.id, subscribed_only=False))
    if not elections.has_enough_members(member_count):
        await query.message.reply_text(
            "В группе недостаточно участников для голосования, пригласите несколько человек и попробуйте снова."
        )
        return
    from schedule_bot.starosta_candidates import build_candidate_keyboard

    await query.message.reply_text(
        "Выберите нового старосту:", reply_markup=build_candidate_keyboard(conn, group.id)
    )


async def _apply_election_result(bot, conn, election: "storage.ElectionRow") -> None:
    now = datetime.now(timezone.utc).isoformat()
    storage.close_election(conn, election.id, "approved", now)
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


async def election_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    election_id = int(query.data.split(":")[2])
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id

    election = storage.get_election(conn, election_id)
    if election is None or election.status != "open":
        await query.message.reply_text("Это голосование уже завершено.")
        return

    storage.add_election_approval(conn, election_id, chat_id, datetime.now(timezone.utc).isoformat())
    count = storage.count_election_approvals(conn, election_id)
    if count >= 2:
        await _apply_election_result(context.bot, conn, election)
    else:
        await query.message.reply_text(f"Голос засчитан ({count}/2).")


async def election_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer("Отклонения не учитываются, решает количество одобрений.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_starosta_commands.py -v`
Expected: PASS. (`starosta_change_callback`'s `from schedule_bot.starosta_candidates import ...` is deferred inside the function body specifically so the module imports cleanly even though `starosta_candidates` doesn't exist until Task 6 — none of this task's tests call `starosta_change_callback` itself, so that deferred import is never actually executed yet.)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/starosta_commands.py tests/test_starosta_commands.py
git commit -m "feat: /starosta command and first-nomination voting flow"
```

---

## Task 6: Change-of-starosta candidate picker + confirmation

**Files:**
- Create: `schedule_bot/starosta_candidates.py`
- Test: `tests/test_starosta_candidates.py`

**Interfaces:**
- Consumes: `storage.list_group_members`/`get_member`/`get_group`/`get_open_election`/`create_election` (Tasks 1-3), `elections.format_candidate_label` (Task 4), `starosta_commands._broadcast_election` (Task 5).
- Produces: `starosta_candidates.build_candidate_keyboard(conn, group_id, page=0) -> InlineKeyboardMarkup`, `starosta_candidates.CANDIDATES_PER_PAGE`, `starosta_candidates.page_callback`, `starosta_candidates.select_callback`, `starosta_candidates.abort_callback`, `starosta_candidates.confirm_callback`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_starosta_candidates.py`:

```python
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.starosta_candidates import (
    CANDIDATES_PER_PAGE,
    abort_callback,
    build_candidate_keyboard,
    confirm_callback,
    page_callback,
    select_callback,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_group_with_members(conn, count):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    for i in range(count):
        storage.upsert_member(conn, 100 + i, group_id, joined_at="t", subscribed=True)
        storage.update_member_profile(conn, 100 + i, telegram_username=f"user{i}", display_name=None)
    return group_id


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    context.bot.send_message = AsyncMock()
    return context


def test_build_candidate_keyboard_paginates(conn):
    group_id = _make_group_with_members(conn, CANDIDATES_PER_PAGE + 2)
    first_page = build_candidate_keyboard(conn, group_id, page=0)
    buttons = [b for row in first_page.inline_keyboard for b in row if b.callback_data.startswith("starcand:select:")]
    assert len(buttons) == CANDIDATES_PER_PAGE


@pytest.mark.asyncio
async def test_page_callback_updates_keyboard(conn):
    group_id = _make_group_with_members(conn, CANDIDATES_PER_PAGE + 2)
    storage.upsert_member(conn, 999, group_id, joined_at="t")
    update = make_callback_update(999, "starcand:page:1")
    context = make_context(conn)

    await page_callback(update, context)

    update.callback_query.message.edit_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_select_callback_shows_confirmation(conn):
    group_id = _make_group_with_members(conn, 3)
    update = make_callback_update(100, "starcand:select:101")
    context = make_context(conn)

    await select_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_abort_callback_replies_cancelled(conn):
    update = make_callback_update(100, "starcand:abort")
    context = make_context(conn)

    await abort_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_callback_creates_election_and_broadcasts(conn):
    group_id = _make_group_with_members(conn, 3)
    update = make_callback_update(100, "starcand:confirm:101")
    context = make_context(conn)

    await confirm_callback(update, context)

    election = storage.get_open_election(conn, group_id)
    assert election is not None
    assert election.kind == "change"
    assert election.candidate_chat_id == 101
    assert context.bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_confirm_callback_refuses_when_vote_already_started_meanwhile(conn):
    group_id = _make_group_with_members(conn, 3)
    storage.create_election(conn, group_id, "change", 102, 102, "t")
    update = make_callback_update(100, "starcand:confirm:101")
    context = make_context(conn)

    await confirm_callback(update, context)

    assert context.bot.send_message.await_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_starosta_candidates.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.starosta_candidates'`

- [ ] **Step 3: Implement**

Create `schedule_bot/starosta_candidates.py`:

```python
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import elections, storage

logger = logging.getLogger(__name__)

CANDIDATES_PER_PAGE = 8

_SELECT_PREFIX = "starcand:select:"
_PAGE_PREFIX = "starcand:page:"
_CONFIRM_PREFIX = "starcand:confirm:"
_ABORT_CALLBACK_DATA = "starcand:abort"


def build_candidate_keyboard(conn, group_id: int, page: int = 0) -> InlineKeyboardMarkup:
    chat_ids = sorted(storage.list_group_members(conn, group_id, subscribed_only=False))
    offset = page * CANDIDATES_PER_PAGE
    page_ids = chat_ids[offset : offset + CANDIDATES_PER_PAGE]

    rows = []
    for candidate_chat_id in page_ids:
        candidate = storage.get_member(conn, candidate_chat_id)
        label = elections.format_candidate_label(candidate)
        rows.append([InlineKeyboardButton(label, callback_data=f"{_SELECT_PREFIX}{candidate_chat_id}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{_PAGE_PREFIX}{page - 1}"))
    if offset + CANDIDATES_PER_PAGE < len(chat_ids):
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{_PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(rows)


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    await query.message.edit_reply_markup(reply_markup=build_candidate_keyboard(conn, member.group_id, page))


async def select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    candidate_chat_id = int(query.data.removeprefix(_SELECT_PREFIX))
    conn = context.bot_data["conn"]
    candidate = storage.get_member(conn, candidate_chat_id)
    label = elections.format_candidate_label(candidate)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"{_CONFIRM_PREFIX}{candidate_chat_id}"),
                InlineKeyboardButton("✖️ Отмена", callback_data=_ABORT_CALLBACK_DATA),
            ]
        ]
    )
    await query.message.reply_text(
        f"Запустить голосование за {label} в качестве старосты? Это разошлётся всей группе.",
        reply_markup=keyboard,
    )


async def abort_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Отменено.")


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from schedule_bot.starosta_commands import _broadcast_election

    query = update.callback_query
    await query.answer()
    candidate_chat_id = int(query.data.removeprefix(_CONFIRM_PREFIX))
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)

    if storage.get_open_election(conn, group.id) is not None:
        await query.message.reply_text("Голосование уже идёт.")
        return

    election_id = storage.create_election(
        conn, group.id, "change", chat_id, candidate_chat_id, datetime.now(timezone.utc).isoformat()
    )
    await _broadcast_election(context.bot, conn, group, election_id, candidate_chat_id, "change")
    await query.message.reply_text("Голосование запущено.")
```

This introduces a circular import at module load time between `starosta_commands` (which does `from schedule_bot.starosta_candidates import build_candidate_keyboard` inside `starosta_change_callback`, deferred to call time) and `starosta_candidates` (which does `from schedule_bot.starosta_commands import _broadcast_election` inside `confirm_callback`, also deferred to call time) — both imports are deliberately placed **inside the function body**, not at module top level, specifically to break the cycle. Do not move them to the top of either file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_starosta_candidates.py tests/test_starosta_commands.py -v`
Expected: PASS (full files, including the previously-skipped `starosta_change_callback` path)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/starosta_candidates.py tests/test_starosta_candidates.py
git commit -m "feat: candidate picker and confirmation for starosta change votes"
```

---

## Task 7: `/votes` — pull-based open-elections view

**Files:**
- Modify: `schedule_bot/starosta_commands.py`
- Test: `tests/test_starosta_commands.py`

**Interfaces:**
- Consumes: `storage.get_member`/`list_open_elections`/`get_member`/`count_election_approvals` (Tasks 1-3), `elections.format_candidate_label` (Task 4).
- Produces: `starosta_commands.votes_command`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_starosta_commands.py`:

```python
from schedule_bot.starosta_commands import votes_command


@pytest.mark.asyncio
async def test_votes_command_without_group_prompts_start(conn):
    update = make_message_update(111)
    context = make_context(conn)

    await votes_command(update, context)

    assert "/start" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_votes_command_reports_no_open_votes(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    update = make_message_update(111)
    context = make_context(conn)

    await votes_command(update, context)

    assert "Нет открытых голосований" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_votes_command_lists_open_election_with_working_button(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 222, 222, "t")
    update = make_message_update(111)
    context = make_context(conn)

    await votes_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text, kwargs = update.message.reply_text.call_args[0][0], update.message.reply_text.call_args[1]
    assert "0/2" in text
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data == f"election:approve:{election_id}" for b in buttons)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_starosta_commands.py -v -k votes`
Expected: FAIL with `ImportError: cannot import name 'votes_command'`

- [ ] **Step 3: Implement**

Append to `schedule_bot/starosta_commands.py`:

```python
async def votes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return

    open_elections = storage.list_open_elections(conn, member.group_id)
    if not open_elections:
        await update.message.reply_text("Нет открытых голосований.")
        return

    for election in open_elections:
        candidate = storage.get_member(conn, election.candidate_chat_id)
        count = storage.count_election_approvals(conn, election.id)
        kind_label = "Первое назначение старосты" if election.kind == "initial" else "Смена старосты"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Одобрить", callback_data=f"election:approve:{election.id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"election:reject:{election.id}"),
                ]
            ]
        )
        text = f"{kind_label}: {elections.format_candidate_label(candidate)} ({count}/2)"
        await update.message.reply_text(text, reply_markup=keyboard)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_starosta_commands.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/starosta_commands.py tests/test_starosta_commands.py
git commit -m "feat: /votes lists open elections so no one needs a reminder ping"
```

---

## Task 8: Admin override — `/set_starosta`, `/remove_starosta`

**Files:**
- Modify: `schedule_bot/bot.py`
- Test: `tests/test_bot_admin_commands.py`

**Interfaces:**
- Consumes: `bot._resolve_admin_group`/`is_admin` (sub-project 1 Task 10), `storage.cancel_open_elections_for_group`/`update_group_starosta`/`set_group_last_starosta_change_at` (Tasks 1-2), `elections.format_starosta_welcome` (Task 4).
- Produces: `bot.set_starosta_command`, `bot.remove_starosta_command`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bot_admin_commands.py`:

```python
from datetime import datetime, timezone

from schedule_bot.bot import remove_starosta_command, set_starosta_command


@pytest.mark.asyncio
async def test_set_starosta_assigns_and_notifies(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "111"]
    context.bot.send_message = AsyncMock()

    await set_starosta_command(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id == 111
    assert storage.get_group(conn, group_id).last_starosta_change_at is not None
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_set_starosta_cancels_open_election_and_resets_cooldown(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.set_group_last_starosta_change_at(conn, group_id, "2026-10-01T00:00:00+00:00")
    election_id = storage.create_election(conn, group_id, "change", 222, 333, "t")
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "111"]
    context.bot.send_message = AsyncMock()

    await set_starosta_command(update, context)

    assert storage.get_election(conn, election_id).status == "cancelled"
    assert storage.get_group(conn, group_id).last_starosta_change_at != "2026-10-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_set_starosta_rejects_non_admin(conn):
    group_id = _make_group(conn)
    update = make_update(12345)
    context = make_context(conn)
    context.args = [str(group_id), "111"]
    context.bot.send_message = AsyncMock()

    await set_starosta_command(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id is None
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_starosta_rejects_non_numeric_chat_id(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "not-a-number"]

    await set_starosta_command(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id is None


@pytest.mark.asyncio
async def test_remove_starosta_clears_and_resets_cooldown(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id)]

    await remove_starosta_command(update, context)

    group = storage.get_group(conn, group_id)
    assert group.starosta_chat_id is None
    assert group.last_starosta_change_at is not None


@pytest.mark.asyncio
async def test_remove_starosta_rejects_non_admin(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    update = make_update(12345)
    context = make_context(conn)
    context.args = [str(group_id)]

    await remove_starosta_command(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id == 111
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_bot_admin_commands.py -v -k starosta`
Expected: FAIL with `ImportError: cannot import name 'set_starosta_command'`

- [ ] **Step 3: Implement**

Append to `schedule_bot/bot.py` (add `from schedule_bot import elections` to the existing imports, and `from datetime import timezone` if not already imported — `datetime`/`timezone` are already imported in `bot.py` from sub-project 1):

```python
async def set_starosta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_starosta from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_starosta <group_id> <chat_id>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    if not context.args[1].isdigit():
        await update.message.reply_text("chat_id должен быть числом.")
        return
    new_starosta_chat_id = int(context.args[1])
    now = datetime.now(timezone.utc).isoformat()
    storage.cancel_open_elections_for_group(conn, group.id, now)
    storage.update_group_starosta(conn, group.id, new_starosta_chat_id)
    storage.set_group_last_starosta_change_at(conn, group.id, now)
    welcome = elections.format_starosta_welcome(group.name, "admin")
    try:
        await send_with_retry(lambda: context.bot.send_message(chat_id=new_starosta_chat_id, text=welcome))
    except Exception as error:
        logger.warning("bot: failed to notify new starosta chat_id=%s: %s", new_starosta_chat_id, error)
    await update.message.reply_text(f"Староста группы «{group.name}» назначен.")


async def remove_starosta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /remove_starosta from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /remove_starosta <group_id>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    now = datetime.now(timezone.utc).isoformat()
    storage.cancel_open_elections_for_group(conn, group.id, now)
    storage.update_group_starosta(conn, group.id, None)
    storage.set_group_last_starosta_change_at(conn, group.id, now)
    await update.message.reply_text(f"Староста группы «{group.name}» снят.")
```

Also add both to `ADMIN_COMMANDS`:

```python
ADMIN_COMMANDS = [
    ("set_pdf_source", "Изменить источник PDF группы: <group_id> <url_или_путь>"),
    ("set_send_time", "Изменить время ежедневной рассылки группы: <group_id> <HH:MM>"),
    ("set_check_interval", "Изменить интервал проверки группы: <group_id> <минуты>"),
    ("refresh", "Принудительно проверить и разослать обновления группы: <group_id>"),
    ("alert", "Разослать объявление всем пользователям бота: <текст>"),
    ("set_starosta", "Назначить старосту группы: <group_id> <chat_id>"),
    ("remove_starosta", "Снять старосту группы: <group_id>"),
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_bot_admin_commands.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/bot.py tests/test_bot_admin_commands.py
git commit -m "feat: /set_starosta and /remove_starosta admin overrides"
```

---

## Task 9: `/change_group` + approval flow

**Files:**
- Create: `schedule_bot/change_group_commands.py`
- Test: `tests/test_change_group_commands.py`

**Interfaces:**
- Consumes: `storage.get_member`/`get_pending_request_for_chat`/`list_all_groups`/`get_group`/`create_group_change_request`/`get_group_change_request`/`decide_group_change_request`/`upsert_member` (Tasks 1-3, sub-project 1), `group_commands.GROUPS_PER_PAGE` (sub-project 1 Task 7).
- Produces: `change_group_commands.change_group_command`, `change_group_commands.change_group_page_callback`, `change_group_commands.change_group_select_callback`, `change_group_commands.change_group_abort_callback`, `change_group_commands.change_group_confirm_callback`, `change_group_commands.change_request_approve_callback`, `change_group_commands.change_request_reject_callback`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_change_group_commands.py`:

```python
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.change_group_commands import (
    change_group_command,
    change_group_confirm_callback,
    change_group_select_callback,
    change_request_approve_callback,
    change_request_reject_callback,
)

ADMIN_CHAT_ID = 999


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_group(conn, name):
    return storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )


def make_message_update(chat_id):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn, "admin_chat_id": ADMIN_CHAT_ID}
    context.bot.send_message = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_change_group_command_shows_picker_excluding_current_group(conn):
    group_a = _make_group(conn, "A")
    _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_message_update(111)
    context = make_context(conn)

    await change_group_command(update, context)

    _, kwargs = update.message.reply_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "A" not in texts
    assert "B" in texts


@pytest.mark.asyncio
async def test_change_group_command_refuses_second_pending_request(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_message_update(111)
    context = make_context(conn)

    await change_group_command(update, context)

    assert "уже есть заявка" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_select_callback_shows_confirmation(conn):
    group_b = _make_group(conn, "B")
    update = make_callback_update(111, f"chggrp:select:{group_b}")
    context = make_context(conn)

    await change_group_select_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_confirm_callback_creates_request_and_notifies_admin_only_without_starosta(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    request = storage.get_pending_request_for_chat(conn, 111)
    assert request is not None
    assert request.to_group_id == group_b
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.await_args.kwargs["chat_id"] == ADMIN_CHAT_ID


@pytest.mark.asyncio
async def test_confirm_callback_notifies_target_starosta_too(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    notified_chat_ids = {call.kwargs["chat_id"] for call in context.bot.send_message.await_args_list}
    assert notified_chat_ids == {ADMIN_CHAT_ID, 555}


@pytest.mark.asyncio
async def test_confirm_callback_refuses_second_pending_request(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_request_approve_by_admin_moves_member(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b
    assert storage.get_group_change_request(conn, request_id).status == "approved"
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_change_request_approve_by_target_starosta_moves_member(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(555, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b


@pytest.mark.asyncio
async def test_change_request_approve_rejects_unauthorized_caller(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(777, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_a
    assert storage.get_group_change_request(conn, request_id).status == "pending"


@pytest.mark.asyncio
async def test_change_request_reject_by_admin_keeps_member_in_place(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:reject:{request_id}")
    context = make_context(conn)

    await change_request_reject_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_a
    assert storage.get_group_change_request(conn, request_id).status == "rejected"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_change_group_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.change_group_commands'`

- [ ] **Step 3: Implement**

Create `schedule_bot/change_group_commands.py`:

```python
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.group_commands import GROUPS_PER_PAGE
from schedule_bot.telegram_utils import send_with_retry

logger = logging.getLogger(__name__)

_SELECT_PREFIX = "chggrp:select:"
_PAGE_PREFIX = "chggrp:page:"
_CONFIRM_PREFIX = "chgconfirm:"
_ABORT_CALLBACK_DATA = "chgabort"


def build_change_group_keyboard(conn, exclude_group_id: int, page: int = 0) -> InlineKeyboardMarkup:
    candidates = [g for g in storage.list_all_groups(conn) if g.id != exclude_group_id]
    offset = page * GROUPS_PER_PAGE
    page_groups = candidates[offset : offset + GROUPS_PER_PAGE]

    rows = [[InlineKeyboardButton(g.name, callback_data=f"{_SELECT_PREFIX}{g.id}")] for g in page_groups]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{_PAGE_PREFIX}{page - 1}"))
    if offset + GROUPS_PER_PAGE < len(candidates):
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{_PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(rows)


async def change_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return
    if storage.get_pending_request_for_chat(conn, chat_id) is not None:
        await update.message.reply_text("У вас уже есть заявка на рассмотрении.")
        return
    keyboard = build_change_group_keyboard(conn, member.group_id)
    await update.message.reply_text("Выберите новую группу:", reply_markup=keyboard)


async def change_group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    await query.message.edit_reply_markup(reply_markup=build_change_group_keyboard(conn, member.group_id, page))


async def change_group_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    target_group_id = int(query.data.removeprefix(_SELECT_PREFIX))
    conn = context.bot_data["conn"]
    target_group = storage.get_group(conn, target_group_id)
    if target_group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"{_CONFIRM_PREFIX}{target_group_id}"),
                InlineKeyboardButton("✖️ Отмена", callback_data=_ABORT_CALLBACK_DATA),
            ]
        ]
    )
    await query.message.reply_text(
        f"Подать заявку на переход в группу «{target_group.name}»?", reply_markup=keyboard
    )


async def change_group_abort_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Отменено.")


async def _notify_approvers(bot, conn, target_group, request_id: int, requester_chat_id: int, admin_chat_id: int) -> None:
    requester = storage.get_member(conn, requester_chat_id)
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
            logger.warning("change_group_commands: failed to notify chat_id=%s: %s", recipient_chat_id, error)


async def change_group_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    target_group_id = int(query.data.removeprefix(_CONFIRM_PREFIX))
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return
    if storage.get_pending_request_for_chat(conn, chat_id) is not None:
        await query.message.reply_text("У вас уже есть заявка на рассмотрении.")
        return
    target_group = storage.get_group(conn, target_group_id)
    if target_group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return

    request_id = storage.create_group_change_request(
        conn, chat_id, member.group_id, target_group_id, datetime.now(timezone.utc).isoformat()
    )
    await _notify_approvers(context.bot, conn, target_group, request_id, chat_id, context.bot_data["admin_chat_id"])
    await query.message.reply_text("Заявка отправлена, ожидайте решения.")


def _can_decide(caller_chat_id: int, target_group, admin_chat_id: int) -> bool:
    is_admin_caller = caller_chat_id == admin_chat_id
    is_target_starosta = target_group is not None and target_group.starosta_chat_id == caller_chat_id
    return is_admin_caller or is_target_starosta


async def change_request_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":")[2])
    conn = context.bot_data["conn"]
    caller_chat_id = update.effective_chat.id
    request = storage.get_group_change_request(conn, request_id)
    if request is None or request.status != "pending":
        await query.message.reply_text("Эта заявка уже обработана.")
        return
    target_group = storage.get_group(conn, request.to_group_id)
    if not _can_decide(caller_chat_id, target_group, context.bot_data["admin_chat_id"]):
        await query.message.reply_text("Недостаточно прав для решения по этой заявке.")
        return

    now = datetime.now(timezone.utc).isoformat()
    storage.decide_group_change_request(conn, request_id, "approved", now)
    storage.upsert_member(conn, request.chat_id, request.to_group_id, joined_at=now, subscribed=True)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=request.chat_id,
                text=f"Ваша заявка на переход в группу «{target_group.name}» одобрена.",
            )
        )
    except Exception as error:
        logger.warning("change_group_commands: failed to notify requester chat_id=%s: %s", request.chat_id, error)
    await query.message.reply_text("Заявка одобрена.")


async def change_request_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":")[2])
    conn = context.bot_data["conn"]
    caller_chat_id = update.effective_chat.id
    request = storage.get_group_change_request(conn, request_id)
    if request is None or request.status != "pending":
        await query.message.reply_text("Эта заявка уже обработана.")
        return
    target_group = storage.get_group(conn, request.to_group_id)
    if not _can_decide(caller_chat_id, target_group, context.bot_data["admin_chat_id"]):
        await query.message.reply_text("Недостаточно прав для решения по этой заявке.")
        return

    now = datetime.now(timezone.utc).isoformat()
    storage.decide_group_change_request(conn, request_id, "rejected", now)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=request.chat_id,
                text=f"Ваша заявка на переход в группу «{target_group.name}» отклонена.",
            )
        )
    except Exception as error:
        logger.warning("change_group_commands: failed to notify requester chat_id=%s: %s", request.chat_id, error)
    await query.message.reply_text("Заявка отклонена.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_change_group_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/change_group_commands.py tests/test_change_group_commands.py
git commit -m "feat: /change_group with starosta/admin approval"
```

---

## Task 10: `main.py` wiring

**Files:**
- Modify: `main.py`
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: everything from Tasks 1-9.
- Produces: `main.build_application` registers `/starosta`, `/votes`, `/change_group`, `/set_starosta`, `/remove_starosta`, and every new `CallbackQueryHandler`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main_wiring.py`:

```python
def test_build_application_registers_starosta_and_change_group_handlers(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)
    assert {"starosta", "votes", "change_group", "set_starosta", "remove_starosta"}.issubset(command_names)

    callback_patterns = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "pattern") and handler.pattern is not None:
                callback_patterns.add(handler.pattern.pattern)
    assert r"^starosta:nominate_self$" in callback_patterns
    assert r"^election:approve:\d+$" in callback_patterns
    assert r"^chgreq:approve:\d+$" in callback_patterns

    application.bot_data["_scheduler"].shutdown(wait=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v -k starosta_and_change_group`
Expected: FAIL — `starosta`/`votes`/`change_group`/`set_starosta`/`remove_starosta` not in `command_names`.

- [ ] **Step 3: Implement**

In `main.py`, add imports:

```python
from schedule_bot.bot import remove_starosta_command, set_starosta_command
from schedule_bot.change_group_commands import (
    change_group_abort_callback,
    change_group_command,
    change_group_confirm_callback,
    change_group_page_callback,
    change_group_select_callback,
    change_request_approve_callback,
    change_request_reject_callback,
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
```

In `build_application`, after the existing `application.add_handler(...)` block (right before `scheduler_holder["scheduler"] = build_scheduler(...)`), add:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: PASS (full file)

Then run the full suite:

Run: `./.venv/bin/python -m pytest -v`
Expected: PASS (every test in the repository, across sub-projects 1 and 2)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "feat: wire starosta and change-group handlers into main.py"
```
