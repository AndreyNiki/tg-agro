import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from telegram.error import BadRequest, RetryAfter, TimedOut

logger = logging.getLogger(__name__)

T = TypeVar("T")

_NOT_MODIFIED_MARKER = "message is not modified"

# Telegram's bulk-broadcast ceiling is ~30 messages/sec; pacing sends below
# that keeps loops with many subscribers from tripping RetryAfter at all.
SEND_PACING_SECONDS = 0.05


async def send_with_retry(send: Callable[[], Awaitable[T]]) -> T:
    """Call `send` (a zero-arg callable returning an awaitable) and retry
    once if Telegram rate-limits or times out the request. A single retry
    is enough for the transient cases (RetryAfter, TimedOut); anything
    else propagates so the caller's existing per-subscriber isolation
    still applies."""
    try:
        return await send()
    except RetryAfter as error:
        logger.warning("telegram: rate limited, retrying after %.1fs", error.retry_after)
        await asyncio.sleep(error.retry_after)
        return await send()
    except TimedOut:
        logger.warning("telegram: request timed out, retrying once")
        await asyncio.sleep(1)
        return await send()


async def reply(update, text, **kwargs) -> None:
    """Replies through whichever of update.message/update.callback_query.message
    is actually populated — lets a handler written for a plain /command
    invocation also work unchanged when the menu calls it directly with
    a callback-query Update."""
    if update.message is not None:
        await update.message.reply_text(text, **kwargs)
    else:
        await update.callback_query.message.reply_text(text, **kwargs)


async def safe_edit_message_text(query, text: str, **kwargs) -> None:
    """Wraps query.edit_message_text, swallowing the extremely common
    "Message is not modified" BadRequest a double-tapped navigation
    button produces (the second tap re-sends identical text+markup).
    Any other BadRequest (stale query, chat not found, ...) propagates
    to the Application's global error handler."""
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as error:
        if _NOT_MODIFIED_MARKER not in str(error).lower():
            raise


def parse_positive_int(value: str) -> int | None:
    """Stricter than str.isdigit(): some Unicode characters (superscript
    digits, fullwidth digits, etc.) satisfy isdigit() but make int(value)
    raise ValueError. isdecimal() narrows that to ASCII-compatible decimal
    digit characters, which int() always accepts; this additionally
    rejects non-positive values so every admin-id/interval parsing call
    site gets one consistent, exception-free check."""
    if not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


async def safe_edit_reply_markup(query, **kwargs) -> None:
    try:
        await query.message.edit_reply_markup(**kwargs)
    except BadRequest as error:
        if _NOT_MODIFIED_MARKER not in str(error).lower():
            raise


TELEGRAM_MESSAGE_LIMIT = 4096


def chunk_text_by_lines(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Splits `text` into chunks no longer than `limit`, breaking only at
    line boundaries so a line is never cut in the middle. A single line
    longer than `limit` becomes its own (over-length) chunk rather than
    being split mid-word, since that case does not occur with this
    codebase's current callers."""
    lines = text.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        added_len = len(line) + (1 if current else 0)
        if current and current_len + added_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += added_len
    if current:
        chunks.append("\n".join(current))
    return chunks
