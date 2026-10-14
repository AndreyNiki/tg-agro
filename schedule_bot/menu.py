import asyncio
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import elections, storage
from schedule_bot.bot import (
    _apply_alert,
    _apply_notify,
    _apply_remove_starosta,
    _apply_set_starosta,
    _resolve_admin_group,
    build_help_text,
    format_refresh_result,
    schedule_command,
)
from schedule_bot.change_group_commands import (
    _build_all_requests_messages,
    _build_requests_messages,
    build_info_decision_keyboard,
    change_group_command,
)
from schedule_bot.classmates import classmates_command
from schedule_bot.faq import faq_command
from schedule_bot.group_links import refresh_institute_catalog
from schedule_bot.group_commands import build_institute_picker_keyboard, subscribe_command, unsubscribe_command
from schedule_bot.institute_commands import create_institute_and_scan, format_institute_links
from schedule_bot.report import _apply_report
from schedule_bot.starosta_commands import starosta_command, votes_command
from schedule_bot.telegram_utils import parse_positive_int, safe_edit_message_text, send_with_retry

logger = logging.getLogger(__name__)

ROOT_CALLBACK_DATA = "menu:root:_"

PENDING_ACTION_TTL_SECONDS = 300  # 5 minutes — long enough to type a message, short enough that an
# abandoned "Отправьте текст..." prompt can't be reactivated by an unrelated later message.


def _pending_action(action: str, **extra) -> dict:
    return {"action": action, "started_at": datetime.now(timezone.utc).isoformat(), **extra}


async def _notify_admin_of_info_request(context, request_id: int, kind_label: str, request_text: str) -> None:
    keyboard = build_info_decision_keyboard(request_id)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=context.bot_data["admin_chat_id"],
                text=f"Заявка ({kind_label}):\n{request_text}",
                reply_markup=keyboard,
            )
        )
    except Exception as error:
        logger.warning("menu: failed to notify admin of request_id=%s: %s", request_id, error)


def build_root_keyboard(conn, chat_id: int, admin_chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    member = storage.get_member(conn, chat_id)
    if member is not None:
        rows.append([InlineKeyboardButton("📅 Расписание", callback_data="menu:schedule:root")])
        rows.append([InlineKeyboardButton("👥 Группа", callback_data="menu:group:root")])
        rows.append([InlineKeyboardButton("👑 Староста", callback_data="menu:starosta:root")])
    else:
        rows.append([InlineKeyboardButton("🎓 Выбрать институт и группу", callback_data="menu:start:pick")])
    if chat_id == admin_chat_id:
        rows.append([InlineKeyboardButton("⚙️ Админ", callback_data="menu:admin:root")])
    rows.append([InlineKeyboardButton("🆘 Сообщить о проблеме", callback_data="menu:report:start")])
    rows.append([InlineKeyboardButton("❓ FAQ", callback_data="menu:faq:show")])
    rows.append([InlineKeyboardButton("📜 Список команд", callback_data="menu:help:show")])
    return InlineKeyboardMarkup(rows)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_menu_action", None)
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    keyboard = build_root_keyboard(conn, chat_id, context.bot_data["admin_chat_id"])
    await update.message.reply_text("Главное меню:", reply_markup=keyboard)


def _schedule_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Выбрать день", callback_data="menu:schedule:daypicker")],
            [InlineKeyboardButton("🔔 Подписаться/Отписаться", callback_data="menu:schedule:togglesub")],
            [InlineKeyboardButton("◀️ Назад", callback_data=ROOT_CALLBACK_DATA)],
        ]
    )


def _group_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Одногруппники", callback_data="menu:group:classmates")],
            [InlineKeyboardButton("🔄 Сменить группу", callback_data="menu:group:changegroup")],
            [InlineKeyboardButton("🗳 Открытые голосования", callback_data="menu:group:votes")],
            [InlineKeyboardButton("◀️ Назад", callback_data=ROOT_CALLBACK_DATA)],
        ]
    )


