import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.change_group_commands import (
    build_change_group_search_result,
    change_group_command,
    change_group_confirm_callback,
    change_group_institute_select_callback,
    change_group_page_callback,
    change_group_select_callback,
    change_request_approve_callback,
    change_request_reject_callback,
)

ADMIN_CHAT_ID = 999


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


def _make_group(conn, name, is_verified=1, pdf_url=None):
    # Default is_verified=1: these tests exercise the switch mechanics, not
    # the subscribe-time PDF validation (which has its own tests below and
    # in test_group_commands.py).
    institute_id = _make_institute(conn, f"ИТ-{name}")
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=pdf_url or f"https://example.com/{name}.pdf",
        group_names=[name], raw_label=name, last_seen_at="t",
    )
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)
    storage.update_group_is_verified(conn, group_id, is_verified)
    return group_id


def make_message_update(chat_id):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {
        "conn": conn, "admin_chat_id": ADMIN_CHAT_ID,
        "config": MagicMock(pdf_cache_dir="/tmp/cache"),
        "sync_institute_link_fn": AsyncMock(return_value=[]),
    }
    context.bot.send_message = AsyncMock()
    context.bot.set_my_commands = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_change_group_command_shows_institute_picker(conn):
    group_a = _make_group(conn, "A")
    _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_message_update(111)
    context = make_context(conn)

    await change_group_command(update, context)

    _, kwargs = update.message.reply_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert texts == ["ИТ-A", "ИТ-B"]


@pytest.mark.asyncio
async def test_institute_select_callback_shows_group_picker_excluding_current_group(conn):
    group_a = _make_group(conn, "A")
    institute_a = storage.get_group(conn, group_a).institute_id
    link_id = storage.create_institute_link(
        conn, institute_id=institute_a, pdf_url="https://example.com/A2.pdf",
        group_names=["A2"], raw_label="A2", last_seen_at="t",
    )
    storage.create_group(conn, institute_id=institute_a, institute_link_id=link_id, name="A2")  # same institute as A, a valid switch target
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_callback_update(111, f"chggrp:inst:{institute_a}")
    update.callback_query.edit_message_text = AsyncMock()
    context = make_context(conn)

    await change_group_institute_select_callback(update, context)

    update.callback_query.edit_message_text.assert_awaited_once()
    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "A2" in texts
    assert "A" not in texts  # current group excluded
    assert "🔍 Поиск по названию" in texts


