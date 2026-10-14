import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, timezone


def _needs_institute_schema_rebuild(conn: sqlite3.Connection) -> bool:
    info = conn.execute("PRAGMA table_info(groups)").fetchall()
    if not info:
        return False  # table doesn't exist yet; CREATE TABLE IF NOT EXISTS below handles it
    columns = {row[1] for row in info}
    return "institute_id" not in columns


def _backup_legacy_member_chat_ids(conn: sqlite3.Connection) -> None:
    info = conn.execute("PRAGMA table_info(members)").fetchall()
    if not info:
        return  # no members table existed yet in the old schema — nothing to back up
    old_columns = {row[1] for row in info}
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_members_backup (
            chat_id INTEGER PRIMARY KEY,
            subscribed INTEGER NOT NULL DEFAULT 1,
            joined_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    _migrate_legacy_members_backup_add_subscribed_and_joined_at(conn)
    if "subscribed" in old_columns and "joined_at" in old_columns:
        conn.execute(
            "INSERT OR IGNORE INTO legacy_members_backup (chat_id, subscribed, joined_at) "
            "SELECT chat_id, subscribed, joined_at FROM members"
        )
    else:
        # Pre-subscribed/joined_at legacy schema (older than commit
        # 3eab0b5): nothing to preserve beyond chat_id, so these fall
        # back to the same defaults upsert_member already uses.
        conn.execute("INSERT OR IGNORE INTO legacy_members_backup (chat_id) SELECT chat_id FROM members")
    conn.commit()


def _migrate_legacy_members_backup_add_subscribed_and_joined_at(conn: sqlite3.Connection) -> None:
    info = conn.execute("PRAGMA table_info(legacy_members_backup)").fetchall()
    columns = {row[1] for row in info}
    if not info or {"subscribed", "joined_at"} <= columns:
        return  # table doesn't exist yet, or is already on the current shape
    conn.executescript(
        """
        ALTER TABLE legacy_members_backup RENAME TO legacy_members_backup_old;
        CREATE TABLE legacy_members_backup (
            chat_id INTEGER PRIMARY KEY,
            subscribed INTEGER NOT NULL DEFAULT 1,
            joined_at TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO legacy_members_backup (chat_id) SELECT chat_id FROM legacy_members_backup_old;
        DROP TABLE legacy_members_backup_old;
        """
    )
    conn.commit()


def _rebuild_groups_members_schedule_versions(conn: sqlite3.Connection) -> None:
    # Every existing members.chat_id is preserved in legacy_members_backup
    # BEFORE the drop, specifically so run_legacy_migration_if_configured
    # (which runs much later, inside post_init) has something to migrate
    # from — without this, every subscriber from a pre-institutes-schema
    # database is silently and permanently lost the moment this rebuild
    # runs.
    _backup_legacy_member_chat_ids(conn)
    # FK-safe drop order: schedule_versions/members reference groups(id).
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


@dataclass
class LegacyMemberBackupRow:
    chat_id: int
    subscribed: bool
    joined_at: str


def list_legacy_members_backup(conn: sqlite3.Connection) -> list[LegacyMemberBackupRow]:
    info = conn.execute("PRAGMA table_info(legacy_members_backup)").fetchall()
    if not info:
        return []
    columns = {row[1] for row in info}
    if {"subscribed", "joined_at"} <= columns:
        rows = conn.execute("SELECT chat_id, subscribed, joined_at FROM legacy_members_backup").fetchall()
        return [LegacyMemberBackupRow(chat_id, bool(subscribed), joined_at) for chat_id, subscribed, joined_at in rows]
    # Pre-migration shape (chat_id only) — defaults match what
    # upsert_member used before this row's subscribed/joined_at existed.
    rows = conn.execute("SELECT chat_id FROM legacy_members_backup").fetchall()
    return [LegacyMemberBackupRow(chat_id, True, "") for (chat_id,) in rows]


def prune_legacy_members_backup(conn: sqlite3.Connection, chat_ids: list[int]) -> None:
    """Deletes the given chat_ids from legacy_members_backup once they've
    been migrated onto a real `members` row, so a completed migration
    doesn't leave the backup table growing forever."""
    if not chat_ids:
        return
    conn.executemany("DELETE FROM legacy_members_backup WHERE chat_id = ?", [(chat_id,) for chat_id in chat_ids])
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    if _needs_institute_schema_rebuild(conn):
        _rebuild_groups_members_schedule_versions(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS institutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            catalog_url TEXT NOT NULL,
            check_interval_minutes INTEGER NOT NULL,
            send_time TEXT NOT NULL,
            timezone TEXT NOT NULL,
            catalog_refresh_interval_minutes INTEGER NOT NULL,
            last_catalog_sync_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS institute_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            institute_id INTEGER NOT NULL REFERENCES institutes(id) ON DELETE RESTRICT,
            pdf_url TEXT NOT NULL,
            group_names_json TEXT NOT NULL,
            raw_label TEXT NOT NULL,
            is_stale INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(institute_id, pdf_url)
        );

        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            institute_id INTEGER NOT NULL REFERENCES institutes(id) ON DELETE RESTRICT,
            institute_link_id INTEGER NOT NULL REFERENCES institute_links(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            is_verified INTEGER NOT NULL DEFAULT 0,
            starosta_chat_id INTEGER,
            last_starosta_change_at TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            last_sync_failed_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(institute_id, name)
        );

        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            telegram_username TEXT,
            display_name TEXT,
            subscribed INTEGER NOT NULL DEFAULT 1,
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
            to_group_id INTEGER REFERENCES groups(id),
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_at TEXT,
            kind TEXT NOT NULL DEFAULT 'group_change',
            request_text TEXT
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
    _migrate_group_change_requests_nullable_from_group(conn)
    _migrate_group_change_requests_add_kind_and_text(conn)
    _migrate_legacy_members_backup_add_subscribed_and_joined_at(conn)


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
            to_group_id INTEGER REFERENCES groups(id),
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_at TEXT,
            kind TEXT NOT NULL DEFAULT 'group_change',
            request_text TEXT
        );
        INSERT INTO group_change_requests
            (id, chat_id, from_group_id, to_group_id, requested_at, status, decided_at)
            SELECT id, chat_id, from_group_id, to_group_id, requested_at, status, decided_at
            FROM group_change_requests_old;
        DROP TABLE group_change_requests_old;
        """
    )
    conn.commit()


def _migrate_group_change_requests_add_kind_and_text(conn: sqlite3.Connection) -> None:
    info = conn.execute("PRAGMA table_info(group_change_requests)").fetchall()
    columns = {row[1] for row in info}
    if "kind" in columns and "request_text" in columns:
        to_group_column = next(row for row in info if row[1] == "to_group_id")
        if to_group_column[3] == 0:
            return  # already migrated: nullable to_group_id + both new columns present
    conn.executescript(
        """
        ALTER TABLE group_change_requests RENAME TO group_change_requests_old;
        CREATE TABLE group_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            from_group_id INTEGER REFERENCES groups(id),
            to_group_id INTEGER REFERENCES groups(id),
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_at TEXT,
            kind TEXT NOT NULL DEFAULT 'group_change',
            request_text TEXT
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
    institute_id: int
    institute_link_id: int
    name: str
    is_verified: int
    starosta_chat_id: int | None
    last_starosta_change_at: str | None
    is_closed: bool
    last_sync_failed_at: str | None
    created_at: str