async def _handle_schedule_section(update, context, conn, chat_id: int, action: str) -> None:
    query = update.callback_query
    if action == "root":
        await safe_edit_message_text(query, "Расписание:", reply_markup=_schedule_section_keyboard())
        return
    if action == "daypicker":
        await schedule_command(update, context)
        return
    if action == "togglesub":
        member = storage.get_member(conn, chat_id)
        if member is None:
            await query.message.reply_text("Сначала выберите группу: /start")
            return
        if member.subscribed:
            await unsubscribe_command(update, context)
        else:
            await subscribe_command(update, context)
        return


async def _handle_group_section(update, context, conn, chat_id: int, action: str) -> None:
    query = update.callback_query
    if action == "root":
        await safe_edit_message_text(query, "Группа:", reply_markup=_group_section_keyboard())
        return
    if action == "classmates":
        await classmates_command(update, context)
        return
    if action == "changegroup":
        await change_group_command(update, context)
        return
    if action == "votes":
        await votes_command(update, context)
        return


def _starosta_privileges_keyboard(group) -> InlineKeyboardMarkup:
    closed_label = "🔓 Открыть группу" if group.is_closed else "🔒 Закрыть группу"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❌ Исключить участника", callback_data="menu:starosta:exclude")],
            [InlineKeyboardButton(closed_label, callback_data="menu:starosta:toggleclosed")],
            [InlineKeyboardButton("📢 Оповестить группу", callback_data="menu:starosta:notify")],
            [InlineKeyboardButton("📥 Заявки", callback_data="menu:starosta:requests")],
            [InlineKeyboardButton("◀️ Назад", callback_data=ROOT_CALLBACK_DATA)],
        ]
    )


async def _handle_starosta_section(update, context, conn, chat_id: int, action: str) -> None:
    from schedule_bot.classmates import classmates_command

    query = update.callback_query
    group = storage.get_group_where_starosta(conn, chat_id)

    if action == "root":
        if group is None:
            await starosta_command(update, context)
            return
        await safe_edit_message_text(query, "Привилегии старосты:", reply_markup=_starosta_privileges_keyboard(group))
        return

    if group is None:
        await query.message.reply_text("Вы не староста ни одной группы.")
        return

    if action == "exclude":
        await classmates_command(update, context)
        return
    if action == "toggleclosed":
        storage.update_group_closed(conn, group.id, not group.is_closed)
        group = storage.get_group(conn, group.id)
        await safe_edit_message_text(query, "Привилегии старосты:", reply_markup=_starosta_privileges_keyboard(group))
        return
    if action == "requests":
        entries = _build_requests_messages(conn, group)
        if not entries:
            await query.message.reply_text("Нет заявок на рассмотрении.")
            return
        for text, keyboard in entries:
            await query.message.reply_text(text, reply_markup=keyboard)
        return
    if action == "notify":
        context.user_data["pending_menu_action"] = _pending_action("notify", group_id=group.id)
        await query.message.reply_text("Отправьте текст оповещения:")
        return


GROUPS_PER_MENU_PAGE = 8


def _admin_root_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Объявление всем", callback_data="menu:admin:alert")],
            [InlineKeyboardButton("🏫 Институты", callback_data="menu:admin:institutes")],
            [InlineKeyboardButton("🔧 Настройки группы", callback_data="menu:admin:groupsettings")],
            [InlineKeyboardButton("👑 Старосты", callback_data="menu:admin:starostas")],
            [InlineKeyboardButton("🔒 Открытость группы", callback_data="menu:admin:closedtoggle")],
            [InlineKeyboardButton("📥 Заявки", callback_data="menu:admin:requests")],
            [InlineKeyboardButton("◀️ Назад", callback_data=ROOT_CALLBACK_DATA)],
        ]
    )


def _build_admin_group_picker(conn, pick_action: str, page: int = 0) -> InlineKeyboardMarkup:
    # This picker is deliberately cross-institute (admin group settings/
    # starosta/closed-toggle/requests apply per-group, not per-institute),
    # unlike group_commands.py's build_group_picker_keyboard which is
    # institute-scoped for the subscriber-facing flow. storage.list_groups/
    # count_groups now require an institute_id, so we use the unpaginated
    # list_all_groups and paginate in Python instead.
    all_groups = storage.list_all_groups(conn)
    total = len(all_groups)
    offset = page * GROUPS_PER_MENU_PAGE
    groups = all_groups[offset : offset + GROUPS_PER_MENU_PAGE]
    rows = [[InlineKeyboardButton(g.name, callback_data=f"menu:{pick_action}:{g.id}")] for g in groups]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"menu:{pick_action}page:{page - 1}"))
    if offset + GROUPS_PER_MENU_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"menu:{pick_action}page:{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:root")])
    return InlineKeyboardMarkup(rows)


