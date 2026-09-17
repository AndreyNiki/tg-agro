import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from schedule_bot import storage
from schedule_bot.diff import compute_diff
from schedule_bot.pdf_parser import (
    _load_pdf_bytes,
    parse_schedule,
    schedule_from_dict,
    schedule_to_dict,
)

Notifier = Callable[[int, str], Awaitable[None]]


@dataclass
class SyncResult:
    status: str
    changes: list[str] = field(default_factory=list)


def _write_cache(pdf_bytes: bytes, pdf_cache_dir: str, group_name: str) -> str:
    cache_dir = Path(pdf_cache_dir) / group_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destination = cache_dir / f"{timestamp}.pdf"
    destination.write_bytes(pdf_bytes)
    return str(destination)


async def check_and_apply_diff(
    conn, pdf_source: str, group_name: str, pdf_cache_dir: str, notify: Notifier
) -> SyncResult:
    active = storage.get_active_version(conn, group_name)
    old_schedule = schedule_from_dict(json.loads(active.payload_json)) if active else None

    # I5: read the PDF exactly once (avoids a TOCTOU mismatch between the
    # bytes that get parsed and the bytes that end up cached on disk when
    # pdf_source is a remote URL that could change between two fetches).
    pdf_bytes = _load_pdf_bytes(pdf_source)
    tmp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp_file.write(pdf_bytes)
        tmp_file.close()

        new_schedule = parse_schedule(tmp_file.name, group_name)
        changes = compute_diff(old_schedule, new_schedule)

        if active is not None and not changes:
            return SyncResult(status="no_changes")

        cache_path = _write_cache(pdf_bytes, pdf_cache_dir, group_name)
        payload_json = json.dumps(schedule_to_dict(new_schedule))
        parsed_at = datetime.now(timezone.utc).isoformat()
        storage.insert_new_version(conn, group_name, payload_json, cache_path, parsed_at)

        # I7: the previous active version has just been superseded; its
        # cached PDF file is no longer referenced by any row, so delete it
        # to stop the cache directory from growing without bound.
        if active is not None and active.pdf_cache_path:
            try:
                Path(active.pdf_cache_path).unlink(missing_ok=True)
            except OSError as exc:
                print(
                    f"sync: failed to delete superseded cache file "
                    f"{active.pdf_cache_path!r} for group {group_name!r}: {exc}",
                    file=sys.stderr,
                )
    finally:
        try:
            os.unlink(tmp_file.name)
        except OSError:
            pass

    if active is None:
        return SyncResult(status="initial")

    message = "Расписание изменилось:\n" + "\n".join(changes)
    for chat_id in storage.list_subscribers(conn, group_name):
        try:
            await notify(chat_id, message)
        except Exception as exc:
            print(
                f"sync: failed to notify chat_id={chat_id} for group {group_name!r}: {exc}",
                file=sys.stderr,
            )

    return SyncResult(status="applied", changes=changes)
