from datetime import datetime, timedelta, timezone

import pytest

from schedule_bot.elections import (
    cooldown_remaining,
    format_candidate_label,
    format_starosta_welcome,
    has_enough_members,
)
from schedule_bot.storage import MemberRow


def test_has_enough_members_requires_at_least_three():
    assert has_enough_members(1) is False
    assert has_enough_members(2) is False
    assert has_enough_members(3) is True
    assert has_enough_members(10) is True


def test_cooldown_remaining_none_when_never_changed():
    assert cooldown_remaining(None, datetime.now(timezone.utc)) is None


def test_cooldown_remaining_blocks_within_14_days():
    now = datetime.now(timezone.utc)
    last_change = (now - timedelta(days=5)).isoformat()
    remaining = cooldown_remaining(last_change, now)
    assert remaining is not None
    assert remaining > timedelta(days=0)


def test_cooldown_remaining_allows_after_14_days():
    now = datetime.now(timezone.utc)
    last_change = (now - timedelta(days=15)).isoformat()
    assert cooldown_remaining(last_change, now) is None


def test_cooldown_remaining_allows_exactly_at_boundary():
    now = datetime.now(timezone.utc)
    last_change = (now - timedelta(days=14, seconds=1)).isoformat()
    assert cooldown_remaining(last_change, now) is None


def _member(**overrides):
    defaults = dict(
        chat_id=111, group_id=1, telegram_username=None, display_name=None, subscribed=True, joined_at="t"
    )
    defaults.update(overrides)
    return MemberRow(**defaults)


def test_format_candidate_label_with_both_fields():
    member = _member(display_name="Ivan Ivanov", telegram_username="ivan")
    assert format_candidate_label(member) == "Ivan Ivanov (@ivan)"


def test_format_candidate_label_with_only_display_name():
    member = _member(display_name="Ivan Ivanov")
    assert format_candidate_label(member) == "Ivan Ivanov"


def test_format_candidate_label_with_only_username():
    member = _member(telegram_username="ivan")
    assert format_candidate_label(member) == "@ivan"


def test_format_candidate_label_with_neither():
    member = _member()
    assert format_candidate_label(member) == "Неизвестный"


def test_format_starosta_welcome_mentions_election():
    text = format_starosta_welcome("Б-Мв-101", "election")
    assert "Б-Мв-101" in text
    assert "голосования" in text


def test_format_starosta_welcome_mentions_admin():
    text = format_starosta_welcome("Б-Мв-101", "admin")
    assert "администратора" in text


def test_format_starosta_welcome_mentions_starosta_only_commands():
    # Regression: /close_group, /open_group, /notify, /requests are not in
    # /help for non-admin starostas, so the welcome text is their only
    # discovery path for these commands.
    text = format_starosta_welcome("Б-Мв-101", "admin")
    for command in ("/close_group", "/open_group", "/notify", "/requests"):
        assert command in text