def _group_settings_submenu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Обновить расписание", callback_data=f"menu:refreshgroup:{group_id}")],
            [InlineKeyboardButton("📢 Оповестить группу", callback_data=f"menu:notifygroup:{group_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:groupsettings")],
        ]
    )  # pdf/send-time/interval are institute-level now — see _institute_settings_submenu


def _institute_picker_keyboard(conn) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(i.name, callback_data=f"menu:pickinstitute:{i.id}")] for i in storage.list_institutes(conn)]
    rows.append([InlineKeyboardButton("➕ Создать институт", callback_data="menu:admin:createinstitute")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:root")])
    return InlineKeyboardMarkup(rows)


def _institute_settings_submenu(institute_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("URL каталога", callback_data=f"menu:setinstitutecatalogurl:{institute_id}")],
            [InlineKeyboardButton("Время рассылки", callback_data=f"menu:setinstitutesendtime:{institute_id}")],
            [InlineKeyboardButton("Интервал проверки", callback_data=f"menu:setinstitutecheckinterval:{institute_id}")],
            [InlineKeyboardButton("Интервал скана каталога", callback_data=f"menu:setinstitutecataloginterval:{institute_id}")],
            [InlineKeyboardButton("🔄 Пересканировать сейчас", callback_data=f"menu:refreshinstitutelinks:{institute_id}")],
            [InlineKeyboardButton("📋 Ссылки каталога", callback_data=f"menu:institutelinks:{institute_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:institutes")],
        ]
    )


def _requests_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Все заявки", callback_data="menu:admin:allrequests")],
            [InlineKeyboardButton("🔎 По группе", callback_data="menu:admin:requestsbygroup")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:root")],
        ]
    )


def _starosta_pick_submenu(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Назначить", callback_data=f"menu:setstarosta:{group_id}")],
            [InlineKeyboardButton("Снять", callback_data=f"menu:removestarosta:{group_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:starostas")],
        ]
    )


async def _handle_admin_section(update, context, conn, chat_id: int, admin_chat_id: int, action: str) -> None:
    query = update.callback_query
    if chat_id != admin_chat_id:
        await query.message.reply_text("Недостаточно прав.")
        return

    if action == "root":
        await safe_edit_message_text(query, "Админ-меню:", reply_markup=_admin_root_keyboard())
        return
    if action == "alert":
        context.user_data["pending_menu_action"] = _pending_action("alert")
        await query.message.reply_text("Отправьте текст объявления:")
        return
    if action == "institutes":
        await safe_edit_message_text(query, "Выберите институт:", reply_markup=_institute_picker_keyboard(conn))
        return
    if action == "createinstitute":
        context.user_data["pending_menu_action"] = _pending_action("create_institute_name")
        await query.message.reply_text("Отправьте название нового института:")
        return
    if action == "groupsettings":
        await safe_edit_message_text(query, "Выберите группу:", reply_markup=_build_admin_group_picker(conn, "pickgroup"))
        return
    if action == "starostas":
        await safe_edit_message_text(query,
            "Выберите группу:", reply_markup=_build_admin_group_picker(conn, "pickstarostagroup")
        )
        return
    if action == "closedtoggle":
        await safe_edit_message_text(query,
            "Выберите группу:", reply_markup=_build_admin_group_picker(conn, "toggleclosed")
        )
        return
    if action == "requests":
        await safe_edit_message_text(query, "Заявки:", reply_markup=_requests_submenu_keyboard())
        return
    if action == "requestsbygroup":
        await safe_edit_message_text(query,
            "Выберите группу:", reply_markup=_build_admin_group_picker(conn, "pickrequestsgroup")
        )
        return
    if action == "allrequests":
        entries = _build_all_requests_messages(conn)
        if not entries:
            await query.message.reply_text("Нет заявок на рассмотрении.")
            return
        for text, keyboard in entries:
            await query.message.reply_text(text, reply_markup=keyboard)
        return


