import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import elections, storage
from schedule_bot.bot import _resolve_admin_group, is_admin, sync_visible_commands
from schedule_bot.group_commands import (
    _rollback_membership,
    validate_and_sync_group_on_first_subscribe,
)
from schedule_bot.join_requests import notify_approvers
from schedule_bot.telegram_utils import reply, safe_edit_message_text, safe_edit_reply_markup, send_with_retry

logger = logging.getLogger(__name__)

_INSTITUTE_SELECT_PREFIX = "chggrp:inst:"
_INSTITUTE_PAGE_PREFIX = "chggrp:instpage:"
_SELECT_PREFIX = "chggrp:select:"
_PAGE_PREFIX = "chggrp:page:"  # chggrp:page:<institute_id>:<page>
_SEARCH_PREFIX = "chggrp:search:"
_TO_INSTITUTES_CALLBACK_DATA = "chggrp:toinstitutes"
_CONFIRM_PREFIX = "chgconfirm:"
_ABORT_CALLBACK_DATA = "chgabort"

INSTITUTE_SELECT_CALLBACK_PATTERN = r"^chggrp:inst:\d+$"
INSTITUTE_PAGE_CALLBACK_PATTERN = r"^chggrp:instpage:\d+$"
GROUP_PAGE_CALLBACK_PATTERN = r"^chggrp:page:\d+:\d+$"
SEARCH_CALLBACK_PATTERN = r"^chggrp:search:\d+$"

# How many institutes/groups to list per keyboard page — matches the
# first-subscribe picker's page size so both flows feel consistent.
INSTITUTES_PER_PAGE = 8
GROUPS_PER_PAGE = 8

# Search has no pagination of its own (YAGNI — a typed query is expected to
# narrow the list a lot); this just caps a pathologically broad query (e.g.
# a single common letter) from flooding the chat with buttons.
SEARCH_RESULTS_LIMIT = 20

# How many pending-request messages to send per batch before showing a
# "show more" button, so a pile-up of requests can't flood a chat with
# dozens of messages (and their keyboards) in one go.
REQUESTS_PAGE_SIZE = 10
REQUESTS_GROUP_PAGE_PREFIX = "reqpage:group:"
REQUESTS_ALL_PAGE_PREFIX = "reqpage:all:"
REQUESTS_GROUP_PAGE_CALLBACK_PATTERN = r"^reqpage:group:\d+:\d+$"
REQUESTS_ALL_PAGE_CALLBACK_PATTERN = r"^reqpage:all:\d+$"


def _candidate_groups_for_institute(conn, institute_id: int, exclude_group_id: int):
    # is_verified == -1 means "name not found in the PDF" and MUST stay
    # hidden from every picker (global constraint (e)) — not just the
    # subscribe-flow one.
    return [
        g for g in storage.list_groups(conn, institute_id, limit=1_000_000, offset=0)
        if g.id != exclude_group_id
    ]


