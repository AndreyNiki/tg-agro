# Ядро мультигрупповости — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bot's single-global-group model with a `groups`/`members` data model supporting any number of groups, each user picking and keeping one group, per-group sync/scheduling, and automatic PDF-link rediscovery on a 404 (the motivating bug).

**Architecture:** `storage.py` grows `groups`/`members` tables plus a one-time migration from the legacy `settings`/`subscribers` schema. `sync.py`'s `check_and_apply_diff` takes a `GroupRow` instead of bare strings and gains a 404-reconnect step backed by a new `group_links.py` (regex HTML link resolution, reusing the existing regex-over-raw-HTML style already used in `weektype.py`). `scheduler.py` runs one hourly+daily job pair per group instead of one pair total. A new `group_commands.py` owns `/start`'s group picker (paginated inline keyboard, reusing the `day:`-callback pattern from `bot.py`), the group-creation `ConversationHandler`, and `/subscribe`/`/unsubscribe`. `bot.py`'s existing commands (`/schedule`, day callback, admin `/set_*`, `/alert`, `/refresh`) are updated to resolve a `group_id` instead of reading one global setting.

**Tech Stack:** Python 3.12, `python-telegram-bot` 21.6 (`ConversationHandler`, `CallbackQueryHandler`), `APScheduler` 3.10, `sqlite3` stdlib, `pytest`/`pytest-asyncio`, regex-based HTML parsing (no new dependency — matches the existing `weektype.py` convention).

**Spec:** `docs/superpowers/specs/2026-10-08-multi-group-core-design.md`

## Global Constraints

- `weektype_url`/`WeekFormula`/`weekly_weektype_refresh_job` stay global (bot-wide), per the spec's corrected data model — do not add per-group week-formula state.
- `send_time`, `check_interval_minutes`, `timezone`, `last_sync_failed_at` move to `groups` and become per-group.
- Only the global admin (`admin_chat_id`) may create/change group sync settings via `/set_*` commands; any user may create a brand-new group via the `/start` picker's "Создать группу" button.
- One `chat_id` maps to at most one `group_id` at a time (`members.chat_id` stays a PRIMARY KEY, mirroring the old `subscribers` invariant) — changing an already-attached group is explicitly out of scope for this plan (next sub-project).
- The 404-reconnect (`group_links.resolve_group_pdf_source`) must only be attempted once per sync call, not once per retry attempt, to avoid hammering the source page.
- Every per-recipient send loop (daily send, `/alert`, sync change-notification) must keep isolating failures per `chat_id` — this already-established pattern must not regress.

## Review Focus

- Creating a group whose name already exists (`groups.name` is `UNIQUE`) must produce a clean user-facing message, not an unhandled `sqlite3.IntegrityError` inside the `ConversationHandler`.
- A source page with several `.pdf` links where more than one textually contains the group name (e.g. "1 курс БЭ, БМ оз.pdf" matches both "БЭ" and "БМ") must deterministically resolve to the first match, not error or pick randomly.
- `/start` from a chat that already has a `members` row must show its current status, not silently re-show the group picker or reassign the group.
- The 404-reconnect finding a link that itself is also unreachable (institute renamed the file again, or broke the page) must still end the sync attempt within the existing `MAX_SYNC_ATTEMPTS` budget, not retry indefinitely.
- An admin running `/set_send_time`/`/set_check_interval`/`/set_pdf_source`/`/alert`/`/refresh` with a `group_id` that doesn't exist must get a clear rejection, not a crash or a silent no-op.

---

## Task 1: `groups` table and CRUD

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `storage.GroupRow` (dataclass: `id, name, source_page_url, pdf_source_url, send_time, check_interval_minutes, timezone, last_sync_failed_at, created_at`), `storage.create_group(conn, *, name, source_page_url, pdf_source_url, send_time, check_interval_minutes, tz_name) -> int`, `storage.get_group(conn, group_id) -> GroupRow | None`, `storage.get_group_by_name(conn, name) -> GroupRow | None`, `storage.list_groups(conn, *, limit, offset) -> list[GroupRow]`, `storage.list_all_groups(conn) -> list[GroupRow]`, `storage.count_groups(conn) -> int`, `storage.update_group_pdf_source_url(conn, group_id, pdf_source_url)`, `storage.update_group_send_time(conn, group_id, send_time)`, `storage.update_group_check_interval(conn, group_id, check_interval_minutes)`, `storage.set_group_last_sync_failed_at(conn, group_id, value)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def test_create_group_returns_new_group_with_defaults(conn):
    group_id = storage.create_group(
        conn,
        name="Б-Мв-101",
        source_page_url="https://example.com/schedules",
        pdf_source_url="https://example.com/101.pdf",
        send_time="09:00",
        check_interval_minutes=60,
        tz_name="Europe/Moscow",
    )
    group = storage.get_group(conn, group_id)
    assert group.name == "Б-Мв-101"
    assert group.pdf_source_url == "https://example.com/101.pdf"
    assert group.send_time == "09:00"
    assert group.check_interval_minutes == 60
    assert group.timezone == "Europe/Moscow"
    assert group.last_sync_failed_at is None


def test_create_group_rejects_duplicate_name(conn):
    storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_group(
            conn, name="Б-Мв-101", source_page_url="u2", pdf_source_url="p2",
            send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
        )


def test_get_group_returns_none_for_missing_id(conn):
    assert storage.get_group(conn, 999) is None


def test_get_group_by_name_returns_none_when_absent(conn):
    assert storage.get_group_by_name(conn, "Nope") is None


def test_list_groups_orders_alphabetically_and_paginates(conn):
    for name in ["В-группа", "А-группа", "Б-группа"]:
        storage.create_group(
            conn, name=name, source_page_url="u", pdf_source_url="p",
            send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
        )
    page1 = storage.list_groups(conn, limit=2, offset=0)
    page2 = storage.list_groups(conn, limit=2, offset=2)
    assert [g.name for g in page1] == ["А-группа", "Б-группа"]
    assert [g.name for g in page2] == ["В-группа"]
    assert storage.count_groups(conn) == 3


def test_list_all_groups_returns_every_group_unpaginated(conn):
    for i in range(10):
        storage.create_group(
            conn, name=f"Группа-{i}", source_page_url="u", pdf_source_url="p",
            send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
        )
    assert len(storage.list_all_groups(conn)) == 10


def test_update_group_pdf_source_url(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="old.pdf",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.update_group_pdf_source_url(conn, group_id, "new.pdf")
    assert storage.get_group(conn, group_id).pdf_source_url == "new.pdf"


def test_update_group_send_time_and_check_interval(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.update_group_send_time(conn, group_id, "07:30")
    storage.update_group_check_interval(conn, group_id, 30)
    group = storage.get_group(conn, group_id)
    assert group.send_time == "07:30"
    assert group.check_interval_minutes == 30


def test_set_group_last_sync_failed_at(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.set_group_last_sync_failed_at(conn, group_id, "2026-10-08T00:00:00+00:00")
    assert storage.get_group(conn, group_id).last_sync_failed_at == "2026-10-08T00:00:00+00:00"
    storage.set_group_last_sync_failed_at(conn, group_id, "")
    assert storage.get_group(conn, group_id).last_sync_failed_at == ""
```