_GROUP_COLUMNS = (
    "id, institute_id, institute_link_id, name, is_verified, "
    "starosta_chat_id, last_starosta_change_at, is_closed, last_sync_failed_at, created_at"
)


def _group_row_from_tuple(row) -> GroupRow:
    id_, institute_id, institute_link_id, name, is_verified, starosta_chat_id, last_starosta_change_at, is_closed, last_sync_failed_at, created_at = row
    return GroupRow(
        id=id_,
        institute_id=institute_id,
        institute_link_id=institute_link_id,
        name=name,
        is_verified=is_verified,
        starosta_chat_id=starosta_chat_id,
        last_starosta_change_at=last_starosta_change_at,
        is_closed=bool(is_closed),
        last_sync_failed_at=last_sync_failed_at,
        created_at=created_at,
    )


def create_group(
    conn: sqlite3.Connection,
    *,
    institute_id: int,
    institute_link_id: int,
    name: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO groups (institute_id, institute_link_id, name, created_at) VALUES (?, ?, ?, ?)",
        (
            institute_id,
            institute_link_id,
            name,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return cursor.lastrowid


@dataclass
class InstituteRow:
    id: int
    name: str
    catalog_url: str
    check_interval_minutes: int
    send_time: str
    timezone: str
    catalog_refresh_interval_minutes: int
    last_catalog_sync_at: str | None
    created_at: str


_INSTITUTE_COLUMNS = (
    "id, name, catalog_url, check_interval_minutes, send_time, timezone, "
    "catalog_refresh_interval_minutes, last_catalog_sync_at, created_at"
)


def create_institute(
    conn: sqlite3.Connection, *, name: str, catalog_url: str, check_interval_minutes: int,
    send_time: str, tz_name: str, catalog_refresh_interval_minutes: int,
) -> int:
    cursor = conn.execute(
        "INSERT INTO institutes (name, catalog_url, check_interval_minutes, send_time, "
        "timezone, catalog_refresh_interval_minutes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, catalog_url, check_interval_minutes, send_time, tz_name,
         catalog_refresh_interval_minutes, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cursor.lastrowid


def get_institute(conn: sqlite3.Connection, institute_id: int) -> InstituteRow | None:
    row = conn.execute(
        f"SELECT {_INSTITUTE_COLUMNS} FROM institutes WHERE id = ?", (institute_id,)
    ).fetchone()
    return InstituteRow(*row) if row else None


def get_institute_by_catalog_url(conn: sqlite3.Connection, catalog_url: str) -> InstituteRow | None:
    row = conn.execute(
        f"SELECT {_INSTITUTE_COLUMNS} FROM institutes WHERE catalog_url = ?", (catalog_url,)
    ).fetchone()
    return InstituteRow(*row) if row else None


def list_institutes(conn: sqlite3.Connection) -> list[InstituteRow]:
    rows = conn.execute(f"SELECT {_INSTITUTE_COLUMNS} FROM institutes ORDER BY name").fetchall()
    return [InstituteRow(*row) for row in rows]


def update_institute_catalog_url(conn: sqlite3.Connection, institute_id: int, catalog_url: str) -> None:
    conn.execute("UPDATE institutes SET catalog_url = ? WHERE id = ?", (catalog_url, institute_id))
    conn.commit()


def update_institute_check_interval(conn: sqlite3.Connection, institute_id: int, minutes: int) -> None:
    conn.execute("UPDATE institutes SET check_interval_minutes = ? WHERE id = ?", (minutes, institute_id))
    conn.commit()


def update_institute_send_time(conn: sqlite3.Connection, institute_id: int, send_time: str) -> None:
    conn.execute("UPDATE institutes SET send_time = ? WHERE id = ?", (send_time, institute_id))
    conn.commit()


def update_institute_catalog_refresh_interval(conn: sqlite3.Connection, institute_id: int, minutes: int) -> None:
    conn.execute("UPDATE institutes SET catalog_refresh_interval_minutes = ? WHERE id = ?", (minutes, institute_id))
    conn.commit()


def set_institute_last_catalog_sync_at(conn: sqlite3.Connection, institute_id: int, value: str) -> None:
    conn.execute("UPDATE institutes SET last_catalog_sync_at = ? WHERE id = ?", (value, institute_id))
    conn.commit()


@dataclass
class InstituteLinkRow:
    id: int
    institute_id: int
    pdf_url: str
    group_names_json: str
    raw_label: str
    is_stale: bool
    last_seen_at: str
    created_at: str


_INSTITUTE_LINK_COLUMNS = (
    "id, institute_id, pdf_url, group_names_json, raw_label, is_stale, last_seen_at, created_at"
)


def _institute_link_row_from_tuple(row) -> InstituteLinkRow:
    *rest, is_stale, last_seen_at, created_at = row
    return InstituteLinkRow(*rest, bool(is_stale), last_seen_at, created_at)


def create_institute_link(
    conn: sqlite3.Connection, *, institute_id: int, pdf_url: str,
    group_names: list[str], raw_label: str, last_seen_at: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO institute_links (institute_id, pdf_url, group_names_json, raw_label, "
        "last_seen_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (institute_id, pdf_url, json.dumps(group_names), raw_label, last_seen_at,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cursor.lastrowid


def get_institute_link(conn: sqlite3.Connection, link_id: int) -> InstituteLinkRow | None:
    row = conn.execute(
        f"SELECT {_INSTITUTE_LINK_COLUMNS} FROM institute_links WHERE id = ?", (link_id,)
    ).fetchone()
    return _institute_link_row_from_tuple(row) if row else None


def get_institute_link_by_pdf_url(conn: sqlite3.Connection, institute_id: int, pdf_url: str) -> InstituteLinkRow | None:
    row = conn.execute(
        f"SELECT {_INSTITUTE_LINK_COLUMNS} FROM institute_links WHERE institute_id = ? AND pdf_url = ?",
        (institute_id, pdf_url),
    ).fetchone()
    return _institute_link_row_from_tuple(row) if row else None


def list_institute_links(conn: sqlite3.Connection, institute_id: int, *, include_stale: bool = True) -> list[InstituteLinkRow]:
    if include_stale:
        rows = conn.execute(
            f"SELECT {_INSTITUTE_LINK_COLUMNS} FROM institute_links WHERE institute_id = ? ORDER BY id",
            (institute_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_INSTITUTE_LINK_COLUMNS} FROM institute_links WHERE institute_id = ? AND is_stale = 0 ORDER BY id",
            (institute_id,),
        ).fetchall()
    return [_institute_link_row_from_tuple(row) for row in rows]


def update_institute_link_group_names(
    conn: sqlite3.Connection, link_id: int, *, group_names: list[str], raw_label: str, last_seen_at: str
) -> None:
    conn.execute(
        "UPDATE institute_links SET group_names_json = ?, raw_label = ?, last_seen_at = ?, is_stale = 0 WHERE id = ?",
        (json.dumps(group_names), raw_label, last_seen_at, link_id),
    )
    conn.commit()


def update_institute_link_pdf_url(conn: sqlite3.Connection, link_id: int, pdf_url: str) -> None:
    conn.execute("UPDATE institute_links SET pdf_url = ? WHERE id = ?", (pdf_url, link_id))
    conn.commit()


def mark_institute_link_seen(conn: sqlite3.Connection, link_id: int, last_seen_at: str) -> None:
    conn.execute(
        "UPDATE institute_links SET last_seen_at = ?, is_stale = 0 WHERE id = ?", (last_seen_at, link_id)
    )
    conn.commit()


def mark_stale_links_not_in(conn: sqlite3.Connection, institute_id: int, confirmed_link_ids: list[int]) -> None:
    if not confirmed_link_ids:
        conn.execute(
            "UPDATE institute_links SET is_stale = 1 WHERE institute_id = ?",
            (institute_id,),
        )
    else:
        placeholders = ",".join("?" * len(confirmed_link_ids))
        conn.execute(
            f"UPDATE institute_links SET is_stale = 1 WHERE institute_id = ? AND id NOT IN ({placeholders})",
            (institute_id, *confirmed_link_ids),
        )
    conn.commit()


def get_group(conn: sqlite3.Connection, group_id: int) -> GroupRow | None:
    row = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    return _group_row_from_tuple(row) if row else None


def get_group_by_name(conn: sqlite3.Connection, institute_id: int, name: str) -> GroupRow | None:
    row = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE institute_id = ? AND name = ?", (institute_id, name)
    ).fetchone()
    return _group_row_from_tuple(row) if row else None


def list_groups_for_link(conn: sqlite3.Connection, institute_link_id: int) -> list[GroupRow]:
    rows = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE institute_link_id = ? ORDER BY name", (institute_link_id,)
    ).fetchall()
    return [_group_row_from_tuple(row) for row in rows]


def list_groups(
    conn: sqlite3.Connection, institute_id: int, *, limit: int, offset: int, include_unverified: bool = False
) -> list[GroupRow]:
    verified_clause = "" if include_unverified else "AND is_verified != -1"
    rows = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE institute_id = ? {verified_clause} "
        "ORDER BY name LIMIT ? OFFSET ?",
        (institute_id, limit, offset),
    ).fetchall()
    return [_group_row_from_tuple(row) for row in rows]


def list_all_groups(conn: sqlite3.Connection) -> list[GroupRow]:
    rows = conn.execute(f"SELECT {_GROUP_COLUMNS} FROM groups ORDER BY name").fetchall()
    return [_group_row_from_tuple(row) for row in rows]


def count_groups(conn: sqlite3.Connection, institute_id: int, *, include_unverified: bool = False) -> int:
    verified_clause = "" if include_unverified else "AND is_verified != -1"
    return conn.execute(
        f"SELECT COUNT(*) FROM groups WHERE institute_id = ? {verified_clause}", (institute_id,)
    ).fetchone()[0]


def update_group_is_verified(conn: sqlite3.Connection, group_id: int, value: int) -> None:
    conn.execute("UPDATE groups SET is_verified = ? WHERE id = ?", (value, group_id))
    conn.commit()


def update_group_institute_link_id(conn: sqlite3.Connection, group_id: int, institute_link_id: int) -> None:
    conn.execute("UPDATE groups SET institute_link_id = ? WHERE id = ?", (institute_link_id, group_id))
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


def reassign_member_group(conn: sqlite3.Connection, chat_id: int, group_id: int) -> None:
    conn.execute("UPDATE members SET group_id = ? WHERE chat_id = ?", (group_id, chat_id))
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
    from_group_id: int | None
    to_group_id: int | None
    requested_at: str
    status: str
    decided_at: str | None
    kind: str
    request_text: str | None


_REQUEST_COLUMNS = "id, chat_id, from_group_id, to_group_id, requested_at, status, decided_at, kind, request_text"


def create_group_change_request(
    conn: sqlite3.Connection, chat_id: int, from_group_id: int | None, to_group_id: int | None, requested_at: str,
    *, kind: str = "group_change", request_text: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO group_change_requests (chat_id, from_group_id, to_group_id, requested_at, status, kind, request_text) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (chat_id, from_group_id, to_group_id, requested_at, kind, request_text),
    )
    conn.commit()
    return cursor.lastrowid


def get_pending_request_for_chat(conn: sqlite3.Connection, chat_id: int) -> GroupChangeRequestRow | None:
    row = conn.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM group_change_requests "
        "WHERE chat_id = ? AND status = 'pending' AND kind = 'group_change'",
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


def list_all_pending_requests(conn: sqlite3.Connection) -> list[GroupChangeRequestRow]:
    rows = conn.execute(
        f"SELECT {_REQUEST_COLUMNS} FROM group_change_requests WHERE status = 'pending' ORDER BY requested_at"
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
