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
from schedule_bot.group_links import resolve_group_pdf_source
from schedule_bot.pdf_parser import (
    _load_pdf_bytes,
    parse_schedule,
    schedule_from_dict,
    schedule_to_dict,
)
from schedule_bot.telegram_utils import SEND_PACING_SECONDS

logger = logging.getLogger(__name__)

Notifier = Callable[[int, str], Awaitable[None]]

MAX_SYNC_ATTEMPTS = 3


@dataclass
class SyncResult:
    status: str
    changes: list[str] = field(default_factory=list)


_UNSAFE_PATH_CHARS = re.compile(r"[^\w.-]")


def _write_cache(pdf_bytes: bytes, pdf_cache_dir: str, group_name: str) -> str:
    # group_name ultimately comes from admin-set config, not untrusted user
    # input, but it's used as a path segment — sanitize it so a stray "/"
    # or ".." can't be interpreted as a path separator/traversal.
    safe_group_name = _UNSAFE_PATH_CHARS.sub("_", group_name)
    cache_dir = Path(pdf_cache_dir) / safe_group_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destination = cache_dir / f"{timestamp}.pdf"
    destination.write_bytes(pdf_bytes)
    return str(destination)


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


async def check_and_apply_diff(conn, group, pdf_cache_dir: str, notify: Notifier) -> SyncResult:
    group_id, pdf_source, group_name = group.id, group.pdf_source_url, group.name
    logger.info("sync: checking %r for group %r", pdf_source, group_name)
    active = storage.get_active_version(conn, group_id)
    old_schedule = schedule_from_dict(json.loads(active.payload_json)) if active else None

    # Network fetch + pdfplumber parsing are blocking calls; running them
    # directly here would freeze the whole asyncio event loop (including
    # python-telegram-bot's own polling/HTTP traffic) for their entire
    # duration. Offload to a worker thread so the loop stays responsive.
    pdf_bytes, new_schedule, resolved_source, last_error = await asyncio.to_thread(
        _fetch_and_parse_with_retries, pdf_source, group_name, group.source_page_url
    )

    if last_error is not None:
        storage.set_group_last_sync_failed_at(conn, group_id, datetime.now(timezone.utc).isoformat())
        logger.error(
            "sync: all %d attempts failed for %r (group %r): %s",
            MAX_SYNC_ATTEMPTS, pdf_source, group_name, last_error,
        )
        raise last_error

    # Only persist a 404-reconnected link once the sync has actually
    # succeeded against it — persisting an unproven replacement link on a
    # failed sync would permanently overwrite a working pdf_source_url
    # with a link that was never confirmed to work.
    if resolved_source != pdf_source:
        storage.update_group_pdf_source_url(conn, group_id, resolved_source)

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
