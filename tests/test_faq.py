from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot.faq import build_faq_text, faq_command


def test_build_faq_text_lists_key_commands():
    text = build_faq_text()
    for command in ("/start", "/schedule", "/classmates", "/change_group", "/starosta", "/votes", "/notify", "/report"):
        assert command in text


def test_build_faq_text_describes_starosta_voting_rules():
    text = build_faq_text()
    assert "2" in text
    assert "14" in text
    assert "3" in text


def test_build_faq_text_describes_closed_group_and_exclusion_rules():
    text = build_faq_text()
    assert "закрыт" in text.lower()
    assert "исключ" in text.lower()


@pytest.mark.asyncio
async def test_faq_command_replies_with_the_full_text():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    context = MagicMock()

    await faq_command(update, context)

    update.message.reply_text.assert_awaited_once_with(build_faq_text())
