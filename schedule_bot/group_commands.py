import asyncio
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from schedule_bot import storage
from schedule_bot.group_links import GroupLinkNotFoundError, SourcePageUnreachableError, resolve_group_pdf_source
from schedule_bot.join_requests import create_join_request_and_notify, requires_approval
from schedule_bot.pdf_parser import _load_pdf_bytes, parse_schedule
from schedule_bot.telegram_utils import reply

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 8

SELECT_PREFIX = "grp:select:"
PAGE_PREFIX = "grp:page:"
CREATE_CALLBACK_DATA = "grp:create"

SELECT_CALLBACK_PATTERN = r"^grp:select:\d+$"
PAGE_CALLBACK_PATTERN = r"^grp:page:\d+$"


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


def build_group_picker_keyboard(conn, page: int = 0) -> InlineKeyboardMarkup:
    offset = page * GROUPS_PER_PAGE
    groups = storage.list_groups(conn, limit=GROUPS_PER_PAGE, offset=offset)
    total = storage.count_groups(conn)

    rows = [[InlineKeyboardButton(group.name, callback_data=f"{SELECT_PREFIX}{group.id}")] for group in groups]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{PAGE_PREFIX}{page - 1}"))
    if offset + GROUPS_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("➕ Создать группу", callback_data=CREATE_CALLBACK_DATA)])
    return InlineKeyboardMarkup(rows)


async def group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(PAGE_PREFIX))
    conn = context.bot_data["conn"]
    await query.message.edit_reply_markup(reply_markup=build_group_picker_keyboard(conn, page))


async def group_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.removeprefix(SELECT_PREFIX))
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

    storage.upsert_member(conn, chat_id, group_id, joined_at=datetime.now(timezone.utc).isoformat(), subscribed=True)
    storage.update_member_profile(
        conn, chat_id,
        telegram_username=update.effective_user.username,
        display_name=update.effective_user.full_name,
    )
    await query.message.reply_text(
        f"Вы подписались на рассылку расписания группы «{group.name}». "
        "Команда /schedule покажет расписание на выбранный день."
    )


ASK_GROUP_NAME, ASK_SOURCE_PAGE_URL = range(2)


async def start_group_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Введите название новой группы:")
    return ASK_GROUP_NAME


async def receive_group_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_group_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Пришлите ссылку на страницу со списком ссылок на PDF-расписания "
        "(ту, где опубликованы расписания всех групп)."
    )
    return ASK_SOURCE_PAGE_URL


def _resolve_and_validate_pdf_source(source_page_url: str, group_name: str) -> str:
    """Resolves the PDF link for group_name from the source page, then
    fetches and parses the PDF to confirm group_name actually exists as a
    column in the table. This is a stricter check than link resolution
    itself: group_links.py matches the group name against the PDF *link*
    case-insensitively, while pdf_parser.find_group_column_index matches
    it against the table *header* case-sensitively — a name that passes
    the former can still fail the latter, which would otherwise create a
    group that can never sync successfully. Runs entirely off the event
    loop (see check_and_apply_diff in sync.py for the same pattern): both
    the link resolution and the PDF fetch+parse are blocking network/CPU
    calls."""
    pdf_source_url = resolve_group_pdf_source(source_page_url, group_name)
    pdf_bytes = _load_pdf_bytes(pdf_source_url)
    tmp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp_file.write(pdf_bytes)
        tmp_file.close()
        parse_schedule(tmp_file.name, group_name)
    finally:
        try:
            os.unlink(tmp_file.name)
        except OSError:
            pass
    return pdf_source_url


async def receive_source_page_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_name = context.user_data["new_group_name"]
    source_page_url = update.message.text.strip()
    conn = context.bot_data["conn"]

    if storage.get_group_by_name(conn, group_name) is not None:
        await update.message.reply_text(
            f"Группа «{group_name}» уже существует, выберите её в списке через /start."
        )
        return ConversationHandler.END

    try:
        # Blocking network fetch + pdfplumber parsing — offload to a worker
        # thread so a slow/unreachable source page never freezes the whole
        # bot's event loop for every chat, not just this conversation.
        pdf_source_url = await asyncio.to_thread(
            _resolve_and_validate_pdf_source, source_page_url, group_name
        )
    except SourcePageUnreachableError:
        await update.message.reply_text(
            "Не удалось открыть страницу со ссылками на расписания. Проверьте ссылку и пришлите её ещё раз."
        )
        return ASK_SOURCE_PAGE_URL
    except GroupLinkNotFoundError:
        await update.message.reply_text(
            f"На этой странице не нашлось ссылки на расписание группы «{group_name}». "
            "Проверьте название группы или пришлите другую ссылку на страницу."
        )
        return ASK_SOURCE_PAGE_URL
    except ValueError:
        # Raised by pdf_parser.find_group_column_index: the PDF was
        # reachable and parseable, but group_name isn't an actual column
        # header in it — the link matched something else by substring.
        await update.message.reply_text(
            f"Группа «{group_name}» не найдена в расписании по этой ссылке — "
            "проверьте точное название группы."
        )
        return ASK_SOURCE_PAGE_URL
    except Exception as error:
        logger.warning("group_commands: pdf link found but unreachable: %s", error)
        await update.message.reply_text(
            f"Ссылка на расписание группы «{group_name}» найдена, но PDF не открывается. "
            "Проверьте ссылку на страницу и пришлите её ещё раз."
        )
        return ASK_SOURCE_PAGE_URL

    config = context.bot_data["config"]
    try:
        group_id = storage.create_group(
            conn,
            name=group_name,
            source_page_url=source_page_url,
            pdf_source_url=pdf_source_url,
            send_time=config.default_daily_send_time,
            check_interval_minutes=config.default_check_interval_minutes,
            tz_name=config.default_timezone,
        )
    except sqlite3.IntegrityError:
        # Guards against a race with another concurrent creation of the same
        # group name slipping past the get_group_by_name pre-check above —
        # groups.name is UNIQUE, so this must never surface as a raw crash.
        await update.message.reply_text(
            f"Группа «{group_name}» уже существует, выберите её в списке через /start."
        )
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    storage.upsert_member(conn, chat_id, group_id, joined_at=datetime.now(timezone.utc).isoformat(), subscribed=True)
    storage.update_member_profile(
        conn, chat_id,
        telegram_username=update.effective_user.username,
        display_name=update.effective_user.full_name,
    )
    context.bot_data["reschedule_group_jobs"](group_id)
    await update.message.reply_text(f"Группа «{group_name}» создана, вы подписаны на неё.")
    return ConversationHandler.END


async def cancel_group_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Создание группы отменено.")
    return ConversationHandler.END


group_creation_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(start_group_creation, pattern=f"^{CREATE_CALLBACK_DATA}$")],
    states={
        ASK_GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_group_name)],
        ASK_SOURCE_PAGE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_source_page_url)],
    },
    fallbacks=[CommandHandler("cancel", cancel_group_creation)],
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    logger.info("group_commands: /start from chat_id=%s", chat_id)
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Выберите свою группу:", reply_markup=build_group_picker_keyboard(conn))
        return

    group = storage.get_group(conn, member.group_id)
    group_name = group.name if group else "—"
    status = "подписаны на рассылку" if member.subscribed else "отписаны от рассылки (группа закреплена)"
    await update.message.reply_text(
        f"Вы {status} группы «{group_name}». Команда /schedule покажет расписание на выбранный день."
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
    await reply(update,
        "Рассылка отключена. Группа остаётся закреплённой, /schedule продолжит работать."
    )
