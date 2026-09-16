import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from schedule_bot import storage
from schedule_bot.diff import compute_diff
from schedule_bot.pdf_parser import parse_schedule, schedule_from_dict, schedule_to_dict

Notifier = Callable[[int, str], Awaitable[None]]


@dataclass
class SyncResult:
    status: str
    changes: list[str] = field(default_factory=list)


def _cache_pdf(pdf_source: str, pdf_cache_dir: str, group_name: str) -> str:
    cache_dir = Path(pdf_cache_dir) / group_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destination = cache_dir / f"{timestamp}.pdf"
    if pdf_source.startswith("http://") or pdf_source.startswith("https://"):
        import requests

        response = requests.get(pdf_source, timeout=30)
        response.raise_for_status()
        destination.write_bytes(response.content)
    else:
        shutil.copyfile(pdf_source, destination)
    return str(destination)


async def check_and_apply_diff(
    conn, pdf_source: str, group_name: str, pdf_cache_dir: str, notify: Notifier
) -> SyncResult:
    active = storage.get_active_version(conn, group_name)
    old_schedule = schedule_from_dict(json.loads(active.payload_json)) if active else None

    new_schedule = parse_schedule(pdf_source, group_name)
    changes = compute_diff(old_schedule, new_schedule)

    if active is not None and not changes:
        return SyncResult(status="no_changes")

    cache_path = _cache_pdf(pdf_source, pdf_cache_dir, group_name)
    payload_json = json.dumps(schedule_to_dict(new_schedule))
    parsed_at = datetime.now(timezone.utc).isoformat()
    storage.insert_new_version(conn, group_name, payload_json, cache_path, parsed_at)

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
