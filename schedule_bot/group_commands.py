import logging
import sqlite3
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
from schedule_bot.pdf_parser import _load_pdf_bytes

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 8

SELECT_PREFIX = "grp:select:"
PAGE_PREFIX = "grp:page:"
CREATE_CALLBACK_DATA = "grp:create"

SELECT_CALLBACK_PATTERN = r"^grp:select:\d+$"
PAGE_CALLBACK_PATTERN = r"^grp:page:\d+$"


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
        pdf_source_url = resolve_group_pdf_source(source_page_url, group_name)
        _load_pdf_bytes(pdf_source_url)
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
