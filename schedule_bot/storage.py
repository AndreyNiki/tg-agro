import sqlite3
from dataclasses import dataclass


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
            group_name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            pdf_cache_path TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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


def add_subscriber(conn: sqlite3.Connection, chat_id: int, group_name: str, joined_at: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO subscribers (chat_id, group_name, joined_at) VALUES (?, ?, ?)",
        (chat_id, group_name, joined_at),
    )
    conn.commit()


def remove_subscriber(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
    conn.commit()


def list_subscribers(conn: sqlite3.Connection, group_name: str) -> list[int]:
    rows = conn.execute(
        "SELECT chat_id FROM subscribers WHERE group_name = ?", (group_name,)
    ).fetchall()
    return [row[0] for row in rows]


@dataclass
class ScheduleVersionRow:
    id: int
    group_name: str
    payload_json: str
    pdf_cache_path: str
    parsed_at: str
    status: str


def get_active_version(conn: sqlite3.Connection, group_name: str) -> ScheduleVersionRow | None:
    row = conn.execute(
        "SELECT id, group_name, payload_json, pdf_cache_path, parsed_at, status "
        "FROM schedule_versions WHERE group_name = ? AND status = 'active'",
        (group_name,),
    ).fetchone()
    return ScheduleVersionRow(*row) if row else None


def insert_new_version(
    conn: sqlite3.Connection,
    group_name: str,
    payload_json: str,
    pdf_cache_path: str,
    parsed_at: str,
) -> int:
    conn.execute(
        "UPDATE schedule_versions SET status = 'superseded' WHERE group_name = ? AND status = 'active'",
        (group_name,),
    )
    cursor = conn.execute(
        "INSERT INTO schedule_versions (group_name, payload_json, pdf_cache_path, parsed_at, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (group_name, payload_json, pdf_cache_path, parsed_at),
    )
    conn.commit()
    return cursor.lastrowid
