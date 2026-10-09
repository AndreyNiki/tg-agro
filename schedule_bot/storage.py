import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


def init_db(conn: sqlite3.Connection) -> None:
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
