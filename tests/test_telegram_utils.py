from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter, TimedOut

from schedule_bot.telegram_utils import send_with_retry


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
