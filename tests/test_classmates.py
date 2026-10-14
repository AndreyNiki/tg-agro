import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.classmates import format_member_line, classmates_command
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


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_institute(conn, name="ИТ"):
    return storage.create_institute(
        conn, name=name, catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def _make_group(conn, name="Б-Мв-101"):
    institute_id = _make_institute(conn, f"ИТ-{name}")
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=f"https://example.com/{name}.pdf",
        group_names=[name], raw_label=name, last_seen_at="t",
    )
    return storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)


def make_update(chat_id):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    context.bot.set_my_commands = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_classmates_command_without_group_prompts_start(conn):
    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    assert "/start" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_classmates_command_lists_sorted_members_with_starosta_mark_and_legend(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.update_member_profile(conn, 222, telegram_username="anna", display_name=None)
    storage.update_group_starosta(conn, group_id, 111)

    update = make_update(222)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    lines = text.split("\n")
    assert lines[0] == "@anna"
    assert lines[1] == "👑 @boris"
    assert "👑 — староста группы" in text


@pytest.mark.asyncio
async def test_classmates_command_no_legend_when_no_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)

    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "👑" not in text


@pytest.mark.asyncio
async def test_classmates_command_includes_unsubscribed_members(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=True)
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)
    storage.upsert_member(conn, 222, group_id, joined_at="t", subscribed=False)
    storage.update_member_profile(conn, 222, telegram_username="anna", display_name=None)

    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "@anna" in text
    assert "@boris" in text


from schedule_bot.classmates import exclude_confirm_callback, exclude_select_callback

ADMIN_CHAT_ID = 999


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def make_context_with_admin(conn, admin_chat_id=ADMIN_CHAT_ID):
    context = MagicMock()
    context.bot_data = {"conn": conn, "admin_chat_id": admin_chat_id}
    context.bot.send_message = AsyncMock()
    context.bot.set_my_commands = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_classmates_command_shows_exclude_buttons_for_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    update = make_update(111)
    context = make_context_with_admin(conn)

    await classmates_command(update, context)

    _, kwargs = update.message.reply_text.call_args
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data == "exclude:select:222" for b in buttons)
    assert not any(b.callback_data == "exclude:select:111" for b in buttons)  # no self-exclude button


