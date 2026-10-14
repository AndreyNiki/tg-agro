from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from schedule_bot.telegram_utils import (
    chunk_text_by_lines,
    parse_positive_int,
    safe_edit_message_text,
    safe_edit_reply_markup,
    send_with_retry,
    reply,
)


def test_chunk_text_by_lines_returns_single_chunk_when_under_limit():
    text = "a\nb\nc"
    assert chunk_text_by_lines(text, limit=100) == [text]


def test_chunk_text_by_lines_splits_at_line_boundaries():
    lines = [f"line{i}" for i in range(10)]
    text = "\n".join(lines)
    # Each "lineN" is 5 chars; force a split after every 3 lines (3*5 + 2
    # newlines = 17 chars fits, a 4th line would push it over).
    chunks = chunk_text_by_lines(text, limit=17)

    assert "\n".join(chunks).split("\n") == lines
    assert all(len(chunk) <= 17 for chunk in chunks)
    assert len(chunks) > 1


def test_chunk_text_by_lines_keeps_an_overlong_single_line_whole():
    long_line = "x" * 50
    text = f"short\n{long_line}\nshort2"
    chunks = chunk_text_by_lines(text, limit=10)
    assert long_line in chunks


def test_chunk_text_by_lines_returns_no_chunks_for_empty_text():
    assert chunk_text_by_lines("") == []


@pytest.mark.asyncio
async def test_send_with_retry_returns_result_on_first_success():
    send = AsyncMock(return_value="ok")

    result = await send_with_retry(send)

    assert result == "ok"
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_send_with_retry_retries_once_after_rate_limit(monkeypatch):
    import schedule_bot.telegram_utils as telegram_utils_module

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(telegram_utils_module.asyncio, "sleep", fake_sleep)

    send = AsyncMock(side_effect=[RetryAfter(retry_after=3), "ok"])

    result = await send_with_retry(send)

    assert result == "ok"
    assert send.await_count == 2
    assert sleeps == [3]


@pytest.mark.asyncio
async def test_send_with_retry_retries_once_after_timeout(monkeypatch):
    import schedule_bot.telegram_utils as telegram_utils_module

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(telegram_utils_module.asyncio, "sleep", fake_sleep)

    send = AsyncMock(side_effect=[TimedOut(), "ok"])

    result = await send_with_retry(send)

    assert result == "ok"
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_send_with_retry_propagates_second_failure():
    send = AsyncMock(side_effect=[TimedOut(), RuntimeError("bot blocked")])

    with pytest.raises(RuntimeError):
        await send_with_retry(send)


@pytest.mark.asyncio
async def test_reply_uses_update_message_when_present():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = None

    await reply(update, "hello", reply_markup="kb")

    update.message.reply_text.assert_awaited_once_with("hello", reply_markup="kb")


@pytest.mark.asyncio
async def test_reply_falls_back_to_callback_query_message_when_no_message():
    update = MagicMock()
    update.message = None
    update.callback_query.message.reply_text = AsyncMock()

    await reply(update, "hello", reply_markup="kb")

    update.callback_query.message.reply_text.assert_awaited_once_with("hello", reply_markup="kb")


@pytest.mark.asyncio
async def test_safe_edit_message_text_swallows_not_modified_error():
    query = MagicMock()
    query.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    await safe_edit_message_text(query, "same text")
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_safe_edit_message_text_reraises_other_bad_request():
    query = MagicMock()
    query.edit_message_text = AsyncMock(side_effect=BadRequest("Chat not found"))
    with pytest.raises(BadRequest):
        await safe_edit_message_text(query, "text")


@pytest.mark.asyncio
async def test_safe_edit_reply_markup_swallows_not_modified_error():
    query = MagicMock()
    query.message.edit_reply_markup = AsyncMock(side_effect=BadRequest("Message is not modified"))
    await safe_edit_reply_markup(query, reply_markup=None)
    query.message.edit_reply_markup.assert_awaited_once()


def test_parse_positive_int_rejects_unicode_digit_lookalikes():
    assert parse_positive_int("²") is None  # str.isdigit() is True, int() raises ValueError
    assert parse_positive_int("-5") is None
    assert parse_positive_int("0") is None
    assert parse_positive_int("") is None
    assert parse_positive_int("42") == 42
