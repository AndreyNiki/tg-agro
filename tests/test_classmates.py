from schedule_bot.classmates import format_member_line
from schedule_bot.storage import MemberRow


def _member(**overrides):
    defaults = dict(
        chat_id=111, group_id=1, telegram_username=None, display_name=None, subscribed=True, joined_at="t"
    )
    defaults.update(overrides)
    return MemberRow(**defaults)


def test_format_member_line_with_both_fields():
    member = _member(display_name="Ivan Ivanov", telegram_username="ivan")
    assert format_member_line(member, is_starosta=False) == "Ivan Ivanov (@ivan)"


def test_format_member_line_with_only_display_name():
    member = _member(display_name="Ivan Ivanov")
    assert format_member_line(member, is_starosta=False) == "Ivan Ivanov"


def test_format_member_line_with_only_username():
    member = _member(telegram_username="ivan")
    assert format_member_line(member, is_starosta=False) == "@ivan"


def test_format_member_line_with_neither():
    member = _member()
    assert format_member_line(member, is_starosta=False) == "Неизвестный"


def test_format_member_line_marks_starosta():
    member = _member(display_name="Ivan Ivanov")
    assert format_member_line(member, is_starosta=True) == "👑 Ivan Ivanov"


def test_format_member_line_no_mark_when_not_starosta():
    member = _member(display_name="Ivan Ivanov")
    line = format_member_line(member, is_starosta=False)
    assert "👑" not in line
