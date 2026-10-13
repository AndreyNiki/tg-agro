import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.join_requests import create_join_request_and_notify, requires_approval
from schedule_bot.pdf_parser import _load_pdf_bytes, find_header_column_index, extract_raw_rows
from schedule_bot.telegram_utils import reply

logger = logging.getLogger(__name__)

INSTITUTES_PER_PAGE = 8
GROUPS_PER_PAGE = 8

INSTITUTE_SELECT_PREFIX = "inst:select:"
INSTITUTE_PAGE_PREFIX = "inst:page:"
SELECT_PREFIX = "grp:select:"
PAGE_PREFIX = "grp:page:"

INSTITUTE_SELECT_CALLBACK_PATTERN = r"^inst:select:\d+$"
INSTITUTE_PAGE_CALLBACK_PATTERN = r"^inst:page:\d+$"
SELECT_CALLBACK_PATTERN = r"^grp:select:\d+:\d+$"
PAGE_CALLBACK_PATTERN = r"^grp:page:\d+:\d+$"


def _format_telegram_user_label(user) -> str:
    """Mirrors elections.format_candidate_label's precedence/formatting,
    but builds the label straight from the Telegram User object instead of
    a `members` row — needed here because no `members` row exists yet for
    a first-time joiner awaiting approval."""
    display_name = user.full_name
    username = user.username
    if display_name and username:
        return f"{display_name} (@{username})"
    if display_name:
        return display_name
    if username:
        return f"@{username}"
    return "Неизвестный"