Also add `import sqlite3` is already present at top of the test file — confirm, it is (`import sqlite3` on line 1).

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k group`
Expected: FAIL with `AttributeError: module 'schedule_bot.storage' has no attribute 'create_group'`

- [ ] **Step 3: Implement**

In `schedule_bot/storage.py`, add `from datetime import datetime, timezone` to the imports at the top (replacing the plain `import sqlite3` block with both lines), then add the `groups` table to `init_db`'s `executescript` (insert right after the existing `settings` table definition, before the closing `"""`):

```python
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            source_page_url TEXT NOT NULL,
            pdf_source_url TEXT NOT NULL,
            send_time TEXT NOT NULL,
            check_interval_minutes INTEGER NOT NULL,
            timezone TEXT NOT NULL,
            last_sync_failed_at TEXT,
            created_at TEXT NOT NULL
        );
```

Then append to the bottom of `storage.py`:

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


_GROUP_COLUMNS = (
    "id, name, source_page_url, pdf_source_url, send_time, "
    "check_interval_minutes, timezone, last_sync_failed_at, created_at"
)


def create_group(
    conn: sqlite3.Connection,
    *,
    name: str,
    source_page_url: str,
    pdf_source_url: str,
    send_time: str,
    check_interval_minutes: int,
    tz_name: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO groups (name, source_page_url, pdf_source_url, send_time, "
        "check_interval_minutes, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            source_page_url,
            pdf_source_url,
            send_time,
            check_interval_minutes,
            tz_name,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_group(conn: sqlite3.Connection, group_id: int) -> GroupRow | None:
    row = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    return GroupRow(*row) if row else None


def get_group_by_name(conn: sqlite3.Connection, name: str) -> GroupRow | None:
    row = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE name = ?", (name,)
    ).fetchone()
    return GroupRow(*row) if row else None


def list_groups(conn: sqlite3.Connection, *, limit: int, offset: int) -> list[GroupRow]:
    rows = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups ORDER BY name LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [GroupRow(*row) for row in rows]


def list_all_groups(conn: sqlite3.Connection) -> list[GroupRow]:
    rows = conn.execute(f"SELECT {_GROUP_COLUMNS} FROM groups ORDER BY name").fetchall()
    return [GroupRow(*row) for row in rows]


def count_groups(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]


def update_group_pdf_source_url(conn: sqlite3.Connection, group_id: int, pdf_source_url: str) -> None:
    conn.execute("UPDATE groups SET pdf_source_url = ? WHERE id = ?", (pdf_source_url, group_id))
    conn.commit()


def update_group_send_time(conn: sqlite3.Connection, group_id: int, send_time: str) -> None:
    conn.execute("UPDATE groups SET send_time = ? WHERE id = ?", (send_time, group_id))
    conn.commit()


def update_group_check_interval(conn: sqlite3.Connection, group_id: int, check_interval_minutes: int) -> None:
    conn.execute(
        "UPDATE groups SET check_interval_minutes = ? WHERE id = ?",
        (check_interval_minutes, group_id),
    )
    conn.commit()


def set_group_last_sync_failed_at(conn: sqlite3.Connection, group_id: int, value: str) -> None:
    conn.execute("UPDATE groups SET last_sync_failed_at = ? WHERE id = ?", (value, group_id))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k group`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add groups table and CRUD to storage"
```

---

## Task 2: `members` table and CRUD

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `storage.create_group` (Task 1), used by tests to get a `group_id` to attach members to.
- Produces: `storage.MemberRow` (dataclass: `chat_id, group_id, telegram_username, display_name, subscribed, joined_at`), `storage.upsert_member(conn, chat_id, group_id, *, joined_at, subscribed=True)`, `storage.get_member(conn, chat_id) -> MemberRow | None`, `storage.set_member_subscribed(conn, chat_id, subscribed)`, `storage.update_member_profile(conn, chat_id, *, telegram_username, display_name)`, `storage.list_group_members(conn, group_id, *, subscribed_only=True) -> list[int]`, `storage.list_members_missing_profile(conn) -> list[int]`, `storage.list_all_member_chat_ids(conn) -> list[int]` (every chat_id in `members` across every group — backs the global `/alert`, not a per-group listing).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def _make_group(conn, name="Б-Мв-101"):
    return storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )


def test_upsert_member_creates_new_member(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="2026-10-08T00:00:00")
    member = storage.get_member(conn, 111)
    assert member.group_id == group_id
    assert member.subscribed is True
    assert member.telegram_username is None


def test_upsert_member_moves_existing_chat_to_new_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="2026-10-08T00:00:00")
    storage.upsert_member(conn, 111, group_b, joined_at="2026-10-08T00:00:01")
    assert storage.get_member(conn, 111).group_id == group_b


def test_get_member_returns_none_when_absent(conn):
    assert storage.get_member(conn, 111) is None


def test_set_member_subscribed_toggles_without_losing_group(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="2026-10-08T00:00:00")
    storage.set_member_subscribed(conn, 111, False)
    member = storage.get_member(conn, 111)
    assert member.subscribed is False
    assert member.group_id == group_id
    storage.set_member_subscribed(conn, 111, True)
    assert storage.get_member(conn, 111).subscribed is True


def test_update_member_profile_sets_username_and_display_name(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="2026-10-08T00:00:00")
    storage.update_member_profile(conn, 111, telegram_username="ivan", display_name="Ivan Ivanov")
    member = storage.get_member(conn, 111)
    assert member.telegram_username == "ivan"
    assert member.display_name == "Ivan Ivanov"


def test_list_group_members_defaults_to_subscribed_only(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=True)
    storage.upsert_member(conn, 222, group_id, joined_at="t", subscribed=False)
    assert storage.list_group_members(conn, group_id) == [111]
    assert sorted(storage.list_group_members(conn, group_id, subscribed_only=False)) == [111, 222]


def test_list_members_missing_profile_returns_chats_without_username_or_name(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.update_member_profile(conn, 222, telegram_username="ivan", display_name="Ivan")
    assert storage.list_members_missing_profile(conn) == [111]


def test_list_all_member_chat_ids_spans_every_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 222, group_b, joined_at="t", subscribed=False)
    assert sorted(storage.list_all_member_chat_ids(conn)) == [111, 222]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k member`
Expected: FAIL with `AttributeError: module 'schedule_bot.storage' has no attribute 'upsert_member'`

- [ ] **Step 3: Implement**

Add the `members` table to `init_db`'s `executescript`, right after the `groups` table block added in Task 1:

```python
        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            telegram_username TEXT,
            display_name TEXT,
            subscribed INTEGER NOT NULL DEFAULT 1,
            joined_at TEXT NOT NULL
        );
```

Append to `storage.py`:

```python
@dataclass
class MemberRow:
    chat_id: int
    group_id: int
    telegram_username: str | None
    display_name: str | None
    subscribed: bool
    joined_at: str


_MEMBER_COLUMNS = "chat_id, group_id, telegram_username, display_name, subscribed, joined_at"


def _member_row_from_tuple(row) -> MemberRow:
    chat_id, group_id, username, display_name, subscribed, joined_at = row
    return MemberRow(chat_id, group_id, username, display_name, bool(subscribed), joined_at)


def upsert_member(
    conn: sqlite3.Connection,
    chat_id: int,
    group_id: int,
    *,
    joined_at: str,
    subscribed: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO members (chat_id, group_id, telegram_username, display_name, subscribed, joined_at) "
        "VALUES (?, ?, NULL, NULL, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET group_id = excluded.group_id, subscribed = excluded.subscribed",
        (chat_id, group_id, 1 if subscribed else 0, joined_at),
    )
    conn.commit()


def get_member(conn: sqlite3.Connection, chat_id: int) -> MemberRow | None:
    row = conn.execute(
        f"SELECT {_MEMBER_COLUMNS} FROM members WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return _member_row_from_tuple(row) if row else None


def set_member_subscribed(conn: sqlite3.Connection, chat_id: int, subscribed: bool) -> None:
    conn.execute(
        "UPDATE members SET subscribed = ? WHERE chat_id = ?", (1 if subscribed else 0, chat_id)
    )
    conn.commit()


def update_member_profile(
    conn: sqlite3.Connection, chat_id: int, *, telegram_username: str | None, display_name: str | None
) -> None:
    conn.execute(
        "UPDATE members SET telegram_username = ?, display_name = ? WHERE chat_id = ?",
        (telegram_username, display_name, chat_id),
    )
    conn.commit()


def list_group_members(conn: sqlite3.Connection, group_id: int, *, subscribed_only: bool = True) -> list[int]:
    if subscribed_only:
        rows = conn.execute(
            "SELECT chat_id FROM members WHERE group_id = ? AND subscribed = 1", (group_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT chat_id FROM members WHERE group_id = ?", (group_id,)).fetchall()
    return [row[0] for row in rows]


def list_members_missing_profile(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT chat_id FROM members WHERE telegram_username IS NULL AND display_name IS NULL"
    ).fetchall()
    return [row[0] for row in rows]


def list_all_member_chat_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT chat_id FROM members").fetchall()
    return [row[0] for row in rows]
```

Note: `upsert_member`'s `ON CONFLICT` deliberately does **not** overwrite `telegram_username`/`display_name` — those are only ever changed by `update_member_profile`, called separately by every handler that has a live `Update` (Task 10), so re-selecting a group never wipes a previously-learned profile.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k member`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add members table and CRUD to storage"
```

---

## Task 3: `schedule_versions` moves from `group_name` to `group_id`

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py` (replace the four `schedule_versions`-related tests)

**Interfaces:**
- Consumes: `storage.create_group` (Task 1).
- Produces: `storage.ScheduleVersionRow` (dataclass: `id, group_id, payload_json, pdf_cache_path, parsed_at, status`), `storage.get_active_version(conn, group_id) -> ScheduleVersionRow | None`, `storage.insert_new_version(conn, group_id, payload_json, pdf_cache_path, parsed_at) -> int`. The old `group_name`-keyed signatures are removed — this is a breaking change for every caller, resolved in Tasks 6/11.

- [ ] **Step 1: Replace the failing tests**

In `tests/test_storage.py`, delete `test_get_active_version_returns_none_when_empty`, `test_insert_new_version_becomes_active_and_supersedes_old`, and `test_versions_are_scoped_by_group_name` (lines 59-86 of the current file), replacing them with:

```python
def test_get_active_version_returns_none_when_empty(conn):
    group_id = _make_group(conn)
    assert storage.get_active_version(conn, group_id) is None


def test_insert_new_version_becomes_active_and_supersedes_old(conn):
    group_id = _make_group(conn)
    first_id = storage.insert_new_version(
        conn, group_id, '{"a": 1}', "/data/pdf_cache/v1.pdf", "2026-09-01T09:00:00"
    )
    second_id = storage.insert_new_version(
        conn, group_id, '{"a": 2}', "/data/pdf_cache/v2.pdf", "2026-09-08T09:00:00"
    )
    active = storage.get_active_version(conn, group_id)
    assert active.id == second_id
    assert active.payload_json == '{"a": 2}'
    assert active.status == "active"

    all_rows = conn.execute(
        "SELECT id, status FROM schedule_versions WHERE group_id = ? ORDER BY id", (group_id,)
    ).fetchall()
    assert all_rows == [(first_id, "superseded"), (second_id, "active")]


def test_versions_are_scoped_by_group_id(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.insert_new_version(conn, group_a, '{"a": 1}', "/v1.pdf", "2026-09-01T09:00:00")
    storage.insert_new_version(conn, group_b, '{"b": 1}', "/v2.pdf", "2026-09-01T09:00:00")
    active = storage.get_active_version(conn, group_a)
    assert active.payload_json == '{"a": 1}'
```

(`_make_group` is the helper added in Task 2 — if executing Task 3 before Task 2 in a different order, add it here too; the plan assumes tasks run in order, so it already exists.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v -k version`
Expected: FAIL — `get_active_version()`/`insert_new_version()` still expect a `group_name` string, so e.g. `test_versions_are_scoped_by_group_id` fails with a type/lookup mismatch (groups table has no row named with an int).

- [ ] **Step 3: Implement**

In `schedule_bot/storage.py`, change the `schedule_versions` table in `init_db`'s script from:

```python
        CREATE TABLE IF NOT EXISTS schedule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            pdf_cache_path TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            status TEXT NOT NULL
        );
```

to:

```python
        CREATE TABLE IF NOT EXISTS schedule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            payload_json TEXT NOT NULL,
            pdf_cache_path TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            status TEXT NOT NULL
        );
```

(A pre-existing production database already has a `schedule_versions` table with the old `group_name` column — `CREATE TABLE IF NOT EXISTS` is a no-op against it, so that table only gets the new shape on a *fresh* database. Task 4's migration adds the `group_id` column and backfills it for upgrades of an existing database.)

Replace the `ScheduleVersionRow` dataclass and its two functions (currently lines 89-125) with:

```python
@dataclass
class ScheduleVersionRow:
    id: int
    group_id: int
    payload_json: str
    pdf_cache_path: str
    parsed_at: str
    status: str


def get_active_version(conn: sqlite3.Connection, group_id: int) -> ScheduleVersionRow | None:
    row = conn.execute(
        "SELECT id, group_id, payload_json, pdf_cache_path, parsed_at, status "
        "FROM schedule_versions WHERE group_id = ? AND status = 'active'",
        (group_id,),
    ).fetchone()
    return ScheduleVersionRow(*row) if row else None


def insert_new_version(
    conn: sqlite3.Connection,
    group_id: int,
    payload_json: str,
    pdf_cache_path: str,
    parsed_at: str,
) -> int:
    conn.execute(
        "UPDATE schedule_versions SET status = 'superseded' WHERE group_id = ? AND status = 'active'",
        (group_id,),
    )
    cursor = conn.execute(
        "INSERT INTO schedule_versions (group_id, payload_json, pdf_cache_path, parsed_at, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (group_id, payload_json, pdf_cache_path, parsed_at),
    )
    conn.commit()
    return cursor.lastrowid
```

Also delete the now-obsolete `add_subscriber`, `remove_subscriber`, `list_subscribers`, `update_subscribers_group` functions (lines 54-86) and their tests `test_subscriber_lifecycle`, `test_add_subscriber_is_idempotent`, `test_update_subscribers_group_migrates_matching_rows` (lines 31-56 of the current `tests/test_storage.py`) — they're superseded by the `members`-based functions from Task 2. Keep the `subscribers` table definition itself in `init_db` (it stays as the read-only source table for Task 4's migration), just drop the Python functions that wrote to it going forward.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "refactor: key schedule_versions by group_id, drop legacy subscriber functions"
```

---

## Task 4: Legacy migration + `LEGACY_GROUP_SOURCE_PAGE_URL` config

**Files:**
- Modify: `schedule_bot/storage.py`
- Modify: `schedule_bot/config.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `storage.create_group`, `storage.upsert_member` (Tasks 1-2); reads legacy `settings`/`subscribers` tables and the old `schedule_versions.group_name` column (still physically present on a pre-existing database, per Task 3).
- Produces: `storage.migrate_legacy_single_group(conn, legacy_source_page_url: str | None) -> int | None` (returns new `group_id`, or `None` if nothing to migrate); `Config.legacy_group_source_page_url: str | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_storage.py`:

```python
def _seed_legacy_single_group_db(conn, *, with_schedule_version=False):
    """Recreates the pre-multi-group state this migration must handle:
    a `settings`-driven single group plus old-shape `subscribers` rows,
    and optionally an old-shape `schedule_versions` row that predates the
    group_id column (simulating an un-migrated production database)."""
    storage.set_setting(conn, "group_name", "Б-Мв-101")
    storage.set_setting(conn, "pdf_source", "https://example.com/101.pdf")
    storage.set_setting(conn, "daily_send_time", "09:00")
    storage.set_setting(conn, "check_interval_minutes", "60")
    storage.set_setting(conn, "timezone", "Europe/Moscow")
    conn.execute(
        "INSERT INTO subscribers (chat_id, group_name, joined_at) VALUES (?, ?, ?)",
        (111, "Б-Мв-101", "2026-09-16T00:00:00"),
    )
    conn.execute(
        "INSERT INTO subscribers (chat_id, group_name, joined_at) VALUES (?, ?, ?)",
        (222, "Б-Мв-101", "2026-09-16T00:00:00"),
    )
    conn.commit()
    if with_schedule_version:
        # Simulate a database created before Task 3's schema change: no
        # group_id column exists yet on schedule_versions.
        conn.execute("DROP TABLE schedule_versions")
        conn.execute(
            "CREATE TABLE schedule_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_name TEXT NOT NULL, payload_json TEXT NOT NULL, pdf_cache_path TEXT NOT NULL, "
            "parsed_at TEXT NOT NULL, status TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schedule_versions (group_name, payload_json, pdf_cache_path, parsed_at, status) "
            "VALUES ('Б-Мв-101', '{\"a\": 1}', '/v1.pdf', '2026-09-16T00:00:00', 'active')"
        )
        conn.commit()


def test_migrate_legacy_single_group_creates_group_from_settings(conn):
    _seed_legacy_single_group_db(conn)
    group_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    group = storage.get_group(conn, group_id)
    assert group.name == "Б-Мв-101"
    assert group.source_page_url == "https://example.com/schedules"
    assert group.pdf_source_url == "https://example.com/101.pdf"
    assert group.send_time == "09:00"
    assert group.check_interval_minutes == 60
    assert group.timezone == "Europe/Moscow"


def test_migrate_legacy_single_group_moves_subscribers_to_members(conn):
    _seed_legacy_single_group_db(conn)
    group_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    assert sorted(storage.list_group_members(conn, group_id)) == [111, 222]


def test_migrate_legacy_single_group_requires_source_page_url(conn):
    _seed_legacy_single_group_db(conn)
    with pytest.raises(ValueError):
        storage.migrate_legacy_single_group(conn, None)


def test_migrate_legacy_single_group_is_noop_on_fresh_install(conn):
    assert storage.migrate_legacy_single_group(conn, None) is None
    assert storage.count_groups(conn) == 0


def test_migrate_legacy_single_group_is_noop_when_already_migrated(conn):
    _seed_legacy_single_group_db(conn)
    first_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    second_result = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    assert second_result is None
    assert storage.count_groups(conn) == 1
    assert first_id is not None


def test_migrate_legacy_single_group_backfills_group_id_on_old_schedule_versions(conn):
    _seed_legacy_single_group_db(conn, with_schedule_version=True)
    group_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    active = storage.get_active_version(conn, group_id)
    assert active is not None
    assert active.payload_json == '{"a": 1}'
```

Add to `tests/test_config.py` (read the existing file first to match its exact style before editing):

```python
def test_load_config_reads_legacy_group_source_page_url_when_present(monkeypatch):
    env = _base_env()
    env["LEGACY_GROUP_SOURCE_PAGE_URL"] = "https://example.com/schedules"
    config = load_config(env)
    assert config.legacy_group_source_page_url == "https://example.com/schedules"


def test_load_config_defaults_legacy_group_source_page_url_to_none():
    config = load_config(_base_env())
    assert config.legacy_group_source_page_url is None
```

(`_base_env()` should reuse whatever helper/dict the existing `test_config.py` already uses to build a minimal valid env — inspect the file and match its existing pattern instead of inventing a new one.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py tests/test_config.py -v -k "migrate or legacy"`
Expected: FAIL with `AttributeError: module 'schedule_bot.storage' has no attribute 'migrate_legacy_single_group'`

- [ ] **Step 3: Implement**

Append to `schedule_bot/storage.py`:

```python
def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def migrate_legacy_single_group(conn: sqlite3.Connection, legacy_source_page_url: str | None) -> int | None:
    """One-time upgrade from the pre-multi-group schema (a single global
    group tracked via settings.group_name/pdf_source/...) to the groups/
    members tables. Returns the new group's id, or None if there was
    nothing to migrate (fresh install, or already migrated — detected by
    `groups` already having a row)."""
    if count_groups(conn) > 0:
        return None

    legacy_group_name = get_setting(conn, "group_name")
    if not legacy_group_name:
        return None  # fresh install, nothing to migrate

    if not legacy_source_page_url:
        raise ValueError(
            "Existing legacy group_name setting found but "
            "LEGACY_GROUP_SOURCE_PAGE_URL is not set — cannot migrate without "
            "a source page URL for 404 link rediscovery."
        )

    group_id = create_group(
        conn,
        name=legacy_group_name,
        source_page_url=legacy_source_page_url,
        pdf_source_url=get_setting(conn, "pdf_source") or "",
        send_time=get_setting(conn, "daily_send_time") or "09:00",
        check_interval_minutes=int(get_setting(conn, "check_interval_minutes") or "60"),
        tz_name=get_setting(conn, "timezone") or "Europe/Moscow",
    )

    for chat_id, joined_at in conn.execute("SELECT chat_id, joined_at FROM subscribers").fetchall():
        upsert_member(conn, chat_id, group_id, joined_at=joined_at, subscribed=True)

    if not _column_exists(conn, "schedule_versions", "group_id"):
        conn.execute("ALTER TABLE schedule_versions ADD COLUMN group_id INTEGER")
    conn.execute("UPDATE schedule_versions SET group_id = ? WHERE group_id IS NULL", (group_id,))
    conn.commit()
    return group_id
```

In `schedule_bot/config.py`, add the field to the `Config` dataclass (after `default_timezone`):

```python
    legacy_group_source_page_url: str | None
```

And in `load_config`, add to the returned `Config(...)` call (after `default_timezone=...`):

```python
        legacy_group_source_page_url=env.get("LEGACY_GROUP_SOURCE_PAGE_URL"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py schedule_bot/config.py tests/test_storage.py tests/test_config.py
git commit -m "feat: migrate legacy single-group settings into groups/members"
```

---

## Task 5: `group_links.py` — resolve a group's PDF link from its source page

**Files:**
- Create: `schedule_bot/group_links.py`
- Test: `tests/test_group_links.py`

**Interfaces:**
- Produces: `group_links.SourcePageUnreachableError`, `group_links.GroupLinkNotFoundError`, `group_links.resolve_pdf_link(html: str, group_name: str, base_url: str) -> str | None`, `group_links.fetch_source_page_html(source_page_url: str) -> str`, `group_links.resolve_group_pdf_source(source_page_url: str, group_name: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_group_links.py`:

```python
import pytest

from schedule_bot.group_links import (
    GroupLinkNotFoundError,
    SourcePageUnreachableError,
    resolve_group_pdf_source,
    resolve_pdf_link,
)

_SAMPLE_HTML = """
<html><body>
<a href="/files/1_kurs_BE_BM.pdf">1 курс БЭ, БМ оз.</a>
<a href="/files/2_kurs_BE.pdf">2 курс БЭ оз.</a>
</body></html>
"""


def test_resolve_pdf_link_matches_by_visible_text_substring():
    link = resolve_pdf_link(_SAMPLE_HTML, "БЭ", "https://example.com/schedules")
    assert link == "https://example.com/files/1_kurs_BE_BM.pdf"


def test_resolve_pdf_link_matches_second_group_sharing_same_link():
    link = resolve_pdf_link(_SAMPLE_HTML, "БМ", "https://example.com/schedules")
    assert link == "https://example.com/files/1_kurs_BE_BM.pdf"


def test_resolve_pdf_link_is_case_insensitive():
    link = resolve_pdf_link(_SAMPLE_HTML, "бэ", "https://example.com/schedules")
    assert link is not None


def test_resolve_pdf_link_returns_none_when_no_match():
    assert resolve_pdf_link(_SAMPLE_HTML, "НЕТ-ТАКОЙ-ГРУППЫ", "https://example.com/schedules") is None


def test_resolve_pdf_link_resolves_relative_href_against_base_url():
    link = resolve_pdf_link(_SAMPLE_HTML, "БЭ", "https://example.com/ucheba/schedules.html")
    assert link.startswith("https://example.com/files/")


def test_resolve_group_pdf_source_raises_when_page_unreachable(monkeypatch):
    import schedule_bot.group_links as group_links_module

    def raise_error(url, timeout):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(group_links_module, "fetch_url", raise_error)

    with pytest.raises(SourcePageUnreachableError):
        resolve_group_pdf_source("https://example.com/schedules", "БЭ")


def test_resolve_group_pdf_source_raises_when_no_matching_link(monkeypatch):
    import schedule_bot.group_links as group_links_module

    class FakeResponse:
        text = _SAMPLE_HTML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(group_links_module, "fetch_url", lambda url, timeout: FakeResponse())

    with pytest.raises(GroupLinkNotFoundError):
        resolve_group_pdf_source("https://example.com/schedules", "НЕТ-ТАКОЙ-ГРУППЫ")


def test_resolve_group_pdf_source_returns_resolved_link_on_success(monkeypatch):
    import schedule_bot.group_links as group_links_module

    class FakeResponse:
        text = _SAMPLE_HTML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(group_links_module, "fetch_url", lambda url, timeout: FakeResponse())

    link = resolve_group_pdf_source("https://example.com/schedules", "БЭ")
    assert link == "https://example.com/files/1_kurs_BE_BM.pdf"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_group_links.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.group_links'`

- [ ] **Step 3: Implement**

Create `schedule_bot/group_links.py`:

```python
import re
from urllib.parse import urljoin

from schedule_bot.browser_client import fetch_url

_PDF_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


class SourcePageUnreachableError(Exception):
    pass


class GroupLinkNotFoundError(Exception):
    pass


def resolve_pdf_link(html: str, group_name: str, base_url: str) -> str | None:
    """Find the first PDF link on `html` whose href or visible text
    contains `group_name` as a case-insensitive substring. One link can
    legitimately cover two groups at once (e.g. a file named
    "1 курс БЭ, БМ оз.pdf"), so this matches on substring, not equality,
    and returns the first match deterministically when several links
    would match different group names on the same page."""
    needle = group_name.casefold()
    for href, inner_html in _PDF_LINK_RE.findall(html):
        visible_text = _TAG_RE.sub("", inner_html)
        if needle in href.casefold() or needle in visible_text.casefold():
            return urljoin(base_url, href)
    return None


def fetch_source_page_html(source_page_url: str) -> str:
    try:
        response = fetch_url(source_page_url, timeout=15)
        response.raise_for_status()
    except Exception as error:
        raise SourcePageUnreachableError(str(error)) from error
    return response.text


def resolve_group_pdf_source(source_page_url: str, group_name: str) -> str:
    """Fetch `source_page_url` and resolve the PDF link for `group_name`.
    Raises SourcePageUnreachableError if the page itself can't be
    fetched, or GroupLinkNotFoundError if no matching link is found."""
    html = fetch_source_page_html(source_page_url)
    link = resolve_pdf_link(html, group_name, source_page_url)
    if link is None:
        raise GroupLinkNotFoundError(
            f"Не найдена ссылка на расписание группы {group_name!r} на странице {source_page_url}"
        )
    return link
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_group_links.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_links.py tests/test_group_links.py
git commit -m "feat: resolve a group's PDF link from its source page by name substring"
```

---

## Task 6: `sync.py` — per-group `check_and_apply_diff` with 404-reconnect

**Files:**
- Modify: `schedule_bot/sync.py`
- Test: `tests/test_sync.py` (full rewrite of the calls — signature changed)

**Interfaces:**
- Consumes: `storage.GroupRow`, `storage.get_group`/`create_group` (Task 1), `storage.get_active_version`/`insert_new_version` (Task 3), `storage.list_group_members` (Task 2), `storage.set_group_last_sync_failed_at`/`update_group_pdf_source_url` (Task 1), `group_links.resolve_group_pdf_source` (Task 5).
- Produces: `sync.check_and_apply_diff(conn, group: GroupRow, pdf_cache_dir: str, notify: Notifier) -> SyncResult` (was `(conn, pdf_source, group_name, pdf_cache_dir, notify)`).

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_sync.py` in full:

```python
import asyncio
import sqlite3
from pathlib import Path

import pytest

from schedule_bot import storage
from schedule_bot.sync import check_and_apply_diff

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_group(conn, pdf_source_url=FIXTURE_PATH, source_page_url="https://example.com/schedules"):
    return storage.create_group(
        conn,
        name="Б-Мв-101",
        source_page_url=source_page_url,
        pdf_source_url=pdf_source_url,
        send_time="09:00",
        check_interval_minutes=60,
        tz_name="Europe/Moscow",
    )


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_first_sync_seeds_active_version_without_notifying(conn, tmp_path):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="2026-09-16T00:00:00")
    notifier = RecordingNotifier()

    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert notifier.sent == []
    assert storage.get_active_version(conn, group_id) is not None


def test_second_sync_with_same_pdf_reports_no_changes(conn, tmp_path):
    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "no_changes"
    assert notifier.sent == []


def test_sync_with_changed_pdf_notifies_all_subscribers(conn, tmp_path, monkeypatch):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    import schedule_bot.sync as sync_module
    from schedule_bot.pdf_parser import parse_schedule as real_parse_schedule

    def fake_parse_schedule(source, group_name):
        schedule = real_parse_schedule(source, group_name)
        schedule.days["Среда"].lessons[0].upper = "ИЗМЕНЁННЫЙ ПРЕДМЕТ"
        return schedule

    monkeypatch.setattr(sync_module, "parse_schedule", fake_parse_schedule)

    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "applied"
    assert len(result.changes) >= 1
    assert {chat_id for chat_id, _ in notifier.sent} == {111, 222}


def test_write_cache_sanitizes_group_name_used_as_path_segment(tmp_path):
    from schedule_bot.sync import _write_cache

    cache_path = Path(_write_cache(b"%PDF-fake", str(tmp_path), "Б-Мв/../101"))

    assert cache_path.exists()
    assert tmp_path in cache_path.resolve().parents
    assert ".." not in cache_path.parts


def test_sync_caches_pdf_file_on_disk(conn, tmp_path):
    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))
    active = storage.get_active_version(conn, group_id)
    assert Path(active.pdf_cache_path).exists()


class PartiallyFailingNotifier:
    def __init__(self, failing_chat_id):
        self.failing_chat_id = failing_chat_id
        self.sent = []

    async def __call__(self, chat_id, text):
        if chat_id == self.failing_chat_id:
            raise RuntimeError("bot blocked by user")
        self.sent.append((chat_id, text))


def test_sync_reads_pdf_source_exactly_once_and_cache_matches_parsed_bytes(conn, tmp_path, monkeypatch):
    import schedule_bot.pdf_parser as pdf_parser_module
    import schedule_bot.sync as sync_module

    real_load_pdf_bytes = pdf_parser_module._load_pdf_bytes
    call_count = 0

    def counting_load_pdf_bytes(source):
        nonlocal call_count
        call_count += 1
        return real_load_pdf_bytes(source)

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", counting_load_pdf_bytes)

    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert call_count == 1

    active = storage.get_active_version(conn, group_id)
    assert Path(active.pdf_cache_path).read_bytes() == Path(FIXTURE_PATH).read_bytes()


def test_sync_deletes_superseded_cache_file_on_change(conn, tmp_path, monkeypatch):
    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))
    first_cache_path = Path(storage.get_active_version(conn, group_id).pdf_cache_path)
    assert first_cache_path.exists()

    import schedule_bot.sync as sync_module
    from schedule_bot.pdf_parser import parse_schedule as real_parse_schedule

    def fake_parse_schedule(source, group_name):
        schedule = real_parse_schedule(source, group_name)
        schedule.days["Среда"].lessons[0].upper = "ИЗМЕНЁННЫЙ ПРЕДМЕТ"
        return schedule

    monkeypatch.setattr(sync_module, "parse_schedule", fake_parse_schedule)

    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "applied"
    second_cache_path = Path(storage.get_active_version(conn, group_id).pdf_cache_path)
    assert not first_cache_path.exists()
    assert second_cache_path.exists()
    assert second_cache_path != first_cache_path


def test_sync_retries_transient_fetch_failure_and_succeeds(conn, tmp_path, monkeypatch):
    import schedule_bot.pdf_parser as pdf_parser_module
    import schedule_bot.sync as sync_module

    real_load_pdf_bytes = pdf_parser_module._load_pdf_bytes
    call_count = 0

    def flaky_load_pdf_bytes(source):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("transient network error")
        return real_load_pdf_bytes(source)

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", flaky_load_pdf_bytes)
    # Disable 404-reconnect for this test: it's about plain transient
    # retries, not link rediscovery, and resolve_group_pdf_source would
    # otherwise fire a real network call on the first failure.
    monkeypatch.setattr(sync_module, "resolve_group_pdf_source", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no reconnect")))

    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert call_count == 3
    assert storage.get_group(conn, group_id).last_sync_failed_at in (None, "")


def test_sync_records_failure_after_exhausting_retries(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    def always_fails(source):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", always_fails)
    monkeypatch.setattr(sync_module, "resolve_group_pdf_source", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("source page also down")))

    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    with pytest.raises(ConnectionError):
        asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert storage.get_group(conn, group_id).last_sync_failed_at


def test_sync_clears_failure_flag_on_next_successful_sync(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    def always_fails(source):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", always_fails)
    monkeypatch.setattr(sync_module, "resolve_group_pdf_source", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no reconnect")))

    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    with pytest.raises(ConnectionError):
        asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))
    assert storage.get_group(conn, group_id).last_sync_failed_at

    monkeypatch.undo()

    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert storage.get_group(conn, group_id).last_sync_failed_at in (None, "")


def test_sync_notifies_remaining_subscribers_when_one_notify_fails(conn, tmp_path, monkeypatch):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    seed_notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), seed_notifier))

    import schedule_bot.sync as sync_module
    from schedule_bot.pdf_parser import parse_schedule as real_parse_schedule

    def fake_parse_schedule(source, group_name):
        schedule = real_parse_schedule(source, group_name)
        schedule.days["Среда"].lessons[0].upper = "ИЗМЕНЁННЫЙ ПРЕДМЕТ"
        return schedule

    monkeypatch.setattr(sync_module, "parse_schedule", fake_parse_schedule)

    notifier = PartiallyFailingNotifier(failing_chat_id=111)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "applied"
    assert {chat_id for chat_id, _ in notifier.sent} == {222}


def test_sync_reconnects_on_404_and_persists_new_pdf_source_url(conn, tmp_path, monkeypatch):
    """The motivating bug fix: a dead pdf_source_url must trigger one
    lookup of a fresh link on source_page_url, and the group's stored
    pdf_source_url must be updated to the rediscovered link on success."""
    import schedule_bot.sync as sync_module

    group_id = _make_group(conn, pdf_source_url="https://example.com/dead-link.pdf")

    def fake_load_pdf_bytes(source):
        if source == "https://example.com/dead-link.pdf":
            raise ConnectionError("404")
        return Path(FIXTURE_PATH).read_bytes()

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", fake_load_pdf_bytes)
    monkeypatch.setattr(
        sync_module, "resolve_group_pdf_source",
        lambda source_page_url, group_name: "https://example.com/fresh-link.pdf",
    )

    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert storage.get_group(conn, group_id).pdf_source_url == "https://example.com/fresh-link.pdf"


def test_sync_reconnect_attempted_only_once_per_sync_call(conn, tmp_path, monkeypatch):
    """A dead link whose rediscovered replacement is ALSO dead must not
    cause repeated reconnect attempts — only one lookup per sync call,
    the remaining retry budget just keeps failing against the same
    (still dead) resolved link."""
    import schedule_bot.sync as sync_module

    group_id = _make_group(conn, pdf_source_url="https://example.com/dead-link.pdf")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404")))
    reconnect_calls = []

    def fake_resolve(source_page_url, group_name):
        reconnect_calls.append((source_page_url, group_name))
        return "https://example.com/also-dead.pdf"

    monkeypatch.setattr(sync_module, "resolve_group_pdf_source", fake_resolve)

    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    with pytest.raises(ConnectionError):
        asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert len(reconnect_calls) == 1


def test_sync_reconnect_failure_falls_back_to_original_error(conn, tmp_path, monkeypatch):
    """If the source page itself is unreachable (or has no matching link),
    the sync must still fail with the original fetch error within the
    existing retry budget, not hang or raise something new."""
    import schedule_bot.sync as sync_module
    from schedule_bot.group_links import SourcePageUnreachableError

    group_id = _make_group(conn, pdf_source_url="https://example.com/dead-link.pdf")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404")))
    monkeypatch.setattr(
        sync_module, "resolve_group_pdf_source",
        lambda *a, **k: (_ for _ in ()).throw(SourcePageUnreachableError("page down")),
    )

    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    with pytest.raises(ConnectionError):
        asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))
```

(The `(_ for _ in ()).throw(...)` idiom is used inside `lambda` bodies purely because `lambda` can't contain a `raise` statement — it's the standard way to make a one-line lambda raise.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_sync.py -v`
Expected: FAIL — `check_and_apply_diff` still takes `(conn, pdf_source, group_name, pdf_cache_dir, notify)`, so every call with a `group` object raises a `TypeError`/AttributeError.

- [ ] **Step 3: Implement**

Rewrite `schedule_bot/sync.py`'s `_fetch_and_parse_with_retries` and `check_and_apply_diff` (keep `_write_cache` and the module docstring/imports otherwise unchanged; add the one new import):

```python
from schedule_bot.group_links import resolve_group_pdf_source
```

Replace `_fetch_and_parse_with_retries` (current lines 51-86) with:

```python
def _fetch_and_parse_with_retries(
    pdf_source: str, group_name: str, source_page_url: str | None
) -> tuple[bytes | None, object | None, str, Exception | None]:
    # I5: read the PDF exactly once per attempt. Retried up to
    # MAX_SYNC_ATTEMPTS times since a fetch/parse failure is often a
    # transient network blip — OR the institute renamed the PDF file
    # (the motivating bug): on the first failure, try re-discovering the
    # link once via source_page_url before burning the rest of the retry
    # budget against a now-permanently-dead URL.
    current_source = pdf_source
    reconnect_attempted = False
    pdf_bytes = None
    new_schedule = None
    last_error: Exception | None = None
    for attempt in range(MAX_SYNC_ATTEMPTS):
        tmp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        try:
            pdf_bytes = _load_pdf_bytes(current_source)
            tmp_file.write(pdf_bytes)
            tmp_file.close()
            new_schedule = parse_schedule(tmp_file.name, group_name)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            logger.warning(
                "sync: attempt %d/%d failed for %r (group %r): %s",
                attempt + 1, MAX_SYNC_ATTEMPTS, current_source, group_name, exc,
            )
            if not reconnect_attempted and source_page_url:
                reconnect_attempted = True
                try:
                    current_source = resolve_group_pdf_source(source_page_url, group_name)
                    logger.info(
                        "sync: re-discovered pdf link for group %r: %s", group_name, current_source
                    )
                except Exception as reconnect_error:
                    logger.warning(
                        "sync: 404-reconnect failed for group %r: %s", group_name, reconnect_error
                    )
        finally:
            try:
                os.unlink(tmp_file.name)
            except OSError:
                pass
    return pdf_bytes, new_schedule, current_source, last_error
```

Replace `check_and_apply_diff` (current lines 89-160) with:

```python
async def check_and_apply_diff(conn, group, pdf_cache_dir: str, notify: Notifier) -> SyncResult:
    group_id, pdf_source, group_name = group.id, group.pdf_source_url, group.name
    logger.info("sync: checking %r for group %r", pdf_source, group_name)
    active = storage.get_active_version(conn, group_id)
    old_schedule = schedule_from_dict(json.loads(active.payload_json)) if active else None

    pdf_bytes, new_schedule, resolved_source, last_error = await asyncio.to_thread(
        _fetch_and_parse_with_retries, pdf_source, group_name, group.source_page_url
    )

    if resolved_source != pdf_source:
        storage.update_group_pdf_source_url(conn, group_id, resolved_source)

    if last_error is not None:
        storage.set_group_last_sync_failed_at(conn, group_id, datetime.now(timezone.utc).isoformat())
        logger.error(
            "sync: all %d attempts failed for %r (group %r): %s",
            MAX_SYNC_ATTEMPTS, pdf_source, group_name, last_error,
        )
        raise last_error

    storage.set_group_last_sync_failed_at(conn, group_id, "")

    changes = compute_diff(old_schedule, new_schedule)

    if active is not None and not changes:
        logger.info("sync: no changes for group %r", group_name)
        return SyncResult(status="no_changes")

    cache_path = _write_cache(pdf_bytes, pdf_cache_dir, group_name)
    payload_json = json.dumps(schedule_to_dict(new_schedule))
    parsed_at = datetime.now(timezone.utc).isoformat()
    storage.insert_new_version(conn, group_id, payload_json, cache_path, parsed_at)

    # I7: the previous active version has just been superseded; its
    # cached PDF file is no longer referenced by any row.
    if active is not None and active.pdf_cache_path:
        try:
            Path(active.pdf_cache_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "sync: failed to delete superseded cache file %r for group %r: %s",
                active.pdf_cache_path, group_name, exc,
            )

    if active is None:
        logger.info("sync: initial schedule loaded for group %r", group_name)
        return SyncResult(status="initial")

    logger.info("sync: schedule changed for group %r: %s", group_name, changes)
    message = "Расписание изменилось. Актуальную версию смотрите на сайте"
    weektype_url = storage.get_setting(conn, "weektype_url")
    if weektype_url:
        message += ":\n" + weektype_url
    for chat_id in storage.list_group_members(conn, group_id):
        try:
            await notify(chat_id, message)
        except Exception as exc:
            logger.warning(
                "sync: failed to notify chat_id=%s for group %r: %s", chat_id, group_name, exc
            )
        await asyncio.sleep(SEND_PACING_SECONDS)

    return SyncResult(status="applied", changes=changes)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_sync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/sync.py tests/test_sync.py
git commit -m "feat: sync.py resolves PDF links per-group and reconnects on 404"
```

---

## Task 7: `group_commands.py` — paginated group picker + selection

**Files:**
- Create: `schedule_bot/group_commands.py`
- Test: `tests/test_group_commands.py`

**Interfaces:**
- Consumes: `storage.list_groups`/`count_groups`/`get_group` (Task 1), `storage.upsert_member`/`update_member_profile` (Task 2).
- Produces: `group_commands.build_group_picker_keyboard(conn, page=0) -> InlineKeyboardMarkup`, `group_commands.group_page_callback`, `group_commands.group_select_callback`, constants `group_commands.GROUPS_PER_PAGE`, `group_commands.SELECT_CALLBACK_PATTERN`, `group_commands.PAGE_CALLBACK_PATTERN`, `group_commands.CREATE_CALLBACK_DATA`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_group_commands.py`:

```python
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.group_commands import (
    GROUPS_PER_PAGE,
    build_group_picker_keyboard,
    group_page_callback,
    group_select_callback,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_groups(conn, count):
    for i in range(count):
        storage.create_group(
            conn, name=f"Группа-{i:02d}", source_page_url="u", pdf_source_url="p",
            send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
        )


def test_build_group_picker_keyboard_shows_one_button_per_group_plus_create(conn):
    _make_groups(conn, 3)
    keyboard = build_group_picker_keyboard(conn)
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "Группа-00" in texts
    assert "Группа-01" in texts
    assert "Группа-02" in texts
    assert any("Создать" in text for text in texts)


def test_build_group_picker_keyboard_paginates(conn):
    _make_groups(conn, GROUPS_PER_PAGE + 2)
    first_page = build_group_picker_keyboard(conn, page=0)
    first_page_names = [
        button.text for row in first_page.inline_keyboard for button in row
        if button.callback_data.startswith("grp:select:")
    ]
    assert len(first_page_names) == GROUPS_PER_PAGE
    nav_data = [
        button.callback_data for row in first_page.inline_keyboard for button in row
        if button.callback_data.startswith("grp:page:")
    ]
    assert nav_data == ["grp:page:1"]  # no "previous" on page 0, "next" present

    second_page = build_group_picker_keyboard(conn, page=1)
    second_page_names = [
        button.text for row in second_page.inline_keyboard for button in row
        if button.callback_data.startswith("grp:select:")
    ]
    assert len(second_page_names) == 2
    nav_data_2 = [
        button.callback_data for row in second_page.inline_keyboard for button in row
        if button.callback_data.startswith("grp:page:")
    ]
    assert nav_data_2 == ["grp:page:0"]  # only "previous" on the last page


def make_callback_update(chat_id, data, username="ivan", full_name="Ivan Ivanov"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.username = username
    update.effective_user.full_name = full_name
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    return context


@pytest.mark.asyncio
async def test_group_page_callback_updates_keyboard(conn):
    _make_groups(conn, GROUPS_PER_PAGE + 2)
    update = make_callback_update(111, "grp:page:1")
    context = make_context(conn)

    await group_page_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.edit_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_select_callback_attaches_member_and_replies(conn):
    _make_groups(conn, 1)
    group_id = storage.list_all_groups(conn)[0].id
    update = make_callback_update(111, f"grp:select:{group_id}")
    context = make_context(conn)

    await group_select_callback(update, context)

    member = storage.get_member(conn, 111)
    assert member.group_id == group_id
    assert member.subscribed is True
    assert member.telegram_username == "ivan"
    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_select_callback_rejects_deleted_group(conn):
    update = make_callback_update(111, "grp:select:9999")
    context = make_context(conn)

    await group_select_callback(update, context)

    assert storage.get_member(conn, 111) is None
    update.callback_query.message.reply_text.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.group_commands'`

- [ ] **Step 3: Implement**

Create `schedule_bot/group_commands.py`:

```python
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 8

SELECT_PREFIX = "grp:select:"
PAGE_PREFIX = "grp:page:"
CREATE_CALLBACK_DATA = "grp:create"

SELECT_CALLBACK_PATTERN = r"^grp:select:\d+$"
PAGE_CALLBACK_PATTERN = r"^grp:page:\d+$"


def build_group_picker_keyboard(conn, page: int = 0) -> InlineKeyboardMarkup:
    offset = page * GROUPS_PER_PAGE
    groups = storage.list_groups(conn, limit=GROUPS_PER_PAGE, offset=offset)
    total = storage.count_groups(conn)

    rows = [[InlineKeyboardButton(group.name, callback_data=f"{SELECT_PREFIX}{group.id}")] for group in groups]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{PAGE_PREFIX}{page - 1}"))
    if offset + GROUPS_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("➕ Создать группу", callback_data=CREATE_CALLBACK_DATA)])
    return InlineKeyboardMarkup(rows)


async def group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(PAGE_PREFIX))
    conn = context.bot_data["conn"]
    await query.message.edit_reply_markup(reply_markup=build_group_picker_keyboard(conn, page))


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
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_commands.py tests/test_group_commands.py
git commit -m "feat: paginated group picker and selection callback"
```

---

## Task 8: Group-creation `ConversationHandler`

**Files:**
- Modify: `schedule_bot/group_commands.py`
- Test: `tests/test_group_commands.py`

**Interfaces:**
- Consumes: `group_links.resolve_group_pdf_source`, `SourcePageUnreachableError`, `GroupLinkNotFoundError` (Task 5); `pdf_parser._load_pdf_bytes` (existing); `storage.get_group_by_name`/`create_group` (Task 1); `storage.upsert_member`/`update_member_profile` (Task 2).
- Produces: `group_commands.ASK_GROUP_NAME`, `group_commands.ASK_SOURCE_PAGE_URL` (state constants), `group_commands.start_group_creation`, `group_commands.receive_group_name`, `group_commands.receive_source_page_url`, `group_commands.cancel_group_creation`, `group_commands.group_creation_handler` (a `ConversationHandler` instance main.py registers directly).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_group_commands.py`:

```python
from schedule_bot.group_commands import (
    ASK_GROUP_NAME,
    ASK_SOURCE_PAGE_URL,
    cancel_group_creation,
    receive_group_name,
    receive_source_page_url,
    start_group_creation,
)
from telegram.ext import ConversationHandler


def make_message_update(chat_id, text, username="ivan", full_name="Ivan Ivanov"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.username = username
    update.effective_user.full_name = full_name
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def make_context_with_config(conn, default_send_time="09:00", default_check_interval=60, default_timezone="Europe/Moscow"):
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "conn": conn,
        "config": MagicMock(
            default_daily_send_time=default_send_time,
            default_check_interval_minutes=default_check_interval,
            default_timezone=default_timezone,
        ),
        "reschedule_group_jobs": MagicMock(),
    }
    return context


@pytest.mark.asyncio
async def test_start_group_creation_asks_for_name(conn):
    update = make_callback_update(111, "grp:create")
    context = make_context_with_config(conn)

    state = await start_group_creation(update, context)

    assert state == ASK_GROUP_NAME
    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_receive_group_name_stores_name_and_asks_for_source_page(conn):
    update = make_message_update(111, "Б-Мв-999")
    context = make_context_with_config(conn)

    state = await receive_group_name(update, context)

    assert state == ASK_SOURCE_PAGE_URL
    assert context.user_data["new_group_name"] == "Б-Мв-999"


@pytest.mark.asyncio
async def test_receive_source_page_url_rejects_duplicate_group_name(conn):
    storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-101"

    state = await receive_source_page_url(update, context)

    assert state == ConversationHandler.END
    assert storage.count_groups(conn) == 1


@pytest.mark.asyncio
async def test_receive_source_page_url_reports_unreachable_source_page(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module
    from schedule_bot.group_links import SourcePageUnreachableError

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source",
        lambda *a, **k: (_ for _ in ()).throw(SourcePageUnreachableError("down")),
    )
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ASK_SOURCE_PAGE_URL
    assert storage.count_groups(conn) == 0


@pytest.mark.asyncio
async def test_receive_source_page_url_reports_group_link_not_found(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module
    from schedule_bot.group_links import GroupLinkNotFoundError

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source",
        lambda *a, **k: (_ for _ in ()).throw(GroupLinkNotFoundError("not found")),
    )
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ASK_SOURCE_PAGE_URL
    assert storage.count_groups(conn) == 0


@pytest.mark.asyncio
async def test_receive_source_page_url_reports_dead_pdf_link(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source", lambda *a, **k: "https://example.com/dead.pdf"
    )
    monkeypatch.setattr(
        group_commands_module, "_load_pdf_bytes",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("404")),
    )
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ASK_SOURCE_PAGE_URL
    assert storage.count_groups(conn) == 0


@pytest.mark.asyncio
async def test_receive_source_page_url_creates_group_on_success(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source", lambda *a, **k: "https://example.com/101.pdf"
    )
    monkeypatch.setattr(group_commands_module, "_load_pdf_bytes", lambda *a, **k: b"%PDF-fake")
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ConversationHandler.END
    group = storage.get_group_by_name(conn, "Б-Мв-999")
    assert group is not None
    assert group.pdf_source_url == "https://example.com/101.pdf"
    assert storage.get_member(conn, 111).group_id == group.id
    context.bot_data["reschedule_group_jobs"].assert_called_once_with(group.id)


@pytest.mark.asyncio
async def test_cancel_group_creation_ends_conversation(conn):
    update = make_message_update(111, "/cancel")
    context = make_context_with_config(conn)

    state = await cancel_group_creation(update, context)

    assert state == ConversationHandler.END
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v -k "creation or receive_group_name or receive_source_page or start_group or cancel"`
Expected: FAIL with `ImportError: cannot import name 'ASK_GROUP_NAME'`

- [ ] **Step 3: Implement**

Append to `schedule_bot/group_commands.py` (adjust the top imports to add `ConversationHandler`, `CallbackQueryHandler`, `CommandHandler`, `MessageHandler`, `filters` from `telegram.ext`, and import `group_links.resolve_group_pdf_source`/`SourcePageUnreachableError`/`GroupLinkNotFoundError` plus `pdf_parser._load_pdf_bytes`):

```python
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters

from schedule_bot.group_links import GroupLinkNotFoundError, SourcePageUnreachableError, resolve_group_pdf_source
from schedule_bot.pdf_parser import _load_pdf_bytes
```

```python
ASK_GROUP_NAME, ASK_SOURCE_PAGE_URL = range(2)


async def start_group_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Введите название новой группы:")
    return ASK_GROUP_NAME


async def receive_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_group_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Пришлите ссылку на страницу со списком ссылок на PDF-расписания "
        "(ту, где опубликованы расписания всех групп)."
    )
    return ASK_SOURCE_PAGE_URL


async def receive_source_page_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_name = context.user_data["new_group_name"]
    source_page_url = update.message.text.strip()
    conn = context.bot_data["conn"]

    if storage.get_group_by_name(conn, group_name) is not None:
        await update.message.reply_text(
            f"Группа «{group_name}» уже существует, выберите её в списке через /start."
        )
        return ConversationHandler.END

    try:
        pdf_source_url = resolve_group_pdf_source(source_page_url, group_name)
        _load_pdf_bytes(pdf_source_url)
    except SourcePageUnreachableError:
        await update.message.reply_text(
            "Не удалось открыть страницу со ссылками на расписания. Проверьте ссылку и пришлите её ещё раз."
        )
        return ASK_SOURCE_PAGE_URL
    except GroupLinkNotFoundError:
        await update.message.reply_text(
            f"На этой странице не нашлось ссылки на расписание группы «{group_name}». "
            "Проверьте название группы или пришлите другую ссылку на страницу."
        )
        return ASK_SOURCE_PAGE_URL
    except Exception as error:
        logger.warning("group_commands: pdf link found but unreachable: %s", error)
        await update.message.reply_text(
            f"Ссылка на расписание группы «{group_name}» найдена, но PDF не открывается. "
            "Проверьте ссылку на страницу и пришлите её ещё раз."
        )
        return ASK_SOURCE_PAGE_URL

    config = context.bot_data["config"]
    group_id = storage.create_group(
        conn,
        name=group_name,
        source_page_url=source_page_url,
        pdf_source_url=pdf_source_url,
        send_time=config.default_daily_send_time,
        check_interval_minutes=config.default_check_interval_minutes,
        tz_name=config.default_timezone,
    )
    chat_id = update.effective_chat.id
    storage.upsert_member(conn, chat_id, group_id, joined_at=datetime.now(timezone.utc).isoformat(), subscribed=True)
    storage.update_member_profile(
        conn, chat_id,
        telegram_username=update.effective_user.username,
        display_name=update.effective_user.full_name,
    )
    context.bot_data["reschedule_group_jobs"](group_id)
    await update.message.reply_text(f"Группа «{group_name}» создана, вы подписаны на неё.")
    return ConversationHandler.END


async def cancel_group_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Создание группы отменено.")
    return ConversationHandler.END


group_creation_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(start_group_creation, pattern=f"^{CREATE_CALLBACK_DATA}$")],
    states={
        ASK_GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_group_name)],
        ASK_SOURCE_PAGE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_source_page_url)],
    },
    fallbacks=[CommandHandler("cancel", cancel_group_creation)],
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_commands.py tests/test_group_commands.py
git commit -m "feat: group-creation conversation with link validation"
```

---

## Task 9: `/start`, `/subscribe`, `/unsubscribe`

**Files:**
- Modify: `schedule_bot/group_commands.py`
- Test: `tests/test_group_commands.py`

**Interfaces:**
- Consumes: `storage.get_member`/`get_group`/`set_member_subscribed` (Tasks 1-2), `build_group_picker_keyboard` (Task 7).
- Produces: `group_commands.start_command`, `group_commands.subscribe_command`, `group_commands.unsubscribe_command`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_group_commands.py`:

```python
from schedule_bot.group_commands import start_command, subscribe_command, unsubscribe_command


@pytest.mark.asyncio
async def test_start_command_shows_picker_when_no_group_attached(conn):
    _make_groups(conn, 1)
    update = make_message_update(111, "/start")
    context = make_context_with_config(conn)

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_start_command_shows_status_when_group_already_attached(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=True)
    update = make_message_update(111, "/start")
    context = make_context_with_config(conn)

    await start_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "Б-Мв-101" in text
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" not in kwargs or kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_subscribe_command_requires_existing_group(conn):
    update = make_message_update(111, "/subscribe")
    context = make_context_with_config(conn)

    await subscribe_command(update, context)

    assert storage.get_member(conn, 111) is None
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_subscribe_command_resubscribes_without_changing_group(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=False)
    update = make_message_update(111, "/subscribe")
    context = make_context_with_config(conn)

    await subscribe_command(update, context)

    member = storage.get_member(conn, 111)
    assert member.subscribed is True
    assert member.group_id == group_id


@pytest.mark.asyncio
async def test_unsubscribe_command_keeps_group_attached(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=True)
    update = make_message_update(111, "/unsubscribe")
    context = make_context_with_config(conn)

    await unsubscribe_command(update, context)

    member = storage.get_member(conn, 111)
    assert member.subscribed is False
    assert member.group_id == group_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v -k "start_command or subscribe"`
Expected: FAIL with `ImportError: cannot import name 'start_command'`

- [ ] **Step 3: Implement**

Append to `schedule_bot/group_commands.py`:

```python
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    logger.info("group_commands: /start from chat_id=%s", chat_id)
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Выберите свою группу:", reply_markup=build_group_picker_keyboard(conn))
        return

    group = storage.get_group(conn, member.group_id)
    group_name = group.name if group else "—"
    status = "подписаны на рассылку" if member.subscribed else "отписаны от рассылки (группа закреплена)"
    await update.message.reply_text(
        f"Вы {status} группы «{group_name}». Команда /schedule покажет расписание на выбранный день."
    )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return
    storage.set_member_subscribed(conn, chat_id, True)
    await update.message.reply_text("Рассылка расписания включена.")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Вы ещё не выбрали группу: /start")
        return
    storage.set_member_subscribed(conn, chat_id, False)
    await update.message.reply_text(
        "Рассылка отключена. Группа остаётся закреплённой, /schedule продолжит работать."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_group_commands.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_commands.py tests/test_group_commands.py
git commit -m "feat: /start group picker, /subscribe, /unsubscribe"
```

---

## Task 10: `bot.py` — per-group `/schedule`, day callback, and admin commands

**Files:**
- Modify: `schedule_bot/bot.py`
- Modify: `schedule_bot/timeutil.py`
- Test: `tests/test_bot_user_commands.py` (rewrite affected tests)
- Test: `tests/test_bot_admin_commands.py` (rewrite affected tests)
- Test: `tests/test_timeutil.py` (update signature)

**Interfaces:**
- Consumes: `storage.get_member`/`get_group` (Tasks 1-2), `storage.update_group_pdf_source_url`/`update_group_send_time`/`update_group_check_interval` (Task 1), `storage.list_group_members` (Task 2).
- Produces: `bot.schedule_command`, `bot.day_callback`, `bot.set_pdf_source_command`, `bot.set_send_time_command`, `bot.set_check_interval_command`, `bot.refresh_command`, `bot.alert_command` all now resolve a `group_id` instead of a single global `group_name`; `timeutil.today_in_configured_timezone(tz_name: str | None) -> date` (was `(conn)`). `bot.start_command`/`bot.stop_command`/`bot.set_group_command` are removed (replaced by `group_commands.start_command`/`subscribe_command`/`unsubscribe_command` from Tasks 7-9).

- [ ] **Step 1: Update `timeutil.py`'s test and the dependent bot.py tests first**

Read `tests/test_timeutil.py` in full before editing, then change its calls from `today_in_configured_timezone(conn)` to `today_in_configured_timezone(tz_name)` (passing the timezone string directly instead of a connection with a pre-set `timezone` setting) — e.g. a test that did:

```python
conn = ...
storage.set_setting(conn, "timezone", "Europe/Moscow")
assert today_in_configured_timezone(conn) == expected
```

becomes:

```python
assert today_in_configured_timezone("Europe/Moscow") == expected
```

and a test for the "no timezone configured" fallback becomes `today_in_configured_timezone(None) == date.today()`. Preserve every existing test case's assertion, just change how the timezone value is supplied.

In `tests/test_bot_user_commands.py`, replace the `make_context` helper (lines 64-73) with:

```python
def make_context(conn, group_id=None, week_formula=None, active_schedule=None, pdf_cache_path="/tmp/does-not-matter.pdf"):
    context = MagicMock()
    context.bot_data = {
        "conn": conn,
        "week_formula": week_formula,
        "active_schedule_getter": lambda gid: active_schedule,
        "active_pdf_path_getter": lambda gid: pdf_cache_path,
    }
    return context


def _make_group_and_member(conn, chat_id=111, name="Б-Мв-101"):
    group_id = storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, chat_id, group_id, joined_at="t", subscribed=True)
    return group_id
```

Then, for every test in that file that calls `schedule_command` or `day_callback` (`test_schedule_command_sends_day_keyboard`, `test_schedule_command_warns_when_last_sync_failed`, `test_schedule_command_no_warning_when_last_sync_ok`, and all `test_day_callback_*` tests), add a call to `_make_group_and_member(conn, chat_id=111)` before invoking the handler (matching whichever `chat_id` that test's `update.effective_chat.id`/`update.callback_query` mock uses — all of them use `111` implicitly via `MagicMock()`, so set `update.effective_chat.id = 111` / `update.callback_query... ` explicitly if not already set, and call `make_context(conn, group_id=group_id, ...)`). For `test_schedule_command_warns_when_last_sync_failed`, replace the `storage.set_setting(conn, "last_sync_failed_at", ...)` call with `storage.set_group_last_sync_failed_at(conn, group_id, "2026-09-17T08:00:00+00:00")`. For `test_day_callback_replies_gracefully_when_no_active_schedule`, still call `_make_group_and_member` (a member must exist — only the schedule is `None`), and set `update.effective_chat.id = 111` on its `update` mock (currently relies on the default `MagicMock()` chat id, which is not an int — fix by setting it explicitly, same for every other `test_day_callback_*` test that doesn't already set `update.effective_chat.id`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_timeutil.py tests/test_bot_user_commands.py -v`
Expected: FAIL — `bot.py`'s `schedule_command`/`day_callback` still read `context.bot_data["group_name"]`, which the new tests no longer set.

- [ ] **Step 3: Implement `bot.py` changes**

In `schedule_bot/bot.py`:

Remove `start_command` (lines 92-101) and `stop_command` (lines 104-109) entirely — they move to `group_commands.py` (Tasks 7/9). Remove them from `USER_COMMANDS` (lines 23-28), replacing with:

```python
USER_COMMANDS = [
    ("start", "Выбрать группу / статус подписки"),
    ("subscribe", "Включить рассылку расписания"),
    ("unsubscribe", "Отключить рассылку (группа остаётся закреплённой)"),
    ("schedule", "Показать расписание на выбранный день"),
    ("help", "Список доступных команд"),
]
```

Replace `_stale_warning_prefix` (lines 45-46) with a version that takes a `GroupRow` instead of `conn`:

```python
def _stale_warning_prefix(group) -> str:
    return STALE_SCHEDULE_WARNING if group and group.last_sync_failed_at else ""
```

Replace `schedule_command` (lines 112-116) with:

```python
async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    logger.info("bot: /schedule from chat_id=%s", chat_id)
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)
    text = _stale_warning_prefix(group) + "Выберите день недели:"
    today = today_in_configured_timezone(group.timezone if group else None)
    await update.message.reply_text(text, reply_markup=build_day_keyboard(today))
```

Replace `day_callback` (lines 119-152) with:

```python
async def day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    on_date = day_date_from_callback(query.data)
    day_name = DAY_ORDER[on_date.weekday()]
    chat_id = update.effective_chat.id
    logger.info("bot: day_callback %s (%s) from chat_id=%s", day_name, on_date, chat_id)

    conn = context.bot_data["conn"]
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return

    schedule = context.bot_data["active_schedule_getter"](member.group_id)
    if schedule is None:
        await query.message.reply_text("Расписание ещё не загружено, попробуйте позже.")
        return

    week_formula: object = context.bot_data["week_formula"]
    week_type = week_formula.compute(on_date)

    text = format_day_schedule(day_name, on_date, week_type, schedule)
    await query.message.reply_text(text)

    pdf_path = context.bot_data["active_pdf_path_getter"](member.group_id)
    if pdf_path is None:
        return

    try:
        image_bytes = render_day_image(pdf_path, schedule, day_name)
    except Exception as error:
        logger.warning("bot: failed to render day image for %r: %s", pdf_path, error)
        image_bytes = None

    if image_bytes is not None:
        await query.message.reply_photo(photo=image_bytes)
```

Replace `ADMIN_COMMANDS` (lines 30-37):

```python
ADMIN_COMMANDS = [
    ("set_pdf_source", "Изменить источник PDF группы: <group_id> <url_или_путь>"),
    ("set_send_time", "Изменить время ежедневной рассылки группы: <group_id> <HH:MM>"),
    ("set_check_interval", "Изменить интервал проверки группы: <group_id> <минуты>"),
    ("refresh", "Принудительно проверить и разослать обновления группы: <group_id>"),
    ("alert", "Разослать объявление всем пользователям бота: <текст>"),
]
```

Add a shared helper right before `set_pdf_source_command`:

```python
def _resolve_admin_group(conn, group_id_str: str):
    if not group_id_str.isdigit():
        return None
    return storage.get_group(conn, int(group_id_str))
```

Replace `set_pdf_source_command` (lines 177-190):

```python
async def set_pdf_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_pdf_source from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_pdf_source <group_id> <url_или_путь>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    value = context.args[1]
    if not _is_valid_pdf_source(value):
        await update.message.reply_text("Неверный источник: разрешены только http(s)-адреса или локальный путь.")
        return
    storage.update_group_pdf_source_url(conn, group.id, value)
    await update.message.reply_text(f"Источник PDF группы «{group.name}» обновлён: {value}")
```

Delete `set_group_command` entirely (lines 193-214).

Replace `set_send_time_command` (lines 231-245):

```python
async def set_send_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_send_time from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_send_time <group_id> <HH:MM>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    value = context.args[1]
    if not _is_valid_hh_mm(value):
        await update.message.reply_text("Неверный формат времени, используйте HH:MM.")
        return
    storage.update_group_send_time(conn, group.id, value)
    context.bot_data["reschedule_group_jobs"](group.id)
    await update.message.reply_text(f"Время ежедневной рассылки группы «{group.name}» обновлено: {value}")
```

Replace `set_check_interval_command` (lines 248-262):

```python
async def set_check_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_check_interval from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_check_interval <group_id> <минуты>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    value = context.args[1]
    if not _is_valid_positive_int(value):
        await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
        return
    storage.update_group_check_interval(conn, group.id, int(value))
    context.bot_data["reschedule_group_jobs"](group.id)
    await update.message.reply_text(f"Интервал проверки группы «{group.name}» обновлён: {value} мин.")
```

Replace `refresh_command` (lines 265-284):

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
    sync_fn = context.bot_data["sync_fn"]
    try:
        result = await sync_fn(group.id)
    except Exception as error:
        logger.warning("bot: /refresh sync failed: %s", error)
        await update.message.reply_text(f"Синхронизация не удалась: {error}. Расписание могло устареть.")
        return
    if result.status == "no_changes":
        await update.message.reply_text("Изменений нет.")
    elif result.status == "initial":
        await update.message.reply_text("Расписание загружено впервые.")
    else:
        await update.message.reply_text("Расписание обновлено и разослано подписчикам.")
```

Replace `alert_command` (lines 287-305). Unlike every other admin command in this task, `/alert` stays **global** — it's the admin's channel for bot-wide announcements (releases, maintenance), not a per-group tool, so it does NOT gain a `group_id` argument; it switches from `storage.list_subscribers(conn, group_name)` to `storage.list_all_member_chat_ids(conn)` (added in Task 2) to reach every chat_id across every group:

```python
async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /alert from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /alert <текст>")
        return
    conn = context.bot_data["conn"]
    text = " ".join(context.args)
    chat_ids = storage.list_all_member_chat_ids(conn)
    for chat_id in chat_ids:
        try:
            await send_with_retry(lambda chat_id=chat_id: context.bot.send_message(chat_id=chat_id, text=text))
        except Exception as error:
            logger.warning("bot: failed to alert chat_id=%s: %s", chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)
    await update.message.reply_text(f"Сообщение разослано {len(chat_ids)} пользователям.")
```

Add `from schedule_bot.timeutil import today_in_configured_timezone` to the existing import (it's already imported — confirm it stays, only the call site changes).

In `schedule_bot/timeutil.py`, replace the whole function:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo


def today_in_configured_timezone(tz_name: str | None) -> date:
    """"Today" for daily-send/day-picker purposes must follow the
    group's configured timezone, not the server's local date — otherwise
    a container running in UTC computes the wrong day (and week type)
    around midnight for any non-UTC timezone."""
    if not tz_name:
        return date.today()
    return datetime.now(ZoneInfo(tz_name)).date()
```

(This drops the `from schedule_bot import storage` import, since the function no longer reads storage directly.)

Now rewrite the affected tests in `tests/test_bot_admin_commands.py` (read the current file first, already shown above): replace `test_set_pdf_source_rejects_non_admin`, `test_set_pdf_source_updates_setting_for_admin`, `test_set_pdf_source_rejects_disallowed_scheme`, `test_set_pdf_source_accepts_local_path`, delete `test_set_group_migrates_existing_subscribers`/`test_set_group_rejects_non_admin` entirely (the command is gone), and update `test_set_send_time_*`, `test_set_check_interval_*`, `test_refresh_command_*`, `test_alert_command_*` to pass a `group_id` and assert against `storage.get_group(...)` instead of `storage.get_setting(...)`:

```python
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import (
    ADMIN_COMMANDS,
    USER_COMMANDS,
    help_command,
    is_admin,
    set_pdf_source_command,
    set_send_time_command,
    set_check_interval_command,
    refresh_command,
    alert_command,
)

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


def make_context(conn, **extra):
    context = MagicMock()
    context.bot_data = {
        "conn": conn,
        "admin_chat_id": ADMIN_CHAT_ID,
        "reschedule_group_jobs": MagicMock(),
        **extra,
    }
    return context


def make_update(chat_id, args_text=""):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = f"/cmd {args_text}".strip()
    update.message.reply_text = AsyncMock()
    return update


def test_is_admin_true_for_configured_chat_id(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    assert is_admin(update, context) is True


def test_is_admin_false_for_other_chat_id(conn):
    update = make_update(12345)
    context = make_context(conn)
    assert is_admin(update, context) is False


@pytest.mark.asyncio
async def test_help_command_shows_only_user_commands_for_non_admin(conn):
    update = make_update(12345)
    context = make_context(conn)
    await help_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    for name, _ in USER_COMMANDS:
        assert f"/{name}" in text
    for name, _ in ADMIN_COMMANDS:
        assert f"/{name}" not in text


@pytest.mark.asyncio
async def test_help_command_shows_admin_commands_for_admin(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    await help_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    for name, _ in USER_COMMANDS + ADMIN_COMMANDS:
        assert f"/{name}" in text


@pytest.mark.asyncio
async def test_set_pdf_source_rejects_non_admin(conn):
    group_id = _make_group(conn)
    update = make_update(12345)
    context = make_context(conn)
    context.args = [str(group_id), "https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    assert storage.get_group(conn, group_id).pdf_source_url == "p"


@pytest.mark.asyncio
async def test_set_pdf_source_updates_group_for_admin(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    assert storage.get_group(conn, group_id).pdf_source_url == "https://example.com/new.pdf"


@pytest.mark.asyncio
async def test_set_pdf_source_rejects_disallowed_scheme(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "file:///etc/passwd"]

    await set_pdf_source_command(update, context)

    assert storage.get_group(conn, group_id).pdf_source_url == "p"


@pytest.mark.asyncio
async def test_set_pdf_source_accepts_local_path(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "/data/schedule.pdf"]

    await set_pdf_source_command(update, context)

    assert storage.get_group(conn, group_id).pdf_source_url == "/data/schedule.pdf"


@pytest.mark.asyncio
async def test_set_pdf_source_rejects_unknown_group_id(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = ["9999", "https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_send_time_reschedules_group_jobs(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "07:30"]

    await set_send_time_command(update, context)

    assert storage.get_group(conn, group_id).send_time == "07:30"
    context.bot_data["reschedule_group_jobs"].assert_called_once_with(group_id)


@pytest.mark.asyncio
async def test_set_check_interval_reschedules_group_jobs(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "30"]

    await set_check_interval_command(update, context)

    assert storage.get_group(conn, group_id).check_interval_minutes == 30
    context.bot_data["reschedule_group_jobs"].assert_called_once_with(group_id)


@pytest.mark.asyncio
async def test_set_send_time_rejects_malformed_value_without_writing(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "9h30"]

    await set_send_time_command(update, context)

    assert storage.get_group(conn, group_id).send_time == "09:00"
    context.bot_data["reschedule_group_jobs"].assert_not_called()


@pytest.mark.asyncio
async def test_set_check_interval_rejects_non_numeric_value_without_writing(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "abc"]

    await set_check_interval_command(update, context)

    assert storage.get_group(conn, group_id).check_interval_minutes == 60
    context.bot_data["reschedule_group_jobs"].assert_not_called()


@pytest.mark.asyncio
async def test_refresh_command_reports_result(conn):
    group_id = _make_group(conn)

    async def fake_sync(group_id_arg):
        from schedule_bot.sync import SyncResult
        return SyncResult(status="no_changes")

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_fn=fake_sync)
    context.args = [str(group_id)]

    await refresh_command(update, context)

    assert "Изменений нет" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_refresh_command_reports_sync_error_without_crashing(conn):
    group_id = _make_group(conn)

    async def failing_sync(group_id_arg):
        raise ConnectionError("network unreachable")

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_fn=failing_sync)
    context.args = [str(group_id)]

    await refresh_command(update, context)

    assert "не удал" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_alert_command_broadcasts_to_every_group_globally(conn):
    """/alert is deliberately global (bot-update announcements), not
    per-group — members of two DIFFERENT groups must both receive it."""
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 222, group_b, joined_at="t")
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = ["Внимание,", "вышло", "обновление", "бота"]
    context.bot.send_message = AsyncMock()

    await alert_command(update, context)

    assert context.bot.send_message.await_count == 2
    sent_chat_ids = {call.kwargs["chat_id"] for call in context.bot.send_message.await_args_list}
    assert sent_chat_ids == {111, 222}


@pytest.mark.asyncio
async def test_alert_command_rejects_non_admin(conn):
    storage.upsert_member(conn, 111, _make_group(conn), joined_at="t")
    update = make_update(12345)
    context = make_context(conn)
    context.args = ["test"]
    context.bot.send_message = AsyncMock()

    await alert_command(update, context)

    context.bot.send_message.assert_not_awaited()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_timeutil.py tests/test_bot_user_commands.py tests/test_bot_admin_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/bot.py schedule_bot/timeutil.py tests/test_timeutil.py tests/test_bot_user_commands.py tests/test_bot_admin_commands.py
git commit -m "refactor: bot.py commands resolve a per-group group_id"
```

---

## Task 11: `scheduler.py` — one job pair per group

**Files:**
- Modify: `schedule_bot/scheduler.py`
- Test: `tests/test_scheduler.py` (rewrite)

**Interfaces:**
- Consumes: `storage.list_all_groups`/`get_group` (Task 1), `sync.check_and_apply_diff(conn, group, pdf_cache_dir, notify)` (Task 6).
- Produces: `scheduler.schedule_group_jobs(scheduler, conn, config, bot, group, week_formula_holder, lock)`, `scheduler.reschedule_group_jobs(scheduler, conn, config, bot, group_id, week_formula_holder, lock)`, `scheduler.hourly_check_job(conn, config, bot, group_id, lock=None)`, `scheduler.daily_send_job(conn, config, bot, group_id, week_formula_holder, lock=None)`, `scheduler.build_scheduler(conn, config, bot, week_formula_holder, lock=None)`. Removed: `scheduler.resolve_sync_settings`, `scheduler.reschedule_daily_job`, `scheduler.reschedule_hourly_job` (replaced by `reschedule_group_jobs`).

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_scheduler.py` in full:

```python
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
        table_x0=0.0, table_x1=500.0,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `hourly_check_job`/`daily_send_job` still take `(conn, config, bot, lock=None)` without `group_id`, and `reschedule_group_jobs`/`schedule_group_jobs` don't exist yet.

- [ ] **Step 3: Implement**

Rewrite `schedule_bot/scheduler.py` in full:

```python
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from schedule_bot import storage
from schedule_bot.bot import STALE_SCHEDULE_WARNING
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import DAY_ORDER, schedule_from_dict
from schedule_bot.screenshot import render_day_image
from schedule_bot.sync import check_and_apply_diff
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, send_with_retry
from schedule_bot.timeutil import today_in_configured_timezone
from schedule_bot.weektype import WeekFormula, fetch_week_formula, save_week_formula

logger = logging.getLogger(__name__)


@dataclass
class WeekFormulaHolder:
    formula: WeekFormula


async def _notify(bot, chat_id: int, text: str) -> None:
    await send_with_retry(lambda: bot.send_message(chat_id=chat_id, text=text))


async def _run_sync(conn, config, bot, group, lock: asyncio.Lock | None = None):
    async def _do_sync():
        return await check_and_apply_diff(
            conn, group, config.pdf_cache_dir, lambda chat_id, text: _notify(bot, chat_id, text)
        )

    # Guard against hourly_check_job, daily_send_job and a manual /refresh
    # for the SAME group overlapping (see the original single-group
    # comment this preserves): without this, two concurrent syncs can
    # both diff against the same "active" row for that group, both
    # apply, double-broadcast, and both try to delete the same
    # superseded cache file.
    if lock is None:
        return await _do_sync()
    async with lock:
        return await _do_sync()


async def hourly_check_job(conn, config, bot, group_id: int, lock: asyncio.Lock | None = None) -> None:
    group = storage.get_group(conn, group_id)
    if group is None:
        logger.warning("scheduler: hourly_check_job skipped, group_id=%s no longer exists", group_id)
        return
    logger.info("scheduler: hourly_check_job starting for group %r", group.name)
    try:
        result = await _run_sync(conn, config, bot, group, lock)
    except Exception as error:
        logger.warning("scheduler: hourly_check_job sync failed for group %r: %s", group.name, error)
        try:
            await send_with_retry(
                lambda: bot.send_message(
                    chat_id=config.admin_chat_id,
                    text=f"Ежечасная проверка расписания группы «{group.name}» не удалась: {error}",
                )
            )
        except Exception as notify_error:
            logger.warning("scheduler: hourly_check_job failed to notify admin: %s", notify_error)
        return
    logger.info("scheduler: hourly_check_job finished for group %r with status=%s", group.name, result.status)


async def daily_send_job(
    conn, config, bot, group_id: int, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None
) -> None:
    group = storage.get_group(conn, group_id)
    if group is None:
        logger.warning("scheduler: daily_send_job skipped, group_id=%s no longer exists", group_id)
        return
    logger.info("scheduler: daily_send_job starting for group %r", group.name)
    try:
        await _run_sync(conn, config, bot, group, lock)
    except Exception as error:
        logger.warning(
            "scheduler: daily_send_job sync failed for group %r, sending with last known schedule: %s",
            group.name, error,
        )

    active = storage.get_active_version(conn, group_id)
    if active is None:
        return
    schedule = schedule_from_dict(json.loads(active.payload_json))

    today = today_in_configured_timezone(group.timezone)
    day_name = DAY_ORDER[today.weekday()] if today.weekday() < len(DAY_ORDER) else None
    if day_name is None:
        return  # Sunday: no classes in this schedule

    week_type = week_formula_holder.formula.compute(today)
    text = format_day_schedule(day_name, today, week_type, schedule)
    group = storage.get_group(conn, group_id)  # re-fetch: _run_sync may have just set last_sync_failed_at
    if group.last_sync_failed_at:
        text = STALE_SCHEDULE_WARNING + text

    image_bytes = None
    try:
        image_bytes = render_day_image(active.pdf_cache_path, schedule, day_name)
    except Exception as error:
        logger.warning("scheduler: failed to render day image for %r: %s", active.pdf_cache_path, error)

    members = storage.list_group_members(conn, group_id)
    sent = 0
    for chat_id in members:
        try:
            await send_with_retry(lambda chat_id=chat_id: bot.send_message(chat_id=chat_id, text=text))
            if image_bytes is not None:
                await send_with_retry(lambda chat_id=chat_id: bot.send_photo(chat_id=chat_id, photo=image_bytes))
            sent += 1
        except Exception as error:
            logger.warning("scheduler: failed to send daily schedule to chat_id=%s: %s", chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)
    logger.info("scheduler: daily_send_job for group %r sent to %d/%d subscribers", group.name, sent, len(members))


async def weekly_weektype_refresh_job(conn, week_formula_holder: WeekFormulaHolder) -> None:
    logger.info("scheduler: weekly_weektype_refresh_job starting")
    url = storage.get_setting(conn, "weektype_url")
    if not url:
        return
    try:
        new_formula = await asyncio.to_thread(fetch_week_formula, url)
    except Exception as error:
        logger.warning(
            "scheduler: weekly week-type refresh failed for %r (%s); keeping last known-good formula", url, error
        )
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
        logger.warning(
            "scheduler: invalid send_time %r (%s); falling back to default %r", value, error, default
        )
        return _parse_hh_mm(default)


def _safe_interval_minutes(value: int | str, default: int) -> int:
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("interval must be positive")
        return parsed
    except (ValueError, TypeError) as error:
        logger.warning(
            "scheduler: invalid check_interval_minutes %r (%s); falling back to default %r", value, error, default
        )
        return int(default)


def _group_job_ids(group_id: int) -> tuple[str, str, str]:
    return f"daily_send_{group_id}", f"hourly_check_{group_id}", f"startup_sync_{group_id}"


def schedule_group_jobs(scheduler, conn, config, bot, group, week_formula_holder, lock) -> None:
    """Add (or replace) this group's job triggers. add_job(...,
    replace_existing=True) works whether the job already exists (an
    admin changed send_time/check_interval) or doesn't yet (a brand-new
    group) — so this one function covers both cases."""
    daily_id, hourly_id, startup_id = _group_job_ids(group.id)

    hour, minute = _safe_hh_mm(group.send_time, config.default_daily_send_time)
    scheduler.add_job(
        daily_send_job,
        CronTrigger(hour=hour, minute=minute, timezone=group.timezone),
        args=[conn, config, bot, group.id, week_formula_holder, lock],
        id=daily_id,
        replace_existing=True,
    )

    interval_minutes = _safe_interval_minutes(group.check_interval_minutes, config.default_check_interval_minutes)
    scheduler.add_job(
        hourly_check_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot, group.id, lock],
        id=hourly_id,
        replace_existing=True,
    )

    # A freshly created group has no active schedule until its first
    # hourly_check_job fires — run the same sync once immediately so
    # /schedule works right away for its first subscriber.
    scheduler.add_job(
        hourly_check_job,
        DateTrigger(run_date=datetime.now()),
        args=[conn, config, bot, group.id, lock],
        id=startup_id,
        replace_existing=True,
    )


def reschedule_group_jobs(scheduler, conn, config, bot, group_id: int, week_formula_holder, lock) -> None:
    group = storage.get_group(conn, group_id)
    if group is None:
        return
    schedule_group_jobs(scheduler, conn, config, bot, group, week_formula_holder, lock)


def build_scheduler(conn, config, bot, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None) -> AsyncIOScheduler:
    if lock is None:
        lock = asyncio.Lock()

    scheduler = AsyncIOScheduler(timezone=config.default_timezone)

    for group in storage.list_all_groups(conn):
        schedule_group_jobs(scheduler, conn, config, bot, group, week_formula_holder, lock)

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/scheduler.py tests/test_scheduler.py
git commit -m "refactor: one APScheduler job pair per group instead of one global pair"
```

---

## Task 12: `main.py` wiring

**Files:**
- Modify: `main.py`
- Test: `tests/test_main_wiring.py` (rewrite)

**Interfaces:**
- Consumes: everything from Tasks 1-11.
- Produces: `main.build_application(config, conn) -> Application` with the new `bot_data` shape (`active_schedule_getter(group_id)`, `active_pdf_path_getter(group_id)`, `sync_fn(group_id)`, `reschedule_group_jobs(group_id)`, `config`; no more `group_name`), registers `group_commands.start_command`/`subscribe_command`/`unsubscribe_command`/`group_creation_handler`/`group_page_callback`/`group_select_callback`, runs `storage.migrate_legacy_single_group` and the member-profile backfill at startup.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_main_wiring.py` in full:

```python
import sqlite3
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import day_callback
from schedule_bot.config import Config
from schedule_bot.weektype import WeekFormula
from main import build_application


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


@pytest.fixture(autouse=True)
def _shutdown_schedulers_after_test(monkeypatch):
    import main as main_module

    original_build_scheduler = main_module.build_scheduler
    schedulers = []

    def tracking_build_scheduler(*args, **kwargs):
        scheduler = original_build_scheduler(*args, **kwargs)
        schedulers.append(scheduler)
        return scheduler

    monkeypatch.setattr(main_module, "build_scheduler", tracking_build_scheduler)
    yield
    for scheduler in schedulers:
        if scheduler.running:
            scheduler.shutdown(wait=False)


@pytest.fixture
def config():
    return Config(
        telegram_token="test-token",
        admin_chat_id=999,
        db_path=":memory:",
        pdf_cache_dir="/tmp/cache",
        default_pdf_source="/tmp/does-not-matter.pdf",
        default_group_name="Б-Мв-101",
        default_weektype_url="https://example.com/week",
        default_daily_send_time="09:00",
        default_check_interval_minutes=60,
        default_timezone="Europe/Moscow",
        legacy_group_source_page_url=None,
    )


def _patch_week_formula(monkeypatch):
    monkeypatch.setattr(
        "main.fetch_week_formula", lambda url: WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    )


def test_build_application_registers_all_expected_handlers(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)

    assert {
        "start", "subscribe", "unsubscribe", "schedule", "help",
        "set_pdf_source", "set_send_time", "set_check_interval", "refresh", "alert",
    }.issubset(command_names)
    assert "stop" not in command_names
    assert "set_group" not in command_names


def test_build_application_seeds_only_weektype_url_default(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    build_application(config, conn)

    assert storage.get_setting(conn, "weektype_url") == "https://example.com/week"
    assert storage.get_setting(conn, "group_name") is None  # no longer seeded — would falsely trigger migration


def test_build_application_does_not_create_a_group_on_fresh_install(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    build_application(config, conn)

    assert storage.count_groups(conn) == 0


def test_build_application_migrates_legacy_single_group_database(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)
    storage.set_setting(conn, "group_name", "Б-Мв-101")
    storage.set_setting(conn, "pdf_source", "https://example.com/101.pdf")
    storage.set_setting(conn, "daily_send_time", "09:00")
    storage.set_setting(conn, "check_interval_minutes", "60")
    storage.set_setting(conn, "timezone", "Europe/Moscow")
    conn.execute(
        "INSERT INTO subscribers (chat_id, group_name, joined_at) VALUES (111, 'Б-Мв-101', '2026-09-16T00:00:00')"
    )
    conn.commit()
    config.legacy_group_source_page_url = "https://example.com/schedules"

    application = build_application(config, conn)

    assert storage.count_groups(conn) == 1
    group = storage.list_all_groups(conn)[0]
    assert group.name == "Б-Мв-101"
    assert storage.get_member(conn, 111).group_id == group.id
    application.bot_data["_scheduler"].shutdown(wait=False)


def test_build_application_week_formula_reflects_later_holder_updates(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    week_formula = application.bot_data["week_formula"]
    on_date = date(2026, 9, 16)
    first_result = week_formula.compute(on_date)

    holder = application.bot_data["_week_formula_holder"]
    holder.formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=False)

    assert week_formula.compute(on_date) != first_result


def test_build_application_persists_successfully_fetched_week_formula(conn, config, monkeypatch):
    from schedule_bot.weektype import load_week_formula

    fetched = WeekFormula(base_month=3, base_day=17, even_index_is_upper=False)
    monkeypatch.setattr("main.fetch_week_formula", lambda url: fetched)

    build_application(config, conn)

    assert load_week_formula(conn) == fetched


def test_build_application_falls_back_to_persisted_formula_on_fetch_failure(conn, config, monkeypatch):
    from schedule_bot.weektype import save_week_formula

    persisted_formula = WeekFormula(base_month=2, base_day=1, even_index_is_upper=False)
    save_week_formula(conn, persisted_formula)

    def raise_error(url):
        raise ValueError("Week type formula pattern not found in page HTML")

    monkeypatch.setattr("main.fetch_week_formula", raise_error)

    application = build_application(config, conn)

    assert application.bot_data["_week_formula_holder"].formula == persisted_formula


def test_build_application_falls_back_to_hardcoded_default_when_nothing_persisted(conn, config, monkeypatch):
    import main as main_module

    def raise_error(url):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr("main.fetch_week_formula", raise_error)

    application = build_application(config, conn)

    assert application.bot_data["_week_formula_holder"].formula == main_module._FALLBACK_WEEK_FORMULA


@pytest.mark.asyncio
async def test_day_callback_through_real_bot_data_replies_gracefully_without_active_version(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{date(2026, 9, 16).isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()

    context = MagicMock()
    context.bot_data = application.bot_data

    await day_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_day_callback_through_real_bot_data_replies_with_schedule_when_active_version_exists(conn, config, monkeypatch, tmp_path):
    from pathlib import Path

    _patch_week_formula(monkeypatch)

    fixture_pdf = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")
    config.pdf_cache_dir = str(tmp_path)

    application = build_application(config, conn)
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url=fixture_pdf,
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    sync_fn = application.bot_data["sync_fn"]
    result = await sync_fn(group_id)
    assert result.status == "initial"

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{date(2026, 9, 16).isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()

    context = MagicMock()
    context.bot_data = application.bot_data

    await day_callback(update, context)

    reply_text = update.callback_query.message.reply_text.call_args[0][0]
    assert "ещё не загружено" not in reply_text

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_backfills_missing_member_profiles(conn, config, monkeypatch):
    """A member migrated from the legacy subscribers table (or created
    through some path that didn't capture a profile) must get its
    username/display_name backfilled via bot.get_chat during startup."""
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    fake_chat = MagicMock(username="ivan", full_name="Ivan Ivanov")
    application.bot.get_chat = AsyncMock(return_value=fake_chat)

    post_init = application.post_init
    await post_init(application)

    member = storage.get_member(conn, 111)
    assert member.telegram_username == "ivan"
    assert member.display_name == "Ivan Ivanov"

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_backfill_isolates_per_chat_failures(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    async def get_chat(chat_id):
        if chat_id == 111:
            raise RuntimeError("bot blocked by user")
        return MagicMock(username="ivan", full_name="Ivan Ivanov")

    application.bot.get_chat = AsyncMock(side_effect=get_chat)

    await application.post_init(application)  # must not raise

    assert storage.get_member(conn, 222).telegram_username == "ivan"
    assert storage.get_member(conn, 111).telegram_username is None

    application.bot_data["_scheduler"].shutdown(wait=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: FAIL — `build_application` still seeds `group_name`/`pdf_source`/etc. into `settings`, registers `stop`/`set_group`, and has no `post_init` backfill of member profiles.

- [ ] **Step 3: Implement**

Rewrite `main.py` in full:

```python
import asyncio
import logging
import os

from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from schedule_bot import storage
from schedule_bot.bot import (
    ADMIN_COMMANDS,
    NOOP_CALLBACK_DATA,
    USER_COMMANDS,
    alert_command,
    day_callback,
    help_command,
    noop_callback,
    refresh_command,
    schedule_command,
    set_check_interval_command,
    set_pdf_source_command,
    set_send_time_command,
)
from schedule_bot.config import Config, load_config
from schedule_bot.group_commands import (
    PAGE_CALLBACK_PATTERN,
    SELECT_CALLBACK_PATTERN,
    group_creation_handler,
    group_page_callback,
    group_select_callback,
    start_command,
    subscribe_command,
    unsubscribe_command,
)
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    reschedule_group_jobs,
)
from schedule_bot.sync import check_and_apply_diff
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
    # Only weektype_url stays a bot-wide setting (Global Constraints) —
    # group_name/pdf_source/daily_send_time/check_interval_minutes/timezone
    # must NOT be re-seeded here: doing so would make
    # migrate_legacy_single_group think a fresh install has legacy data
    # to migrate, auto-creating an unwanted group every time.
    storage.ensure_default_settings(conn, {"weektype_url": config.default_weektype_url})
    storage.migrate_legacy_single_group(conn, config.legacy_group_source_page_url)

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

    sync_lock = asyncio.Lock()

    async def sync_fn(group_id: int):
        group = storage.get_group(conn, group_id)
        if group is None:
            raise ValueError(f"Group {group_id} not found")

        async def notify(chat_id, text):
            await send_with_retry(lambda: application.bot.send_message(chat_id=chat_id, text=text))

        async with sync_lock:
            return await check_and_apply_diff(conn, group, config.pdf_cache_dir, notify)

    application.bot_data["sync_fn"] = sync_fn

    scheduler_holder: dict = {}

    def reschedule_group_jobs_cb(group_id: int) -> None:
        reschedule_group_jobs(
            scheduler_holder["scheduler"], conn, config, application.bot, group_id, week_formula_holder, sync_lock
        )

    application.bot_data["reschedule_group_jobs"] = reschedule_group_jobs_cb

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(day_callback, pattern=r"^day:\d{4}-\d{2}-\d{2}$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=f"^{NOOP_CALLBACK_DATA}$"))
    application.add_handler(CallbackQueryHandler(group_select_callback, pattern=SELECT_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(group_page_callback, pattern=PAGE_CALLBACK_PATTERN))
    application.add_handler(group_creation_handler)
    application.add_handler(CommandHandler("set_pdf_source", set_pdf_source_command))
    application.add_handler(CommandHandler("set_send_time", set_send_time_command))
    application.add_handler(CommandHandler("set_check_interval", set_check_interval_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("alert", alert_command))

    scheduler_holder["scheduler"] = build_scheduler(conn, config, application.bot, week_formula_holder, sync_lock)
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: PASS

Then run the full suite to confirm no cross-module regressions:

Run: `./.venv/bin/python -m pytest -v`
Expected: PASS (all tests in the repository)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "feat: wire multi-group migration, scheduling and handlers into main.py"
```

---

## Final check

After Task 12, run the entire suite once more and grep for any leftover references to the removed APIs, to catch anything the task-by-task diffs missed:

```bash
./.venv/bin/python -m pytest -v
grep -rn "group_name" schedule_bot/ main.py | grep -v "def parse_schedule\|pdf_parser\|GROUP_NAME\|group_name=" 
grep -rn "resolve_sync_settings\|reschedule_daily_job\|reschedule_hourly_job\|add_subscriber\|list_subscribers\|update_subscribers_group" schedule_bot/ main.py
```

The second `grep` should return nothing (every one of those removed names should be gone from production code); the first is a sanity scan for stray `group_name` string literals that should have become `group_id`/`GroupRow.name` — `pdf_parser.py`'s `parse_schedule(source, group_name)` parameter is legitimately unrelated (it's the PDF column-header text, unrelated to the storage layer) and should remain untouched.