async def _handle_group_scoped_pick(update, context, conn, chat_id: int, admin_chat_id: int, action: str, group_id: int) -> None:
    query = update.callback_query
    if chat_id != admin_chat_id:
        await query.message.reply_text("Недостаточно прав.")
        return
    group = storage.get_group(conn, group_id)
    if group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return

    if action == "pickgroup":
        await safe_edit_message_text(query,
            f"Настройки группы «{group.name}»:", reply_markup=_group_settings_submenu(group_id)
        )
        return
    if action == "pickstarostagroup":
        await safe_edit_message_text(query,
            f"Старосты группы «{group.name}»:", reply_markup=_starosta_pick_submenu(group_id)
        )
        return
    if action == "toggleclosed":
        storage.update_group_closed(conn, group.id, not group.is_closed)
        await safe_edit_message_text(query,
            f"Группа «{group.name}»: состояние обновлено.",
            reply_markup=_build_admin_group_picker(conn, "toggleclosed"),
        )
        return
    if action == "pickrequestsgroup":
        entries = _build_requests_messages(conn, group)
        if not entries:
            await query.message.reply_text("Нет заявок на рассмотрении.")
            return
        for text, keyboard in entries:
            await query.message.reply_text(text, reply_markup=keyboard)
        return
    if action == "setstarosta":
        context.user_data["pending_menu_action"] = _pending_action("set_starosta", group_id=group_id)
        await query.message.reply_text("Отправьте chat_id нового старосты:")
        return
    if action == "removestarosta":
        await safe_edit_message_text(query,
            _apply_remove_starosta(conn, group),
            reply_markup=_starosta_pick_submenu(group_id),
        )
        return
    if action == "refreshgroup":
        sync_institute_link_fn = context.bot_data["sync_institute_link_fn"]
        try:
            results = await sync_institute_link_fn(group.institute_link_id)
        except Exception as error:
            await query.message.reply_text(f"Синхронизация не удалась: {error}. Расписание могло устареть.")
            return
        await query.message.reply_text(format_refresh_result(results, group))
        return
    if action == "notifygroup":
        context.user_data["pending_menu_action"] = _pending_action("notify_admin", group_id=group_id)
        await query.message.reply_text("Отправьте текст оповещения:")
        return


async def _handle_institute_scoped_pick(
    update, context, conn, chat_id: int, admin_chat_id: int, action: str, institute_id: int
) -> None:
    query = update.callback_query
    if chat_id != admin_chat_id:
        await query.message.reply_text("Недостаточно прав.")
        return
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        await query.message.reply_text("Этот институт больше не существует.")
        return

    if action == "pickinstitute":
        await safe_edit_message_text(query,
            f"Настройки института «{institute.name}»:", reply_markup=_institute_settings_submenu(institute_id)
        )
        return
    if action == "setinstitutecatalogurl":
        context.user_data["pending_menu_action"] = _pending_action("set_institute_catalog_url", institute_id=institute_id)
        await query.message.reply_text("Отправьте новый url каталога:")
        return
    if action == "setinstitutesendtime":
        context.user_data["pending_menu_action"] = _pending_action("set_institute_send_time", institute_id=institute_id)
        await query.message.reply_text("Отправьте новое время (HH:MM):")
        return
    if action == "setinstitutecheckinterval":
        context.user_data["pending_menu_action"] = _pending_action("set_institute_check_interval", institute_id=institute_id)
        await query.message.reply_text("Отправьте новый интервал проверки в минутах:")
        return
    if action == "setinstitutecataloginterval":
        context.user_data["pending_menu_action"] = _pending_action("set_institute_catalog_interval", institute_id=institute_id)
        await query.message.reply_text("Отправьте новый интервал сканирования каталога в минутах:")
        return
    if action == "institutelinks":
        await query.message.reply_text(format_institute_links(conn, institute_id))
        return
    if action == "refreshinstitutelinks":
        from schedule_bot.sync import catalog_lock_for

        alerts = []
        lock = catalog_lock_for(context.bot_data["_catalog_locks"], institute_id)
        try:
            # asyncio.to_thread + try/except mirror
            # institute_commands.refresh_institute_links_command: the fetch
            # is blocking (~15s) and an unreachable catalog page must not
            # raise out of the callback handler.
            async with lock:
                await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
        except Exception as error:
            await query.message.reply_text(f"Сканирование каталога не удалось: {error}")
            return
        context.bot_data["reschedule_institute_jobs"](institute_id)
        for text in alerts:
            await query.message.reply_text(text)
        await query.message.reply_text(f"Каталог института «{institute.name}» пересканирован.")
        return