@pytest.mark.asyncio
async def test_classmates_command_no_exclude_buttons_for_plain_member(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    update = make_update(222)
    context = make_context_with_admin(conn)

    await classmates_command(update, context)

    call_kwargs = update.message.reply_text.call_args[1]
    assert "reply_markup" not in call_kwargs


@pytest.mark.asyncio
async def test_exclude_confirm_callback_removes_member_and_records_exclusion(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    update = make_callback_update(111, "exclude:confirm:222")
    context = make_context_with_admin(conn)

    await exclude_confirm_callback(update, context)

    assert storage.get_member(conn, 222) is None
    assert storage.has_group_exclusion(conn, group_id, 222) is True
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 222


@pytest.mark.asyncio
async def test_exclude_confirm_callback_rejects_self_exclusion_even_if_called_directly(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)

    update = make_callback_update(111, "exclude:confirm:111")
    context = make_context_with_admin(conn)

    await exclude_confirm_callback(update, context)

    assert storage.get_member(conn, 111) is not None
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_exclude_confirm_callback_rejects_unauthorized_caller(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.upsert_member(conn, 333, group_id, joined_at="t")

    update = make_callback_update(333, "exclude:confirm:222")  # 333 is neither starosta nor admin
    context = make_context_with_admin(conn)

    await exclude_confirm_callback(update, context)

    assert storage.get_member(conn, 222) is not None


@pytest.mark.asyncio
async def test_exclude_select_callback_shows_confirmation(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    update = make_callback_update(111, "exclude:select:222")
    context = make_context_with_admin(conn)

    await exclude_select_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_exclude_select_callback_rejects_self_selection(conn):
    update = make_callback_update(111, "exclude:select:111")
    context = make_context_with_admin(conn)

    await exclude_select_callback(update, context)

    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "самого себя" in text


@pytest.mark.asyncio
async def test_exclude_select_callback_rejects_unauthorized_caller_without_disclosing_label(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.update_member_profile(conn, 222, telegram_username="target_user", display_name=None)
    storage.upsert_member(conn, 333, group_id, joined_at="t")

    update = make_callback_update(333, "exclude:select:222")  # 333 is neither starosta nor admin
    context = make_context_with_admin(conn)

    await exclude_select_callback(update, context)

    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "Недостаточно прав" in text
    assert "target_user" not in text


@pytest.mark.asyncio
async def test_classmates_command_hides_exclude_button_for_admin(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.upsert_member(conn, ADMIN_CHAT_ID, group_id, joined_at="t")  # admin happens to be a member

    update = make_update(111)
    context = make_context_with_admin(conn)

    await classmates_command(update, context)

    _, kwargs = update.message.reply_text.call_args
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.callback_data == "exclude:select:222" for b in buttons)
    assert not any(b.callback_data == f"exclude:select:{ADMIN_CHAT_ID}" for b in buttons)


@pytest.mark.asyncio
async def test_exclude_select_callback_rejects_targeting_admin_even_by_admin(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, ADMIN_CHAT_ID, group_id, joined_at="t")

    update = make_callback_update(ADMIN_CHAT_ID, f"exclude:select:{ADMIN_CHAT_ID}")
    context = make_context_with_admin(conn)

    await exclude_select_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    assert "самого себя" in update.callback_query.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_exclude_select_callback_rejects_targeting_admin_by_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, ADMIN_CHAT_ID, group_id, joined_at="t")

    update = make_callback_update(111, f"exclude:select:{ADMIN_CHAT_ID}")
    context = make_context_with_admin(conn)

    await exclude_select_callback(update, context)

    assert "администратора" in update.callback_query.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_exclude_confirm_callback_rejects_targeting_admin_by_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, ADMIN_CHAT_ID, group_id, joined_at="t")

    update = make_callback_update(111, f"exclude:confirm:{ADMIN_CHAT_ID}")
    context = make_context_with_admin(conn)

    await exclude_confirm_callback(update, context)

    assert storage.get_member(conn, ADMIN_CHAT_ID) is not None
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_exclude_confirm_callback_clears_starosta_vacancy_when_excluding_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 222)  # 222 is the starosta being excluded

    update = make_callback_update(111, "exclude:confirm:222")
    context = make_context_with_admin(conn, admin_chat_id=111)

    await exclude_confirm_callback(update, context)

    assert storage.get_member(conn, 222) is None
    assert storage.get_group(conn, group_id).starosta_chat_id is None


from schedule_bot.classmates import EXCLUDE_PAGE_SIZE, exclude_page_callback


@pytest.mark.asyncio
async def test_classmates_command_splits_long_member_list_into_several_messages():
    conn = sqlite3.connect(":memory:")
    storage.init_db(conn)
    group_id = _make_group(conn)
    for i in range(400):
        storage.upsert_member(conn, 1000 + i, group_id, joined_at="t")
        storage.update_member_profile(conn, 1000 + i, telegram_username=f"user{i}" * 5, display_name=None)

    update = make_update(1000)
    context = make_context(conn)

    await classmates_command(update, context)

    from schedule_bot.telegram_utils import TELEGRAM_MESSAGE_LIMIT

    assert update.message.reply_text.await_count > 1
    for call in update.message.reply_text.await_args_list:
        assert len(call.args[0]) <= TELEGRAM_MESSAGE_LIMIT


@pytest.mark.asyncio
async def test_classmates_command_paginates_exclude_buttons_when_group_is_large():
    conn = sqlite3.connect(":memory:")
    storage.init_db(conn)
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    member_count = EXCLUDE_PAGE_SIZE + 5
    for i in range(member_count):
        storage.upsert_member(conn, 1000 + i, group_id, joined_at="t")

    update = make_update(111)
    context = make_context_with_admin(conn)

    await classmates_command(update, context)

    # Last message is the first page of the exclude keyboard.
    _, kwargs = update.message.reply_text.call_args
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    select_buttons = [b for b in buttons if b.callback_data.startswith("exclude:select:")]
    more_buttons = [b for b in buttons if b.callback_data.startswith("exclude:page:")]
    assert len(select_buttons) == EXCLUDE_PAGE_SIZE
    assert len(more_buttons) == 1


@pytest.mark.asyncio
async def test_exclude_page_callback_returns_next_page():
    conn = sqlite3.connect(":memory:")
    storage.init_db(conn)
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    member_count = EXCLUDE_PAGE_SIZE + 5
    for i in range(member_count):
        storage.upsert_member(conn, 1000 + i, group_id, joined_at="t")

    last_chat_id = 1000 + EXCLUDE_PAGE_SIZE - 1
    update = make_callback_update(111, f"exclude:page:{group_id}:{last_chat_id}")
    context = make_context_with_admin(conn)

    await exclude_page_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    select_buttons = [b for b in buttons if b.callback_data.startswith("exclude:select:")]
    assert len(select_buttons) == 5  # the 5 remaining excludable members (member_count - EXCLUDE_PAGE_SIZE)


@pytest.mark.asyncio
async def test_exclude_page_callback_rejects_unauthorized_caller():
    conn = sqlite3.connect(":memory:")
    storage.init_db(conn)
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    update = make_callback_update(333, f"exclude:page:{group_id}:0")
    context = make_context_with_admin(conn)

    await exclude_page_callback(update, context)

    assert "Недостаточно прав" in update.callback_query.message.reply_text.call_args[0][0]