def build_institute_picker_keyboard(conn, page: int = 0) -> InlineKeyboardMarkup:
    offset = page * INSTITUTES_PER_PAGE
    all_institutes = storage.list_institutes(conn)
    institutes = all_institutes[offset : offset + INSTITUTES_PER_PAGE]
    total = len(all_institutes)

    rows = [[InlineKeyboardButton(i.name, callback_data=f"{INSTITUTE_SELECT_PREFIX}{i.id}")] for i in institutes]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{INSTITUTE_PAGE_PREFIX}{page - 1}"))
    if offset + INSTITUTES_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{INSTITUTE_PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


def build_group_picker_keyboard(conn, institute_id: int, page: int = 0) -> InlineKeyboardMarkup:
    offset = page * GROUPS_PER_PAGE
    groups = storage.list_groups(conn, institute_id, limit=GROUPS_PER_PAGE, offset=offset)
    total = storage.count_groups(conn, institute_id)

    rows = [
        [InlineKeyboardButton(group.name, callback_data=f"{SELECT_PREFIX}{institute_id}:{group.id}")]
        for group in groups
    ]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{PAGE_PREFIX}{institute_id}:{page - 1}"))
    if offset + GROUPS_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{PAGE_PREFIX}{institute_id}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    return InlineKeyboardMarkup(rows)


async def institute_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(INSTITUTE_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    await query.message.edit_reply_markup(reply_markup=build_institute_picker_keyboard(conn, page))


async def institute_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    institute_id = int(query.data.removeprefix(INSTITUTE_SELECT_PREFIX))
    conn = context.bot_data["conn"]
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        await query.message.reply_text("Этот институт больше не существует.")
        return
    await query.message.edit_text(
        f"Институт «{institute.name}». Выберите группу:",
        reply_markup=build_group_picker_keyboard(conn, institute_id),
    )


async def group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    institute_id_str, page_str = query.data.removeprefix(PAGE_PREFIX).split(":")
    conn = context.bot_data["conn"]
    await query.message.edit_reply_markup(
        reply_markup=build_group_picker_keyboard(conn, int(institute_id_str), int(page_str))
    )


def _blocking_validate_group_names(pdf_url: str, group_names: list[str]) -> dict[str, bool]:
    """Column lookup here MUST use the same function the real sync
    pipeline uses (`find_header_column_index`, via
    `pdf_parser.parse_schedules_for_groups`) — not the whole-table
    `find_group_column_index`. Otherwise a name that only appears in
    lesson body text ("совм. с Б-А-301") passes validation
    (`is_verified=1`, stays in the picker) and then fails every single
    later sync forever, with no path back to `is_verified=-1`."""
    pdf_bytes = _load_pdf_bytes(pdf_url)
    tmp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp_file.write(pdf_bytes)
        tmp_file.close()
        rows, _ = extract_raw_rows(tmp_file.name)
        found = {}
        for name in group_names:
            try:
                find_header_column_index(rows, name)
                found[name] = True
            except ValueError:
                found[name] = False
        return found
    finally:
        try:
            os.unlink(tmp_file.name)
        except OSError:
            pass


async def validate_and_sync_group_on_first_subscribe(conn, group, pdf_cache_dir: str, notify_admin, sync_institute_link_fn) -> bool:
    """Design spec section 1.5/4.2: a group straight from catalog scanning
    (is_verified == 0) is unproven until someone actually tries to
    subscribe. On that first attempt, validate every sibling group of the
    same institute_link against the real PDF in one pass (one
    download+parse, reused by the immediate first sync that follows on
    success). Already-verified groups (1 or -1) skip this entirely."""
    if group.is_verified != 0:
        return group.is_verified == 1

    institute_link = storage.get_institute_link(conn, group.institute_link_id)
    if institute_link is None or institute_link.is_stale:
        # Binding global constraint: "Устаревшие ссылки никогда не
        # триггерят сетевых запросов." A stale link means the catalog scan
        # no longer lists this PDF — the admin was already alerted exactly
        # once at the moment it went stale (spec 1.3), so this routine,
        # expected state must NOT produce another alert, and must not
        # fetch anything. The caller just tells the user the group is
        # temporarily unavailable.
        logger.info(
            "group_commands: subscribe to group %r refused without any network call — its institute_link is stale/missing",
            group.name,
        )
        return False

    sibling_groups = storage.list_groups_for_link(conn, institute_link.id)
    sibling_names = [g.name for g in sibling_groups]

    try:
        found = await asyncio.to_thread(_blocking_validate_group_names, institute_link.pdf_url, sibling_names)
    except Exception as error:
        logger.warning("group_commands: validation fetch failed for link %r: %s", institute_link.pdf_url, error)
        await notify_admin(f"Не удалось скачать PDF для проверки группы «{group.name}»: {error}")
        return False

    for sibling in sibling_groups:
        storage.update_group_is_verified(conn, sibling.id, 1 if found.get(sibling.name) else -1)

    if not found.get(group.name):
        await notify_admin(f"Группа «{group.name}» не найдена в PDF по ссылке института — is_verified=-1, скрыта из пикера.")
        return False

    await sync_institute_link_fn(institute_link.id)
    return True


def _rollback_membership(conn, chat_id: int, previous_member) -> None:
    """Undo the speculative upsert_member done before validation: either
    delete the brand-new row (no previous membership) or put the chat back
    into exactly the group/subscription state it had before."""
    if previous_member is None:
        storage.remove_member(conn, chat_id)
        return
    storage.upsert_member(
        conn, chat_id, previous_member.group_id,
        joined_at=previous_member.joined_at, subscribed=previous_member.subscribed,
    )


async def group_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # callback_data shape is "grp:select:<institute_id>:<group_id>"
    institute_id_str, group_id_str = query.data.removeprefix(SELECT_PREFIX).split(":")
    group_id = int(group_id_str)
    conn = context.bot_data["conn"]
    group = storage.get_group(conn, group_id)
    if group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return

    chat_id = update.effective_chat.id

    if requires_approval(conn, group, chat_id):
        if storage.get_pending_request_for_chat(conn, chat_id) is not None:
            await query.message.reply_text("У вас уже есть заявка на рассмотрении.")
            return
        requester_label = _format_telegram_user_label(update.effective_user)
        await create_join_request_and_notify(
            context.bot, conn, chat_id, None, group, context.bot_data["admin_chat_id"],
            requester_label=requester_label,
        )
        await query.message.reply_text("Эта группа закрыта или ограничена для вас — заявка отправлена, ожидайте решения.")
        return

    async def notify_admin(text: str) -> None:
        try:
            await context.bot.send_message(chat_id=context.bot_data["admin_chat_id"], text=text)
        except Exception as error:
            logger.warning("group_commands: failed to notify admin: %s", error)

    # Stale link: never fetch, never alert (spec 1.3 — the admin already
    # got one alert when the link disappeared from the catalog), and don't
    # even write a membership row. Validation enforces this too, but doing
    # it here keeps the user-facing wording honest (no "администратор
    # уведомлён" claim when no alert was sent).
    institute_link = storage.get_institute_link(conn, group.institute_link_id)
    if group.is_verified == 0 and (institute_link is None or institute_link.is_stale):
        await query.message.reply_text("Группа временно недоступна, попробуйте позже.")
        return

    # The membership row MUST be written BEFORE validation/first sync, not
    # after: sync_institute_link only syncs links that have at least one
    # group with at least one member (spec 3.2 p.1). With the opposite
    # order the very first subscriber's immediate first sync was a
    # guaranteed no-op (active_groups == [] -> early return, no fetch), so
    # /schedule stayed empty until the next scheduled check. On failure we
    # roll the membership back to exactly its previous state below.
    previous_member = storage.get_member(conn, chat_id)
    storage.upsert_member(conn, chat_id, group_id, joined_at=datetime.now(timezone.utc).isoformat(), subscribed=True)
    storage.update_member_profile(
        conn, chat_id,
        telegram_username=update.effective_user.username,
        display_name=update.effective_user.full_name,
    )

    ok = await validate_and_sync_group_on_first_subscribe(
        conn, group, context.bot_data["config"].pdf_cache_dir, notify_admin,
        context.bot_data["sync_institute_link_fn"],
    )
    if not ok:
        _rollback_membership(conn, chat_id, previous_member)
        await query.message.reply_text("Группа временно недоступна, администратор уведомлён.")
        return

    await query.message.reply_text(
        f"Вы подписались на рассылку расписания группы «{group.name}». "
        "Команда /schedule покажет расписание на выбранный день."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    logger.info("group_commands: /start from chat_id=%s", chat_id)
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Выберите институт:", reply_markup=build_institute_picker_keyboard(conn))
        return

    group = storage.get_group(conn, member.group_id)
    group_name = group.name if group else "—"
    status = "подписаны на рассылку" if member.subscribed else "отписаны от рассылки (группа остаётся закреплённой)"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Меню", callback_data="menu:root:_")]])
    await update.message.reply_text(
        f"Вы {status} группы «{group_name}». Команда /schedule покажет расписание на выбранный день.",
        reply_markup=keyboard,
    )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Сначала выберите группу: /start")
        return
    storage.set_member_subscribed(conn, chat_id, True)
    await reply(update, "Рассылка расписания включена.")


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Вы ещё не выбрали группу: /start")
        return
    storage.set_member_subscribed(conn, chat_id, False)
    await reply(update, "Рассылка отключена. Группа остаётся закреплённой, /schedule продолжит работать.")
