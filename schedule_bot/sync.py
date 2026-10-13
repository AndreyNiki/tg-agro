import asyncio
import json
import logging
import os
import re
import tempfile
import time
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
AdminNotifier = Callable[[str], Awaitable[None]]

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


def _catalog_rescan_allowed(catalog_rescan_state: dict[int, float], institute) -> bool:
    """Rate-limit the 404-reconnect catalog rescan to at most once per
    institute per check cycle (design spec 3.3/3.4).

    This is keyed by institute_id to a monotonic TIMESTAMP rather than a
    boolean "already done this cycle" flag, because scheduling is
    per-link and independent (spec 4.1): there is no single moment at
    which a "cycle" starts or ends that any caller could use to clear a
    flag. A timestamp makes "once per cycle" self-resetting and
    well-defined — the next rescan is allowed once a full
    `institute.check_interval_minutes` has elapsed, which is exactly the
    period after which every link of the institute gets re-checked
    anyway. The same dict must be shared by all scheduled jobs and by the
    manual /refresh path (see main.py), or the limit is per-caller only.
    """
    try:
        interval_minutes = int(institute.check_interval_minutes)
    except (TypeError, ValueError):
        interval_minutes = 60
    min_gap_seconds = max(interval_minutes, 1) * 60

    now = time.monotonic()
    last = catalog_rescan_state.get(institute.id)
    if last is not None and now - last < min_gap_seconds:
        return False
    catalog_rescan_state[institute.id] = now
    return True


def catalog_lock_for(catalog_locks: dict[int, asyncio.Lock], institute_id: int) -> asyncio.Lock:
    """One asyncio.Lock per institute_id, shared by every caller that can
    trigger refresh_institute_catalog for that institute: the manual
    menu button, /add_institute, /refresh_institute_links, the
    background institute_catalog_refresh_job, the legacy migration's
    one-shot scan, and sync.py's own 404-fallback rescan. Two concurrent
    scans of the same institute would otherwise both read the same
    pre-scan snapshot of institute_links and independently mutate the
    DB from it."""
    return catalog_locks.setdefault(institute_id, asyncio.Lock())


async def sync_institute_link(
    conn, institute_link, institute, pdf_cache_dir: str, notify: Notifier,
    *, catalog_rescan_state: dict[int, float],
    notify_admin: AdminNotifier | None = None,
    catalog_locks: dict[int, asyncio.Lock] | None = None,
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
        # 404-style fallback: rate-limited to once per institute per check
        # interval via the caller-owned catalog_rescan_state dict (design
        # spec 3.3/3.4) — a dead link shouldn't trigger a full catalog
        # re-scan once per subscribed link on every sync pass.
        if _catalog_rescan_allowed(catalog_rescan_state, institute):
            # Alerts raised by the rescan (notably spec 1.3's "ссылка
            # пропала из каталога, подписчиков затронуто: K") MUST be
            # forwarded to the admin: this 404 -> rescan -> link gone path
            # is the single most likely way that alert is ever produced,
            # and it can never fire again later (the newly-stale diff only
            # considers previously-live links).
            catalog_alerts: list[str] = []
            lock = catalog_lock_for(catalog_locks, institute.id) if catalog_locks is not None else None
            try:
                if lock is None:
                    await asyncio.to_thread(
                        refresh_institute_catalog, conn, institute, notify_admin=catalog_alerts.append
                    )
                else:
                    async with lock:
                        await asyncio.to_thread(
                            refresh_institute_catalog, conn, institute, notify_admin=catalog_alerts.append
                        )
            except Exception as refresh_error:
                logger.warning(
                    "sync: catalog refresh fallback failed for institute %r: %s",
                    institute.name, refresh_error,
                )
            for alert_text in catalog_alerts:
                logger.warning("sync: catalog rescan alert for institute %r: %s", institute.name, alert_text)
                if notify_admin is not None:
                    try:
                        await notify_admin(alert_text)
                    except Exception as notify_error:
                        logger.warning("sync: failed to forward catalog alert to admin: %s", notify_error)
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
                storage.insert_new_version(
                    conn, group.id, json.dumps(schedule_to_dict(new_schedule)), cache_path, parsed_at
                )
            except Exception:
                Path(cache_path).unlink(missing_ok=True)
                raise

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
        except Exception as error:
            logger.exception("sync: failed to apply sync result for group %r (id=%s): %s", group.name, group.id, error)
            try:
                storage.set_group_last_sync_failed_at(conn, group.id, datetime.now(timezone.utc).isoformat())
            except Exception:
                logger.exception("sync: failed to record last_sync_failed_at for group %r (id=%s)", group.name, group.id)
            results.append(SyncResult(group_id=group.id, status="error"))

    return results
