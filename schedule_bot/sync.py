import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from schedule_bot import storage
from schedule_bot.diff import compute_diff
from schedule_bot.group_links import refresh_institute_catalog
from schedule_bot.pdf_parser import (
    _load_pdf_bytes,
    parse_schedules_for_groups,
    schedule_from_dict,
    schedule_to_dict,
)
from schedule_bot.telegram_utils import SEND_PACING_SECONDS

logger = logging.getLogger(__name__)

Notifier = Callable[[int, str], Awaitable[None]]

MAX_FETCH_ATTEMPTS = 3

_UNSAFE_PATH_CHARS = re.compile(r"[^\w.-]")


@dataclass
class SyncResult:
    group_id: int
    status: str
    changes: list[str] = field(default_factory=list)


def _safe_segment(value: str) -> str:
    # Group/link identifiers end up as path segments below — sanitize so a
    # stray "/" or ".." can't be interpreted as a path separator/traversal.
    return _UNSAFE_PATH_CHARS.sub("_", value)


def _write_cache(pdf_bytes: bytes, pdf_cache_dir: str, institute_link_id: int, group_name: str) -> str:
    cache_dir = Path(pdf_cache_dir) / f"link_{institute_link_id}" / _safe_segment(group_name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destination = cache_dir / f"{timestamp}.pdf"
    destination.write_bytes(pdf_bytes)
    return str(destination)


def _fetch_pdf_bytes_with_retries(pdf_source: str) -> tuple[bytes | None, Exception | None]:
    last_error: Exception | None = None
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            return _load_pdf_bytes(pdf_source), None
        except Exception as exc:
            last_error = exc
            logger.warning(
                "sync: fetch attempt %d/%d failed for %r: %s",
                attempt + 1, MAX_FETCH_ATTEMPTS, pdf_source, exc,
            )
    return None, last_error


def _fetch_and_parse_blocking(
    pdf_source: str, group_names: list[str]
) -> tuple[bytes | None, dict[str, object | None], Exception | None]:
    # Blocking: network fetch (retried) + a single pdfplumber parse shared
    # by every group name on this link (parse_schedules_for_groups calls
    # extract_raw_rows exactly once regardless of len(group_names)). The
    # PDF is read from a local temp-file copy, not re-fetched from the
    # original URL, to avoid a TOCTOU bug if the remote file changes
    # between two independent fetches.
    pdf_bytes, error = _fetch_pdf_bytes_with_retries(pdf_source)
    if pdf_bytes is None:
        return None, {}, error

    tmp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp_file.write(pdf_bytes)
        tmp_file.close()
        schedules = parse_schedules_for_groups(tmp_file.name, group_names)
        return pdf_bytes, schedules, None
    finally:
        try:
            os.unlink(tmp_file.name)
        except OSError:
            pass


async def sync_institute_link(
    conn, institute_link, institute, pdf_cache_dir: str, notify: Notifier,
    *, catalog_refreshed_this_cycle: set[int],
) -> list[SyncResult]:
    """Fetch + parse the link's PDF exactly once per call (deduplicated
    across however many groups are subscribed to it, design spec 3.1/3.2),
    then apply the parsed result to each active group individually so one
    bad/missing group name doesn't abort the rest of the link's sync.

    A stale link (`is_stale=1`) never triggers a network request — the
    `institute_link.is_stale` check below runs before any fetch. A link
    with no active groups (no member, pinned or subscribed) likewise never
    fetches: there'd be nothing to apply the result to.
    """
    if institute_link.is_stale:
        return []

    groups = storage.list_groups_for_link(conn, institute_link.id)
    active_groups = [
        g for g in groups if storage.list_group_members(conn, g.id, subscribed_only=False)
    ]
    if not active_groups:
        return []

    group_names = [g.name for g in active_groups]
    pdf_bytes, schedules, error = await asyncio.to_thread(
        _fetch_and_parse_blocking, institute_link.pdf_url, group_names
    )

    if pdf_bytes is None:
        # 404-style fallback: rate-limited to once per institute per
        # scheduler cycle via the caller-owned catalog_refreshed_this_cycle
        # set (design spec 3.3/3.4) — a dead link shouldn't trigger a full
        # catalog re-scan once per subscribed link on every sync pass.
        if institute.id not in catalog_refreshed_this_cycle:
            catalog_refreshed_this_cycle.add(institute.id)
            try:
                refresh_institute_catalog(conn, institute, notify_admin=lambda text: None)
            except Exception as refresh_error:
                logger.warning(
                    "sync: catalog refresh fallback failed for institute %r: %s",
                    institute.name, refresh_error,
                )
            refreshed_link = storage.get_institute_link(conn, institute_link.id)
            if refreshed_link is not None and refreshed_link.pdf_url != institute_link.pdf_url:
                pdf_bytes, schedules, error = await asyncio.to_thread(
                    _fetch_and_parse_blocking, refreshed_link.pdf_url, group_names
                )

    if pdf_bytes is None:
        now = datetime.now(timezone.utc).isoformat()
        for group in active_groups:
            storage.set_group_last_sync_failed_at(conn, group.id, now)
        logger.error("sync: fetch failed for link %r: %s", institute_link.pdf_url, error)
        return [SyncResult(group_id=group.id, status="error") for group in active_groups]

    results: list[SyncResult] = []
    for group in active_groups:
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
        storage.insert_new_version(conn, group.id, json.dumps(schedule_to_dict(new_schedule)), cache_path, parsed_at)

        # The previous active version has just been superseded; its cached
        # PDF file is no longer referenced by any row — this is the only
        # cleanup mechanism for pdf_cache_dir.
        if active is not None and active.pdf_cache_path:
            try:
                Path(active.pdf_cache_path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "sync: failed to delete superseded cache file %r: %s", active.pdf_cache_path, exc
                )

        if active is None:
            results.append(SyncResult(group_id=group.id, status="initial"))
            continue

        message = "Расписание изменилось. Актуальную версию смотрите на сайте"
        weektype_url = storage.get_setting(conn, "weektype_url")
        if weektype_url:
            message += ":\n" + weektype_url
        # Notification is about delivery, scoped by `subscribed` — unlike
        # the active_groups gate above (subscribed_only=False), which
        # exists so a pinned-but-unsubscribed member still gets /schedule.
        for chat_id in storage.list_group_members(conn, group.id, subscribed_only=True):
            try:
                await notify(chat_id, message)
            except Exception as exc:
                logger.warning("sync: failed to notify chat_id=%s for group %r: %s", chat_id, group.name, exc)
            await asyncio.sleep(SEND_PACING_SECONDS)

        results.append(SyncResult(group_id=group.id, status="applied", changes=changes))

    return results
