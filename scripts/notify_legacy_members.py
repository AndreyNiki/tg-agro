"""One-off script: broadcast a text message to every chat_id preserved in
legacy_members_backup (old subscribers wiped from `members` by the
institutes-schema rebuild, since the LEGACY_MIGRATION_* auto-migration was
deliberately not configured for this deploy).

Usage:
    set -a; source .env; set +a
    ./.venv/bin/python scripts/notify_legacy_members.py docs/release-notes-v2.md

Reads TELEGRAM_BOT_TOKEN and DB_PATH from the environment (same as the bot
itself). Does not touch any bot/application code or any table besides
reading legacy_members_backup. Safe to re-run: a chat_id that blocked the
bot just logs a warning and is skipped, the rest still get the message.
"""

import asyncio
import os
import sqlite3
import sys

from telegram import Bot

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schedule_bot import storage
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, send_with_retry


async def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: notify_legacy_members.py <path-to-text-file>", file=sys.stderr)
        sys.exit(1)

    text = open(sys.argv[1], encoding="utf-8").read().strip()
    if not text:
        print("Text file is empty, aborting.", file=sys.stderr)
        sys.exit(1)

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    db_path = os.environ.get("DB_PATH", "./data/schedule.db")

    conn = sqlite3.connect(db_path)
    try:
        storage.init_db(conn)
        chat_ids = storage.list_legacy_member_chat_ids_backup(conn)
    finally:
        conn.close()

    print(f"Found {len(chat_ids)} legacy chat_ids to notify.")
    bot = Bot(token=token)
    sent = 0
    failed = 0
    async with bot:
        for chat_id in chat_ids:
            try:
                await send_with_retry(lambda chat_id=chat_id: bot.send_message(chat_id=chat_id, text=text))
                sent += 1
            except Exception as error:
                failed += 1
                print(f"Failed to notify chat_id={chat_id}: {error}", file=sys.stderr)
            await asyncio.sleep(SEND_PACING_SECONDS)

    print(f"Done. Sent: {sent}, failed: {failed}.")


if __name__ == "__main__":
    asyncio.run(main())