async def pending_action_followup(update, context) -> None:
    pending = context.user_data.get("pending_menu_action")
    if pending is None:
        return

    started_at = pending.get("started_at")
    if started_at is not None:
        try:
            parsed_started_at = datetime.fromisoformat(started_at)
        except ValueError:
            is_expired = True
        else:
            age = datetime.now(timezone.utc) - parsed_started_at
            is_expired = age.total_seconds() > PENDING_ACTION_TTL_SECONDS
        if is_expired:
            del context.user_data["pending_menu_action"]
            await update.message.reply_text(
                "Время ожидания истекло, действие отменено. Откройте /menu заново, если оно ещё нужно."
            )
            return

    conn = context.bot_data["conn"]
    action = pending["action"]
    value = update.message.text
    del context.user_data["pending_menu_action"]

    if action == "alert":
        text = await _apply_alert(conn, context.bot, value)
        await update.message.reply_text(text)
        return

    if action == "report":
        await update.message.reply_text(
            await _apply_report(conn, context.bot, context.bot_data["admin_chat_id"], update.effective_chat.id, value)
        )
        return

    if action == "create_institute_name":
        context.user_data["pending_menu_action"] = _pending_action("create_institute_url", name=value)
        await update.message.reply_text("Отправьте url каталога нового института:")
        return

    if action == "create_institute_url":
        name = pending["name"]
        config = context.bot_data["config"]
        alerts: list[str] = []
        institute_id, scan_status = await create_institute_and_scan(conn, config, name, value, alerts.append)
        context.bot_data["reschedule_institute_jobs"](institute_id)
        for text in alerts:
            await update.message.reply_text(text)
        await update.message.reply_text(f"Институт «{name}» создан (id={institute_id}), {scan_status}.")
        return

    if action == "new_institute_name":
        context.user_data["pending_menu_action"] = _pending_action("new_institute_url", name=value)
        await update.message.reply_text(
            "Отправьте URL каталога института (на этой странице должны быть ссылки на страницы форм "
            "обучения — Очная/Очно-заочная/Заочная). Если не знаете — отправьте «-»."
        )
        return

    if action == "new_institute_url":
        name = pending["name"]
        url = "—" if value.strip() == "-" else value
        request_text = f"Институт: {name}\nURL каталога: {url}"
        request_id = storage.create_group_change_request(
            conn, update.effective_chat.id, None, None, datetime.now(timezone.utc).isoformat(),
            kind="new_institute", request_text=request_text,
        )
        await _notify_admin_of_info_request(context, request_id, "добавление института", request_text)
        await update.message.reply_text("Заявка отправлена, администратор её рассмотрит.")
        return

    if action == "new_group_text":
        institute = storage.get_institute(conn, pending["institute_id"])
        institute_name = institute.name if institute is not None else "?"
        request_text = f"Институт: {institute_name}\nОписание: {value}"
        request_id = storage.create_group_change_request(
            conn, update.effective_chat.id, None, None, datetime.now(timezone.utc).isoformat(),
            kind="new_group", request_text=request_text,
        )
        await _notify_admin_of_info_request(context, request_id, "добавление группы", request_text)
        await update.message.reply_text("Заявка отправлена, администратор её рассмотрит.")
        return

    if "institute_id" in pending:
        institute = storage.get_institute(conn, pending["institute_id"])
        if institute is None:
            await update.message.reply_text("Этот институт больше не существует.")
            return
        if action == "set_institute_catalog_url":
            storage.update_institute_catalog_url(conn, institute.id, value)
            await update.message.reply_text(f"Каталог института «{institute.name}» обновлён: {value}")
        elif action == "set_institute_send_time":
            from schedule_bot.bot import _is_valid_hh_mm

            if not _is_valid_hh_mm(value):
                await update.message.reply_text("Неверный формат времени, используйте HH:MM.")
                return
            storage.update_institute_send_time(conn, institute.id, value)
            for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
                context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
            await update.message.reply_text(f"Время рассылки института «{institute.name}» обновлено: {value}")
        elif action == "set_institute_check_interval":
            minutes = parse_positive_int(value)
            if minutes is None:
                await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
                return
            storage.update_institute_check_interval(conn, institute.id, minutes)
            for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
                context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
            await update.message.reply_text(f"Интервал проверки института «{institute.name}» обновлён: {minutes} мин.")
        elif action == "set_institute_catalog_interval":
            minutes = parse_positive_int(value)
            if minutes is None:
                await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
                return
            storage.update_institute_catalog_refresh_interval(conn, institute.id, minutes)
            context.bot_data["reschedule_institute_jobs"](institute.id)
            await update.message.reply_text(
                f"Интервал сканирования каталога института «{institute.name}» обновлён: {minutes} мин."
            )
        return

    group = storage.get_group(conn, pending["group_id"])
    if group is None:
        await update.message.reply_text("Эта группа больше не существует.")
        return

    if action == "set_starosta":
        await update.message.reply_text(await _apply_set_starosta(conn, context.bot, group, value))
    elif action == "notify":
        current_group = storage.get_group_where_starosta(conn, update.effective_chat.id)
        if current_group is None or current_group.id != group.id:
            await update.message.reply_text("Вы больше не староста этой группы.")
            return
        await update.message.reply_text(await _apply_notify(conn, context.bot, group, value))
    elif action == "notify_admin":
        await update.message.reply_text(await _apply_notify(conn, context.bot, group, value))


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    admin_chat_id = context.bot_data["admin_chat_id"]
    _, section, action = query.data.split(":", 2)

    # Clear any stale pending free-text prompt before dispatching navigation.
    # Branches that re-issue a prompt (notify/alert/report/set_* etc.) stash a
    # fresh one right after this point, so this only affects abandoned prompts.
    context.user_data.pop("pending_menu_action", None)

    group_scoped_actions = {
        "pickgroup", "pickstarostagroup", "toggleclosed", "pickrequestsgroup", "setstarosta", "removestarosta",
        "refreshgroup", "notifygroup",
    }
    if section in group_scoped_actions:
        await _handle_group_scoped_pick(update, context, conn, chat_id, admin_chat_id, section, int(action))
        return

    institute_scoped_actions = {
        "pickinstitute", "setinstitutecatalogurl", "setinstitutesendtime",
        "setinstitutecheckinterval", "setinstitutecataloginterval", "refreshinstitutelinks",
        "institutelinks",
    }
    if section in institute_scoped_actions:
        await _handle_institute_scoped_pick(update, context, conn, chat_id, admin_chat_id, section, int(action))
        return

    admin_picker_prompts = {
        "pickgrouppage": "Выберите группу:",
        "pickstarostagrouppage": "Выберите группу:",
        "toggleclosedpage": "Выберите группу:",
        "pickrequestsgrouppage": "Выберите группу:",
    }
    if section in admin_picker_prompts:
        if chat_id != admin_chat_id:
            await query.message.reply_text("Недостаточно прав.")
            return
        pick_action = section[: -len("page")]
        await safe_edit_message_text(query,
            admin_picker_prompts[section],
            reply_markup=_build_admin_group_picker(conn, pick_action, page=int(action)),
        )
        return

    if section == "root":
        await safe_edit_message_text(query, "Главное меню:", reply_markup=build_root_keyboard(conn, chat_id, admin_chat_id))
        return
    if section == "schedule":
        await _handle_schedule_section(update, context, conn, chat_id, action)
        return
    if section == "group":
        await _handle_group_section(update, context, conn, chat_id, action)
        return
    if section == "starosta":
        await _handle_starosta_section(update, context, conn, chat_id, action)
        return
    if section == "admin":
        await _handle_admin_section(update, context, conn, chat_id, admin_chat_id, action)
        return
    if section == "report":
        context.user_data["pending_menu_action"] = _pending_action("report")
        await query.message.reply_text("Отправьте текст сообщения администратору:")
        return
    if section == "faq":
        await faq_command(update, context)
        return
    if section == "start":
        await query.message.reply_text("Выберите институт:", reply_markup=build_institute_picker_keyboard(conn))
        return
    if section == "help":
        await query.message.reply_text(build_help_text(chat_id == admin_chat_id))
        return
