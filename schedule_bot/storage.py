import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


def init_db(conn: sqlite3.Connection) -> None:
    # REFERENCES groups(id) on members/schedule_versions is otherwise purely
    # decorative — sqlite3 does not enforce foreign keys unless this pragma
    # is set per-connection.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            group_name TEXT NOT NULL,
            joined_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schedule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            payload_json TEXT NOT NULL,
            pdf_cache_path TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

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

        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            telegram_username TEXT,
            display_name TEXT,
            subscribed INTEGER NOT NULL DEFAULT 1,
            joined_at TEXT NOT NULL
        );

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

        CREATE TABLE IF NOT EXISTS group_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            from_group_id INTEGER REFERENCES groups(id),
            to_group_id INTEGER NOT NULL REFERENCES groups(id),
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS group_exclusions (
            group_id INTEGER NOT NULL REFERENCES groups(id),
            chat_id INTEGER NOT NULL,
            excluded_at TEXT NOT NULL,
            excluded_by_chat_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    _ensure_column(conn, "groups", "starosta_chat_id", "INTEGER")
    _ensure_column(conn, "groups", "last_starosta_change_at", "TEXT")
    _ensure_column(conn, "groups", "is_closed", "INTEGER NOT NULL DEFAULT 0")
    _migrate_group_change_requests_nullable_from_group(conn)


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


def ensure_default_settings(conn: sqlite3.Connection, defaults: dict[str, str]) -> None:
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


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


_GROUP_COLUMNS = (
    "id, name, source_page_url, pdf_source_url, send_time, "
    "check_interval_minutes, timezone, last_sync_failed_at, created_at, "
    "starosta_chat_id, last_starosta_change_at, is_closed"
)


def _group_row_from_tuple(row) -> GroupRow:
    *rest, is_closed = row
    return GroupRow(*rest, bool(is_closed))


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
    return _group_row_from_tuple(row) if row else None


def get_group_by_name(conn: sqlite3.Connection, name: str) -> GroupRow | None:
    row = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE name = ?", (name,)
    ).fetchone()
    return _group_row_from_tuple(row) if row else None


def list_groups(conn: sqlite3.Connection, *, limit: int, offset: int) -> list[GroupRow]:
    rows = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups ORDER BY name LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_group_row_from_tuple(row) for row in rows]


def list_all_groups(conn: sqlite3.Connection) -> list[GroupRow]:
    rows = conn.execute(f"SELECT {_GROUP_COLUMNS} FROM groups ORDER BY name").fetchall()
    return [_group_row_from_tuple(row) for row in rows]


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


def update_group_starosta(conn: sqlite3.Connection, group_id: int, chat_id: int | None) -> None:
    conn.execute("UPDATE groups SET starosta_chat_id = ? WHERE id = ?", (chat_id, group_id))
    conn.commit()


def set_group_last_starosta_change_at(conn: sqlite3.Connection, group_id: int, value: str) -> None:
    conn.execute("UPDATE groups SET last_starosta_change_at = ? WHERE id = ?", (value, group_id))
    conn.commit()


def update_group_closed(conn: sqlite3.Connection, group_id: int, is_closed: bool) -> None:
    conn.execute("UPDATE groups SET is_closed = ? WHERE id = ?", (1 if is_closed else 0, group_id))
    conn.commit()


def get_group_where_starosta(conn: sqlite3.Connection, chat_id: int) -> GroupRow | None:
    row = conn.execute(f"SELECT {_GROUP_COLUMNS} FROM groups WHERE starosta_chat_id = ?", (chat_id,)).fetchone()
    return _group_row_from_tuple(row) if row else None


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

    _rebuild_schedule_versions_for_migration(conn, group_id)
    conn.commit()
    return group_id


def _rebuild_schedule_versions_for_migration(conn: sqlite3.Connection, group_id: int) -> None:
    """Rebuilds `schedule_versions` into the current, final shape
    (group_id NOT NULL, no group_name column) and backfills every
    pre-existing row onto `group_id`. A simple `ALTER TABLE ADD COLUMN`
    is not enough here: on a real pre-multi-group database the old
    `group_name TEXT NOT NULL` column (no default) is still present
    alongside the newly-added nullable `group_id`, so the very next
    `insert_new_version` call — which no longer supplies `group_name` —
    fails with a NOT NULL constraint violation. Rebuilding the table is
    the standard SQLite pattern for dropping/retyping a column.

    Also deduplicates `status = 'active'` down to at most one row per
    group_id (keeping the one with the latest parsed_at), since legacy
    data could in principle have had more than one old group_name
    collapse onto the same migrated group_id — `get_active_version`'s
    bare `fetchone()` has no ORDER BY/tiebreak, so more than one active
    row would make the result arbitrary."""
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(schedule_versions)").fetchall()}
    group_id_select = "group_id" if "group_id" in existing_columns else "NULL"
    old_rows = conn.execute(
        f"SELECT id, {group_id_select}, payload_json, pdf_cache_path, parsed_at, status "
        "FROM schedule_versions"
    ).fetchall()

    conn.execute("DROP TABLE schedule_versions")
    conn.execute(
        """
        CREATE TABLE schedule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            payload_json TEXT NOT NULL,
            pdf_cache_path TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )

    # Dedup active rows per (backfilled) group_id: only the one with the
    # latest parsed_at stays active, the rest become superseded.
    latest_active_parsed_at: dict[int, str] = {}
    for _id, row_group_id, _payload, _cache, parsed_at, status in old_rows:
        effective_group_id = row_group_id if row_group_id is not None else group_id
        if status == "active":
            current = latest_active_parsed_at.get(effective_group_id)
            if current is None or parsed_at > current:
                latest_active_parsed_at[effective_group_id] = parsed_at

    for old_id, row_group_id, payload_json, pdf_cache_path, parsed_at, status in old_rows:
        effective_group_id = row_group_id if row_group_id is not None else group_id
        if status == "active" and parsed_at != latest_active_parsed_at.get(effective_group_id):
            status = "superseded"
        conn.execute(
            "INSERT INTO schedule_versions (id, group_id, payload_json, pdf_cache_path, parsed_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (old_id, effective_group_id, payload_json, pdf_cache_path, parsed_at, status),
        )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in columns


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        conn.commit()


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
    conn: sqlite3.Connection, chat_id: int, from_group_id: int | None, to_group_id: int, requested_at: str
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