@pytest.mark.asyncio
async def test_change_group_command_refuses_second_pending_request(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_message_update(111)
    context = make_context(conn)

    await change_group_command(update, context)

    assert "уже есть заявка" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_select_callback_shows_confirmation(conn):
    group_b = _make_group(conn, "B")
    update = make_callback_update(111, f"chggrp:select:{group_b}")
    context = make_context(conn)

    await change_group_select_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_confirm_callback_creates_request_and_notifies_admin_only_without_starosta(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    request = storage.get_pending_request_for_chat(conn, 111)
    assert request is not None
    assert request.to_group_id == group_b
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.await_args.kwargs["chat_id"] == ADMIN_CHAT_ID


@pytest.mark.asyncio
async def test_confirm_callback_notifies_target_starosta_too(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    notified_chat_ids = {call.kwargs["chat_id"] for call in context.bot.send_message.await_args_list}
    assert notified_chat_ids == {ADMIN_CHAT_ID, 555}


@pytest.mark.asyncio
async def test_confirm_callback_by_admin_switches_immediately_without_request(conn):
    """The admin is a super-user: switching groups must never wait on
    anyone's approval, including the admin's own."""
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, ADMIN_CHAT_ID, group_a, joined_at="t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    assert storage.get_member(conn, ADMIN_CHAT_ID).group_id == group_b
    assert storage.get_pending_request_for_chat(conn, ADMIN_CHAT_ID) is None
    context.bot.send_message.assert_not_awaited()  # no approver to notify, no requester to notify back


@pytest.mark.asyncio
async def test_confirm_callback_by_admin_enters_closed_group_immediately(conn):
    """A closed group normally forces the request/approval path (_can_decide
    gates on the target's starosta or admin) — but the admin IS the
    super-user, so is_closed must not block the direct switch either."""
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.update_group_closed(conn, group_b, True)
    storage.upsert_member(conn, ADMIN_CHAT_ID, group_a, joined_at="t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    assert storage.get_member(conn, ADMIN_CHAT_ID).group_id == group_b
    assert storage.get_pending_request_for_chat(conn, ADMIN_CHAT_ID) is None


@pytest.mark.asyncio
async def test_confirm_callback_refuses_second_pending_request(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_request_approve_by_admin_moves_member(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b
    assert storage.get_group_change_request(conn, request_id).status == "approved"
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_change_request_approve_by_target_starosta_moves_member(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(555, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b


@pytest.mark.asyncio
async def test_change_request_approve_rejects_unauthorized_caller(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(777, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_a
    assert storage.get_group_change_request(conn, request_id).status == "pending"


@pytest.mark.asyncio
async def test_change_request_approve_clears_starosta_of_origin_group(conn):
    # Finding 2 regression: if the requester was the starosta of their OLD
    # group, that group must not keep pointing at a departed starosta.
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.update_group_starosta(conn, group_a, 111)
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b
    assert storage.get_group(conn, group_a).starosta_chat_id is None


@pytest.mark.asyncio
async def test_change_request_reject_by_admin_keeps_member_in_place(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:reject:{request_id}")
    context = make_context(conn)

    await change_request_reject_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_a
    assert storage.get_group_change_request(conn, request_id).status == "rejected"


from schedule_bot.change_group_commands import (
    REQUESTS_PAGE_SIZE,
    requests_all_page_callback,
    requests_command,
    requests_group_page_callback,
)


@pytest.mark.asyncio
async def test_requests_command_starosta_sees_own_group_pending_requests(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")

    update = make_message_update(555)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    update.message.reply_text.assert_awaited_once()
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_requests_command_wording_join_vs_transfer(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    storage.create_group_change_request(conn, 111, None, group_b, "t")  # first-time join
    storage.create_group_change_request(conn, 222, group_a, group_b, "t")  # transfer

    update = make_message_update(555)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    texts = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("вступление" in text and "переход" not in text for text in texts)
    assert any("переход" in text for text in texts)


@pytest.mark.asyncio
async def test_requests_command_reports_empty_list(conn):
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)

    update = make_message_update(555)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    assert "Нет заявок" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_requests_command_rejects_plain_member(conn):
    group_a = _make_group(conn, "A")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_message_update(111)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    assert "не староста" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_requests_command_paginates_when_more_than_page_size(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    request_count = REQUESTS_PAGE_SIZE + 3
    request_ids = [
        storage.create_group_change_request(conn, chat_id, group_a, group_b, "t")
        for chat_id in range(1000, 1000 + request_count)
    ]

    update = make_message_update(555)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    # REQUESTS_PAGE_SIZE request messages + one "show more" message.
    assert update.message.reply_text.await_count == REQUESTS_PAGE_SIZE + 1
    last_call = update.message.reply_text.await_args_list[-1]
    # Absolute position against the full list, not the shrinking "remaining".
    assert f"Показано {REQUESTS_PAGE_SIZE} из {request_count} заявок." == last_call.args[0]
    more_keyboard = last_call.kwargs["reply_markup"]
    more_callback_data = more_keyboard.inline_keyboard[0][0].callback_data
    last_shown_id = request_ids[REQUESTS_PAGE_SIZE - 1]
    assert more_callback_data == f"reqpage:group:{group_b}:{last_shown_id}"


@pytest.mark.asyncio
async def test_requests_group_page_callback_sends_next_page(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    request_count = REQUESTS_PAGE_SIZE + 3
    request_ids = [
        storage.create_group_change_request(conn, chat_id, group_a, group_b, "t")
        for chat_id in range(1000, 1000 + request_count)
    ]
    cursor = request_ids[REQUESTS_PAGE_SIZE - 1]

    update = make_callback_update(555, f"reqpage:group:{group_b}:{cursor}")
    context = make_context(conn)

    await requests_group_page_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    # 3 remaining requests, no further "show more" needed.
    assert update.callback_query.message.reply_text.await_count == 3


@pytest.mark.asyncio
async def test_requests_group_page_callback_does_not_skip_requests_approved_between_pages(conn):
    """Regression test: with a numeric offset (the old implementation),
    approving some of the first page's requests before clicking "show
    more" would shift the remaining list and make the stale offset skip
    unshown requests. The keyset cursor (last shown request id) must
    keep working correctly even though the underlying list shrank."""
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    request_count = REQUESTS_PAGE_SIZE + 3
    request_ids = [
        storage.create_group_change_request(conn, chat_id, group_a, group_b, "t")
        for chat_id in range(1000, 1000 + request_count)
    ]
    cursor = request_ids[REQUESTS_PAGE_SIZE - 1]

    # Resolve the first 3 requests of the already-shown first page —
    # mirrors a starosta approving/rejecting requests before paging on.
    for request_id in request_ids[:3]:
        storage.decide_group_change_request(conn, request_id, "approved", "t2")

    update = make_callback_update(555, f"reqpage:group:{group_b}:{cursor}")
    context = make_context(conn)

    await requests_group_page_callback(update, context)

    # The 3 trailing, still-pending requests must all be shown — none
    # silently skipped because the list got shorter in between.
    assert update.callback_query.message.reply_text.await_count == 3


@pytest.mark.asyncio
async def test_requests_group_page_callback_reports_position_against_the_current_pending_list(conn):
    """The "Показано X из Y" footer's position (X) must advance with
    each page (10, then 15, ...) against the *current* pending count
    (Y) — not get stuck repeating the per-page size (always "10 из ...")
    while only the denominator shrinks, which is what made the old
    wording look like no progress was being made at all."""
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    request_count = 2 * REQUESTS_PAGE_SIZE + 4
    request_ids = [
        storage.create_group_change_request(conn, chat_id, group_a, group_b, "t")
        for chat_id in range(1000, 1000 + request_count)
    ]
    cursor = request_ids[REQUESTS_PAGE_SIZE - 1]

    # Resolve 5 of the first page's requests before paging on.
    for request_id in request_ids[:5]:
        storage.decide_group_change_request(conn, request_id, "approved", "t2")

    update = make_callback_update(555, f"reqpage:group:{group_b}:{cursor}")
    context = make_context(conn)

    await requests_group_page_callback(update, context)

    last_call = update.callback_query.message.reply_text.await_args_list[-1]
    # 19 still pending (24 - 5 resolved); of those, 15 have now been
    # shown (the first page's unresolved 5 + this page's 10) — not "10
    # из 14"/"10 из 19", which would look like zero progress.
    assert "Показано 15 из 19 заявок." == last_call.args[0]


@pytest.mark.asyncio
async def test_requests_group_page_callback_rejects_unauthorized_caller(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")

    update = make_callback_update(222, f"reqpage:group:{group_b}:0")
    context = make_context(conn)

    await requests_group_page_callback(update, context)

    assert "Недостаточно прав" in update.callback_query.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_requests_all_page_callback_sends_next_page_for_admin(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    request_count = REQUESTS_PAGE_SIZE + 2
    request_ids = [
        storage.create_group_change_request(conn, chat_id, group_a, group_b, "t")
        for chat_id in range(1000, 1000 + request_count)
    ]
    cursor = request_ids[REQUESTS_PAGE_SIZE - 1]

    update = make_callback_update(ADMIN_CHAT_ID, f"reqpage:all:{cursor}")
    context = make_context(conn)

    await requests_all_page_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    assert update.callback_query.message.reply_text.await_count == 2


@pytest.mark.asyncio
async def test_requests_all_page_callback_rejects_non_admin(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")

    update = make_callback_update(222, "reqpage:all:0")
    context = make_context(conn)

    await requests_all_page_callback(update, context)

    assert "Недостаточно прав" in update.callback_query.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_requests_command_admin_requires_group_id(conn):
    update = make_message_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = []

    await requests_command(update, context)

    assert "Использование" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_requests_command_admin_with_group_id_sees_requests(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")

    update = make_message_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_b)]

    await requests_command(update, context)

    update.message.reply_text.assert_awaited_once()


from schedule_bot.change_group_commands import build_requests_messages


def testbuild_requests_messages_returns_one_entry_per_pending_request(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    group_b_row = storage.get_group(conn, group_b)

    entries = build_requests_messages(conn, group_b_row)

    assert len(entries) == 1
    request_id, text, keyboard = entries[0]
    assert isinstance(request_id, int)
    assert "A" not in text  # requester label is shown, not the source group name
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert any(b.callback_data.startswith("chgreq:approve:") for b in buttons)


def testbuild_requests_messages_empty_for_group_without_pending(conn):
    group_b = storage.get_group(conn, _make_group(conn, "B"))
    assert build_requests_messages(conn, group_b) == []


@pytest.mark.asyncio
async def test_change_group_picker_hides_not_found_groups(conn):
    """is_verified == -1 ("не найдена в PDF") must be hidden from THIS
    picker too, not only from the subscribe-flow one."""
    group_a = _make_group(conn, "A")
    institute_a = storage.get_group(conn, group_a).institute_id
    link_id = storage.create_institute_link(
        conn, institute_id=institute_a, pdf_url="https://example.com/Bad.pdf",
        group_names=["Bad"], raw_label="Bad", last_seen_at="t",
    )
    bad_group_id = storage.create_group(conn, institute_id=institute_a, institute_link_id=link_id, name="Bad")
    storage.update_group_is_verified(conn, bad_group_id, -1)
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_callback_update(111, f"chggrp:inst:{institute_a}")
    update.callback_query.edit_message_text = AsyncMock()
    context = make_context(conn)

    await change_group_institute_select_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert not any("Bad" in text for text in texts)


@pytest.mark.asyncio
async def test_approve_validates_unverified_target_group_before_switching(conn):
    """Switching into an is_verified == 0 group must run the same
    validation + immediate first sync as a first subscribe; a group whose
    name isn't in the PDF must not be switched into silently."""
    from pathlib import Path

    fixture = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "НЕТ-ТАКОЙ-ГРУППЫ", is_verified=0, pdf_url=fixture)
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_a  # rolled back
    assert storage.get_group(conn, group_b).is_verified == -1
    assert storage.get_group_change_request(conn, request_id).status == "pending"
    context.bot_data["sync_institute_link_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_syncs_unverified_target_group_on_success(conn):
    from pathlib import Path

    fixture = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "Б-Мв-101", is_verified=0, pdf_url=fixture)
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b
    assert storage.get_group(conn, group_b).is_verified == 1
    assert storage.get_group_change_request(conn, request_id).status == "approved"
    context.bot_data["sync_institute_link_fn"].assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_into_stale_link_group_makes_no_network_call(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module

    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B2", is_verified=0)
    link_id = storage.get_group(conn, group_b).institute_link_id
    institute_id = storage.get_group(conn, group_b).institute_id
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")

    monkeypatch.setattr(
        group_commands_module, "_load_pdf_bytes",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("stale link must not be fetched")),
    )

    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_a
    assert storage.get_group_change_request(conn, request_id).status == "pending"


@pytest.mark.asyncio
async def test_change_request_approve_info_kind_resolves_and_notifies_requester(conn):
    request_id = storage.create_group_change_request(
        conn, 111, None, None, "t", kind="new_institute", request_text="Институт X; URL: —"
    )
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_group_change_request(conn, request_id).status == "approved"
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_change_request_approve_info_kind_rejects_non_admin_caller(conn):
    request_id = storage.create_group_change_request(
        conn, 111, None, None, "t", kind="new_group", request_text="Б-Мв-999"
    )
    update = make_callback_update(777, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_group_change_request(conn, request_id).status == "pending"
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_request_reject_info_kind_notifies_requester(conn):
    request_id = storage.create_group_change_request(
        conn, 111, None, None, "t", kind="report", request_text="что-то не так"
    )
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:reject:{request_id}")
    context = make_context(conn)

    await change_request_reject_callback(update, context)

    assert storage.get_group_change_request(conn, request_id).status == "rejected"
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


from schedule_bot.change_group_commands import build_all_requests_messages
from schedule_bot.telegram_utils import TELEGRAM_MESSAGE_LIMIT


def testbuild_all_requests_messages_includes_info_kind_with_request_text(conn):
    storage.create_group_change_request(
        conn, 111, None, None, "t", kind="new_institute", request_text="Институт X; URL: —"
    )

    entries = build_all_requests_messages(conn)

    assert len(entries) == 1
    _, text, keyboard = entries[0]
    assert "Институт X; URL: —" in text
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert any(b.callback_data == "chgreq:approve:1" for b in buttons)


def testbuild_all_requests_messages_truncates_an_overlong_request_text(conn):
    overlong_text = "x" * 4000
    storage.create_group_change_request(conn, 111, None, None, "t", kind="report", request_text=overlong_text)

    entries = build_all_requests_messages(conn)

    assert len(entries) == 1
    _, text, _ = entries[0]
    assert len(text) <= TELEGRAM_MESSAGE_LIMIT
    assert "обрезано" in text


@pytest.mark.asyncio
async def test_change_group_page_callback_tolerates_excluded_chat(conn):
    institute_a = storage.get_group(conn, _make_group(conn, "A")).institute_id
    context = make_context(conn)
    update = make_callback_update(99999, f"chggrp:page:{institute_a}:1")  # 99999 has no member row

    await change_group_page_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_change_group_search_start_callback_sets_pending_action(conn):
    institute_a = storage.get_group(conn, _make_group(conn, "A")).institute_id
    update = make_callback_update(111, f"chggrp:search:{institute_a}")
    context = make_context(conn)
    context.user_data = {}

    from schedule_bot.change_group_commands import change_group_search_start_callback

    await change_group_search_start_callback(update, context)

    pending = context.user_data["pending_menu_action"]
    assert pending["action"] == "change_group_search"
    assert pending["institute_id"] == institute_a
    update.callback_query.message.reply_text.assert_awaited_once()


def test_build_change_group_search_result_filters_by_substring(conn):
    group_a = _make_group(conn, "A")
    institute_a = storage.get_group(conn, group_a).institute_id
    link_id = storage.create_institute_link(
        conn, institute_id=institute_a, pdf_url="https://example.com/C.pdf",
        group_names=["C"], raw_label="C", last_seen_at="t",
    )
    storage.create_group(conn, institute_id=institute_a, institute_link_id=link_id, name="C")

    text, keyboard = build_change_group_search_result(conn, institute_a, group_a, "c")

    assert keyboard is not None
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "C" in texts
    assert "A" not in texts  # excluded current group never matches
    assert "Найдено" in text


def test_build_change_group_search_result_no_matches(conn):
    group_a = _make_group(conn, "A")
    institute_a = storage.get_group(conn, group_a).institute_id

    text, keyboard = build_change_group_search_result(conn, institute_a, group_a, "zzz")

    assert keyboard is None
    assert "не найдено" in text