def build_change_institute_keyboard(conn, page: int = 0) -> InlineKeyboardMarkup:
    offset = page * INSTITUTES_PER_PAGE
    all_institutes = storage.list_institutes(conn)
    institutes = all_institutes[offset : offset + INSTITUTES_PER_PAGE]

    rows = [
        [InlineKeyboardButton(i.name, callback_data=f"{_INSTITUTE_SELECT_PREFIX}{i.id}")]
        for i in institutes
    ]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{_INSTITUTE_PAGE_PREFIX}{page - 1}"))
    if offset + INSTITUTES_PER_PAGE < len(all_institutes):
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{_INSTITUTE_PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


def build_change_group_keyboard(conn, institute_id: int, exclude_group_id: int, page: int = 0) -> InlineKeyboardMarkup:
    candidates = _candidate_groups_for_institute(conn, institute_id, exclude_group_id)
    offset = page * GROUPS_PER_PAGE
    page_groups = candidates[offset : offset + GROUPS_PER_PAGE]

    rows = [
        [InlineKeyboardButton(g.name, callback_data=f"{_SELECT_PREFIX}{g.id}")]
        for g in page_groups
    ]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{_PAGE_PREFIX}{institute_id}:{page - 1}"))
    if offset + GROUPS_PER_PAGE < len(candidates):
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{_PAGE_PREFIX}{institute_id}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🔍 Поиск по названию", callback_data=f"{_SEARCH_PREFIX}{institute_id}")])
    rows.append([InlineKeyboardButton("◀️ К институтам", callback_data=_TO_INSTITUTES_CALLBACK_DATA)])
    return InlineKeyboardMarkup(rows)


def build_change_group_search_result(
    conn, institute_id: int, exclude_group_id: int, query_text: str
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Filters the institute's candidate groups by a case-insensitive
    substring match. Returns (message_text, keyboard) — keyboard is None
    when there are no matches, so the caller doesn't show an empty
    keyboard."""
    needle = query_text.strip().lower()
    matches = [g for g in _candidate_groups_for_institute(conn, institute_id, exclude_group_id) if needle in g.name.lower()]
    if not needle or not matches:
        return "Совпадений не найдено. Попробуйте другой запрос.", None

    shown = matches[:SEARCH_RESULTS_LIMIT]
    rows = [[InlineKeyboardButton(g.name, callback_data=f"{_SELECT_PREFIX}{g.id}")] for g in shown]
    rows.append([InlineKeyboardButton("◀️ К институтам", callback_data=_TO_INSTITUTES_CALLBACK_DATA)])
    text = f"Найдено групп: {len(matches)}." if len(matches) <= SEARCH_RESULTS_LIMIT else (
        f"Показаны первые {SEARCH_RESULTS_LIMIT} из {len(matches)} найденных групп, уточните запрос."
    )
    return text, InlineKeyboardMarkup(rows)


async def change_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Сначала выберите группу: /start")
        return
    if storage.get_pending_request_for_chat(conn, chat_id) is not None:
        await reply(update, "У вас уже есть заявка на рассмотрении.")
        return
    await reply(update, "Выберите институт:", reply_markup=build_change_institute_keyboard(conn))


async def change_group_institute_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(_INSTITUTE_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    await safe_edit_reply_markup(query, reply_markup=build_change_institute_keyboard(conn, page))


async def change_group_institute_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    institute_id = int(query.data.removeprefix(_INSTITUTE_SELECT_PREFIX))
    conn = context.bot_data["conn"]
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        await query.message.reply_text("Этот институт больше не существует.")
        return
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await query.message.reply_text("Вы больше не в какой-либо группе — используйте /start.")
        return
    await safe_edit_message_text(query,
        f"Институт «{institute.name}». Выберите новую группу:",
        reply_markup=build_change_group_keyboard(conn, institute_id, member.group_id),
    )


async def change_group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    institute_id_str, page_str = query.data.removeprefix(_PAGE_PREFIX).split(":")
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await query.message.reply_text("Вы больше не в какой-либо группе — используйте /start.")
        return
    await safe_edit_reply_markup(query,
        reply_markup=build_change_group_keyboard(conn, int(institute_id_str), member.group_id, int(page_str))
    )


async def change_group_search_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from schedule_bot.menu import _pending_action

    query = update.callback_query
    await query.answer()
    institute_id = int(query.data.removeprefix(_SEARCH_PREFIX))
    context.user_data["pending_menu_action"] = _pending_action("change_group_search", institute_id=institute_id)
    await query.message.reply_text("Отправьте часть названия группы:")


async def change_group_to_institutes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    conn = context.bot_data["conn"]
    await query.message.reply_text("Выберите институт:", reply_markup=build_change_institute_keyboard(conn))


async def change_group_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    target_group_id = int(query.data.removeprefix(_SELECT_PREFIX))
    conn = context.bot_data["conn"]
    target_group = storage.get_group(conn, target_group_id)
    if target_group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"{_CONFIRM_PREFIX}{target_group_id}"),
                InlineKeyboardButton("✖️ Отмена", callback_data=_ABORT_CALLBACK_DATA),
            ]
        ]
    )
    await query.message.reply_text(
        f"Подать заявку на переход в группу «{target_group.name}»?", reply_markup=keyboard
    )


async def change_group_abort_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Отменено.")


async def change_group_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    target_group_id = int(query.data.removeprefix(_CONFIRM_PREFIX))
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return
    # Re-check fresh, immediately before creating the request: this guards against the
    # race between opening the picker (change_group_command's earlier check) and the
    # user confirming here, in case a request was created in between.
    if storage.get_pending_request_for_chat(conn, chat_id) is not None:
        await query.message.reply_text("У вас уже есть заявка на рассмотрении.")
        return
    target_group = storage.get_group(conn, target_group_id)
    if target_group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return

    if chat_id == context.bot_data["admin_chat_id"]:
        # The admin is a super-user: switching groups never waits on
        # anyone's approval, including the admin's own — go straight to
        # the same effects an approved request would have, with no
        # request row and no approver notification.
        result = await _perform_group_switch(conn, context, chat_id, member.group_id, target_group)
        if result == "stale":
            await query.message.reply_text("Группа временно недоступна, попробуйте позже.")
            return
        if result == "invalid":
            await query.message.reply_text("Группа временно недоступна, администратор уведомлён.")
            return
        await query.message.reply_text(f"Вы перешли в группу «{target_group.name}».")
        return

    request_id = storage.create_group_change_request(
        conn, chat_id, member.group_id, target_group_id, datetime.now(timezone.utc).isoformat()
    )
    await notify_approvers(
        context.bot, conn, target_group, request_id, chat_id, context.bot_data["admin_chat_id"],
        from_group_id=member.group_id,
    )
    await query.message.reply_text("Заявка отправлена, ожидайте решения.")


async def _perform_group_switch(conn, context, chat_id: int, from_group_id: int | None, target_group) -> str:
    """Core group-switch effects: membership move, subscribe-time
    validation/sync for an unverified target group (rolled back on
    failure), and clearing a vacated starosta seat. Shared by
    change_request_approve_callback (after human approval) and the admin's
    own change_group confirm (the admin is a super-user and switches
    immediately, without anyone's approval). Returns "ok", "stale" (target
    link vanished/never fetched), or "invalid" (validation against the PDF
    failed)."""
    now = datetime.now(timezone.utc).isoformat()
    previous_member = storage.get_member(conn, chat_id)
    storage.upsert_member(conn, chat_id, target_group.id, joined_at=now, subscribed=True)

    if target_group.is_verified == 0:
        institute_link = storage.get_institute_link(conn, target_group.institute_link_id)
        if institute_link is None or institute_link.is_stale:
            _rollback_membership(conn, chat_id, previous_member)
            await sync_visible_commands(context.bot, conn, chat_id)
            return "stale"

        async def notify_admin(text: str) -> None:
            try:
                await send_with_retry(
                    lambda: context.bot.send_message(chat_id=context.bot_data["admin_chat_id"], text=text)
                )
            except Exception as error:
                logger.warning("change_group_commands: failed to notify admin: %s", error)

        ok = await validate_and_sync_group_on_first_subscribe(
            conn, target_group, context.bot_data["config"].pdf_cache_dir, notify_admin,
            context.bot_data["sync_institute_link_fn"],
        )
        if not ok:
            _rollback_membership(conn, chat_id, previous_member)
            await sync_visible_commands(context.bot, conn, chat_id)
            return "invalid"

    await sync_visible_commands(context.bot, conn, chat_id)

    if from_group_id is not None:
        from_group = storage.get_group(conn, from_group_id)
        if from_group is not None and from_group.starosta_chat_id == chat_id:
            storage.cancel_open_elections_for_group(conn, from_group.id, now)
            storage.update_group_starosta(conn, from_group.id, None)
            storage.set_group_last_starosta_change_at(conn, from_group.id, now)

    return "ok"


def _can_decide(caller_chat_id: int, target_group, admin_chat_id: int) -> bool:
    is_admin_caller = caller_chat_id == admin_chat_id
    is_target_starosta = target_group is not None and target_group.starosta_chat_id == caller_chat_id
    return is_admin_caller or is_target_starosta


def build_info_decision_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Shared by report.py and menu.py's new_institute/new_group dialogs so
    the callback_data format for kind != 'group_change' requests can never
    drift from what change_request_approve/reject_callback expect."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Решено", callback_data=f"chgreq:approve:{request_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"chgreq:reject:{request_id}"),
            ]
        ]
    )


_INFO_REQUEST_NOTIFICATION_TEXT = {
    ("new_institute", "approved"): "Ваша заявка на добавление института рассмотрена.",
    ("new_institute", "rejected"): "Ваша заявка на добавление института отклонена.",
    ("new_group", "approved"): "Ваша заявка на добавление группы рассмотрена.",
    ("new_group", "rejected"): "Ваша заявка на добавление группы отклонена.",
    ("report", "approved"): "Ваша жалоба рассмотрена, спасибо за обращение.",
    ("report", "rejected"): "Ваша жалоба отклонена.",
}


async def _decide_info_request(update, context, request, status: str) -> None:
    """kind != 'group_change' requests (new_institute/new_group/report) carry
    no group to join — deciding one is just a status change plus a
    notification to whoever filed it. Creating the actual institute/group
    stays a manual admin action via the existing commands/menu."""
    query = update.callback_query
    if not is_admin(update, context):
        await query.message.reply_text("Недостаточно прав для решения по этой заявке.")
        return
    now = datetime.now(timezone.utc).isoformat()
    storage.decide_group_change_request(context.bot_data["conn"], request.id, status, now)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=request.chat_id, text=_INFO_REQUEST_NOTIFICATION_TEXT[(request.kind, status)]
            )
        )
    except Exception as error:
        logger.warning("change_group_commands: failed to notify requester chat_id=%s: %s", request.chat_id, error)
    await query.message.reply_text("Заявка отмечена как решённая." if status == "approved" else "Заявка отклонена.")


async def change_request_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":")[2])
    conn = context.bot_data["conn"]
    caller_chat_id = update.effective_chat.id
    request = storage.get_group_change_request(conn, request_id)
    if request is None or request.status != "pending":
        await query.message.reply_text("Эта заявка уже обработана.")
        return
    if request.kind != "group_change":
        await _decide_info_request(update, context, request, "approved")
        return
    target_group = storage.get_group(conn, request.to_group_id)
    if not _can_decide(caller_chat_id, target_group, context.bot_data["admin_chat_id"]):
        await query.message.reply_text("Недостаточно прав для решения по этой заявке.")
        return

    # Same contract as the first-subscribe picker (spec 1.5/4.2): an
    # is_verified == 0 group must be validated against the real PDF and
    # get its immediate first sync before the switch is considered done,
    # and a stale link must never be fetched. The membership row is
    # written first because sync_institute_link only syncs links that
    # have at least one member; it is rolled back if validation fails.
    result = await _perform_group_switch(conn, context, request.chat_id, request.from_group_id, target_group)
    if result == "stale":
        await query.message.reply_text(
            "Группа временно недоступна, попробуйте позже. Заявка осталась на рассмотрении."
        )
        return
    if result == "invalid":
        await query.message.reply_text(
            "Группа временно недоступна, администратор уведомлён. Заявка осталась на рассмотрении."
        )
        return

    now = datetime.now(timezone.utc).isoformat()
    storage.decide_group_change_request(conn, request_id, "approved", now)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=request.chat_id,
                text=f"Ваша заявка на переход в группу «{target_group.name}» одобрена.",
            )
        )
    except Exception as error:
        logger.warning("change_group_commands: failed to notify requester chat_id=%s: %s", request.chat_id, error)
    await query.message.reply_text("Заявка одобрена.")


async def change_request_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":")[2])
    conn = context.bot_data["conn"]
    caller_chat_id = update.effective_chat.id
    request = storage.get_group_change_request(conn, request_id)
    if request is None or request.status != "pending":
        await query.message.reply_text("Эта заявка уже обработана.")
        return
    if request.kind != "group_change":
        await _decide_info_request(update, context, request, "rejected")
        return
    target_group = storage.get_group(conn, request.to_group_id)
    if not _can_decide(caller_chat_id, target_group, context.bot_data["admin_chat_id"]):
        await query.message.reply_text("Недостаточно прав для решения по этой заявке.")
        return

    now = datetime.now(timezone.utc).isoformat()
    storage.decide_group_change_request(conn, request_id, "rejected", now)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=request.chat_id,
                text=f"Ваша заявка на переход в группу «{target_group.name}» отклонена.",
            )
        )
    except Exception as error:
        logger.warning("change_group_commands: failed to notify requester chat_id=%s: %s", request.chat_id, error)
    await query.message.reply_text("Заявка отклонена.")


def build_requests_messages(conn, group) -> list[tuple[int, str, "InlineKeyboardMarkup"]]:
    """Returns (request_id, text, keyboard) triples — the id is the
    keyset-pagination cursor send_requests_batch uses, since a plain
    numeric offset would silently skip entries once something earlier
    in the list gets approved/rejected between page loads."""
    entries = []
    for request in storage.list_pending_requests_for_group(conn, group.id):
        requester = storage.get_member(conn, request.chat_id)
        label = elections.format_candidate_label(requester) if requester is not None else "Неизвестный"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Одобрить", callback_data=f"chgreq:approve:{request.id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"chgreq:reject:{request.id}"),
                ]
            ]
        )
        verb = "вступление" if request.from_group_id is None else "переход"
        entries.append((request.id, f"Заявка от {label} на {verb} в группу «{group.name}».", keyboard))
    return entries


_INFO_REQUEST_KIND_LABEL = {
    "new_institute": "добавление института",
    "new_group": "добавление группы",
    "report": "жалоба",
}

# request_text is raw, Telegram-message-sized user input (/report, "моего
# института/группы нет"); a message near Telegram's own 4096-char cap
# plus this prefix would exceed it and raise BadRequest. Truncating
# comfortably below the real limit leaves slack for the prefix/label —
# used both in the batch listings below AND wherever request_text gets
# forwarded to the admin immediately (report.py, menu.py's
# _notify_admin_of_info_request), so one oversized submission can't
# silently fail to reach the admin at all, which a listing-only
# truncation would still allow. Telegram's 4096 cap counts UTF-16 code
# units, not Python characters — this stays far enough below it that
# astral-plane characters (rare in practice here) wouldn't close the
# gap either.
REQUEST_TEXT_DISPLAY_LIMIT = 3500


def truncate_request_text(text: str | None) -> str:
    if not text:
        return ""
    if len(text) <= REQUEST_TEXT_DISPLAY_LIMIT:
        return text
    return text[:REQUEST_TEXT_DISPLAY_LIMIT] + "…\n\n(сообщение обрезано, слишком длинное)"


def build_all_requests_messages(conn) -> list[tuple[int, str, "InlineKeyboardMarkup"]]:
    """Returns (request_id, text, keyboard) triples — see
    build_requests_messages for why the id (not a numeric position) is
    the pagination cursor."""
    entries = []
    for request in storage.list_all_pending_requests(conn):
        if request.kind != "group_change":
            requester = storage.get_member(conn, request.chat_id)
            label = elections.format_candidate_label(requester) if requester is not None else "Неизвестный"
            keyboard = build_info_decision_keyboard(request.id)
            kind_label = _INFO_REQUEST_KIND_LABEL[request.kind]
            entries.append(
                (
                    request.id,
                    f"Заявка ({kind_label}) от {label}:\n{truncate_request_text(request.request_text)}",
                    keyboard,
                )
            )
            continue

        group = storage.get_group(conn, request.to_group_id)
        if group is None:
            continue
        requester = storage.get_member(conn, request.chat_id)
        label = elections.format_candidate_label(requester) if requester is not None else "Неизвестный"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Одобрить", callback_data=f"chgreq:approve:{request.id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"chgreq:reject:{request.id}"),
                ]
            ]
        )
        verb = "вступление" if request.from_group_id is None else "переход"
        entries.append((request.id, f"Заявка от {label} на {verb} в группу «{group.name}».", keyboard))
    return entries


async def send_requests_batch(reply_target, entries, after_id: int, more_callback_data_prefix: str) -> None:
    """Sends the entries whose id comes after `after_id` (a keyset
    cursor, not a numeric offset — entries is rebuilt fresh from the DB
    on every page, and an offset would silently skip requests once an
    earlier one in the list gets approved/rejected between page loads),
    up to REQUESTS_PAGE_SIZE of them, each as its own message with its
    own approve/reject keyboard. If more remain, one extra message with
    a "show more" button carries the new cursor forward."""
    if not entries:
        await reply_target("Нет заявок на рассмотрении.")
        return
    remaining = [entry for entry in entries if entry[0] > after_id]
    if not remaining:
        await reply_target("Нет новых заявок на рассмотрении.")
        return
    page = remaining[:REQUESTS_PAGE_SIZE]
    for _, text, keyboard in page:
        await reply_target(text, reply_markup=keyboard)
    last_id = page[-1][0]
    if len(remaining) > REQUESTS_PAGE_SIZE:
        more_keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("➡️ Показать ещё", callback_data=f"{more_callback_data_prefix}{last_id}")]]
        )
        # Absolute position against the full list (not just `remaining`,
        # which shrinks as earlier requests get resolved), so repeated
        # "show more" taps still read as "10 of 40", "20 of 40", ...
        # instead of a shrinking, position-less "10 of 30", "10 of 20".
        shown_so_far = len(entries) - len(remaining) + len(page)
        await reply_target(
            f"Показано {shown_so_far} из {len(entries)} заявок.", reply_markup=more_keyboard
        )


async def requests_command(update, context) -> None:
    conn = context.bot_data["conn"]
    if is_admin(update, context):
        if not context.args:
            await update.message.reply_text("Использование: /requests <group_id>")
            return
        group = _resolve_admin_group(conn, context.args[0])
        if group is None:
            await update.message.reply_text("Группа с таким id не найдена.")
            return
    else:
        group = storage.get_group_where_starosta(conn, update.effective_chat.id)
        if group is None:
            await update.message.reply_text("Вы не староста ни одной группы.")
            return

    entries = build_requests_messages(conn, group)
    await send_requests_batch(update.message.reply_text, entries, 0, f"{REQUESTS_GROUP_PAGE_PREFIX}{group.id}:")


async def requests_group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, _, group_id_str, after_id_str = query.data.split(":")
    group_id, after_id = int(group_id_str), int(after_id_str)
    conn = context.bot_data["conn"]
    group = storage.get_group(conn, group_id)
    if group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return
    if not _can_decide(update.effective_chat.id, group, context.bot_data.get("admin_chat_id")):
        await query.message.reply_text("Недостаточно прав.")
        return
    entries = build_requests_messages(conn, group)
    await send_requests_batch(query.message.reply_text, entries, after_id, f"{REQUESTS_GROUP_PAGE_PREFIX}{group.id}:")


async def requests_all_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    after_id = int(query.data.removeprefix(REQUESTS_ALL_PAGE_PREFIX))
    if update.effective_chat.id != context.bot_data.get("admin_chat_id"):
        await query.message.reply_text("Недостаточно прав.")
        return
    conn = context.bot_data["conn"]
    entries = build_all_requests_messages(conn)
    await send_requests_batch(query.message.reply_text, entries, after_id, REQUESTS_ALL_PAGE_PREFIX)
