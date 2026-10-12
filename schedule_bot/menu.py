from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import elections, storage
from schedule_bot.bot import (
    _apply_alert,
    _apply_notify,
    _apply_remove_starosta,
    _apply_set_starosta,
    _resolve_admin_group,
    schedule_command,
)
from schedule_bot.change_group_commands import _build_requests_messages, change_group_command
from schedule_bot.classmates import classmates_command
from schedule_bot.faq import faq_command
from schedule_bot.group_commands import subscribe_command, unsubscribe_command
from schedule_bot.report import _apply_report
from schedule_bot.starosta_commands import starosta_command, votes_command

ROOT_CALLBACK_DATA = "menu:root:_"


def build_root_keyboard(conn, chat_id: int, admin_chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    member = storage.get_member(conn, chat_id)
    if member is not None:
        rows.append([InlineKeyboardButton("📅 Расписание", callback_data="menu:schedule:root")])
        rows.append([InlineKeyboardButton("👥 Группа", callback_data="menu:group:root")])
        rows.append([InlineKeyboardButton("👑 Староста", callback_data="menu:starosta:root")])
    if chat_id == admin_chat_id:
        rows.append([InlineKeyboardButton("⚙️ Админ", callback_data="menu:admin:root")])
    rows.append([InlineKeyboardButton("🆘 Сообщить о проблеме", callback_data="menu:report:start")])
    rows.append([InlineKeyboardButton("❓ FAQ", callback_data="menu:faq:show")])
    return InlineKeyboardMarkup(rows)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await query.edit_message_text("Расписание:", reply_markup=_schedule_section_keyboard())
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
        await query.edit_message_text("Группа:", reply_markup=_group_section_keyboard())
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
        await query.edit_message_text("Привилегии старосты:", reply_markup=_starosta_privileges_keyboard(group))
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
        await query.edit_message_text("Привилегии старосты:", reply_markup=_starosta_privileges_keyboard(group))
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
        context.user_data["pending_menu_action"] = {"action": "notify", "group_id": group.id}
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
    offset = page * GROUPS_PER_MENU_PAGE
    groups = storage.list_groups(conn, limit=GROUPS_PER_MENU_PAGE, offset=offset)
    total = storage.count_groups(conn)
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
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:groupsettings")],
        ]
    )  # pdf/send-time/interval are institute-level now — see _institute_settings_submenu


def _institute_picker_keyboard(conn) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(i.name, callback_data=f"menu:pickinstitute:{i.id}")] for i in storage.list_institutes(conn)]
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
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:admin:institutes")],
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
        await query.edit_message_text("Админ-меню:", reply_markup=_admin_root_keyboard())
        return
    if action == "alert":
        context.user_data["pending_menu_action"] = {"action": "alert"}
        await query.message.reply_text("Отправьте текст объявления:")
        return
    if action == "institutes":
        await query.edit_message_text("Выберите институт:", reply_markup=_institute_picker_keyboard(conn))
        return
    if action == "groupsettings":
        await query.edit_message_text("Выберите группу:", reply_markup=_build_admin_group_picker(conn, "pickgroup"))
        return
    if action == "starostas":
        await query.edit_message_text(
            "Выберите группу:", reply_markup=_build_admin_group_picker(conn, "pickstarostagroup")
        )
        return
    if action == "closedtoggle":
        await query.edit_message_text(
            "Выберите группу:", reply_markup=_build_admin_group_picker(conn, "toggleclosed")
        )
        return
    if action == "requests":
        await query.edit_message_text(
            "Выберите группу:", reply_markup=_build_admin_group_picker(conn, "pickrequestsgroup")
        )
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
        await query.edit_message_text(
            f"Настройки группы «{group.name}»:", reply_markup=_group_settings_submenu(group_id)
        )
        return
    if action == "pickstarostagroup":
        await query.edit_message_text(
            f"Старосты группы «{group.name}»:", reply_markup=_starosta_pick_submenu(group_id)
        )
        return
    if action == "toggleclosed":
        storage.update_group_closed(conn, group.id, not group.is_closed)
        await query.edit_message_text(
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
        context.user_data["pending_menu_action"] = {"action": "set_starosta", "group_id": group_id}
        await query.message.reply_text("Отправьте chat_id нового старосты:")
        return
    if action == "removestarosta":
        await query.edit_message_text(
            _apply_remove_starosta(conn, group),
            reply_markup=_starosta_pick_submenu(group_id),
        )
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
        await query.edit_message_text(
            f"Настройки института «{institute.name}»:", reply_markup=_institute_settings_submenu(institute_id)
        )
        return
    if action == "setinstitutecatalogurl":
        context.user_data["pending_menu_action"] = {"action": "set_institute_catalog_url", "institute_id": institute_id}
        await query.message.reply_text("Отправьте новый url каталога:")
        return
    if action == "setinstitutesendtime":
        context.user_data["pending_menu_action"] = {"action": "set_institute_send_time", "institute_id": institute_id}
        await query.message.reply_text("Отправьте новое время (HH:MM):")
        return
    if action == "setinstitutecheckinterval":
        context.user_data["pending_menu_action"] = {"action": "set_institute_check_interval", "institute_id": institute_id}
        await query.message.reply_text("Отправьте новый интервал проверки в минутах:")
        return
    if action == "setinstitutecataloginterval":
        context.user_data["pending_menu_action"] = {"action": "set_institute_catalog_interval", "institute_id": institute_id}
        await query.message.reply_text("Отправьте новый интервал сканирования каталога в минутах:")
        return
    if action == "refreshinstitutelinks":
        from schedule_bot.group_links import refresh_institute_catalog

        alerts = []
        refresh_institute_catalog(conn, institute, notify_admin=alerts.append)
        context.bot_data["reschedule_institute_jobs"](institute_id)
        for text in alerts:
            await query.message.reply_text(text)
        await query.message.reply_text(f"Каталог института «{institute.name}» пересканирован.")
        return


async def pending_action_followup(update, context) -> None:
    pending = context.user_data.get("pending_menu_action")
    if pending is None:
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
            if not value.isdigit() or int(value) <= 0:
                await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
                return
            storage.update_institute_check_interval(conn, institute.id, int(value))
            for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
                context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
            await update.message.reply_text(f"Интервал проверки института «{institute.name}» обновлён: {value} мин.")
        elif action == "set_institute_catalog_interval":
            if not value.isdigit() or int(value) <= 0:
                await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
                return
            storage.update_institute_catalog_refresh_interval(conn, institute.id, int(value))
            context.bot_data["reschedule_institute_jobs"](institute.id)
            await update.message.reply_text(
                f"Интервал сканирования каталога института «{institute.name}» обновлён: {value} мин."
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
    }
    if section in group_scoped_actions:
        await _handle_group_scoped_pick(update, context, conn, chat_id, admin_chat_id, section, int(action))
        return

    institute_scoped_actions = {
        "pickinstitute", "setinstitutecatalogurl", "setinstitutesendtime",
        "setinstitutecheckinterval", "setinstitutecataloginterval", "refreshinstitutelinks",
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
        await query.edit_message_text(
            admin_picker_prompts[section],
            reply_markup=_build_admin_group_picker(conn, pick_action, page=int(action)),
        )
        return

    if section == "root":
        await query.edit_message_text("Главное меню:", reply_markup=build_root_keyboard(conn, chat_id, admin_chat_id))
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
        context.user_data["pending_menu_action"] = {"action": "report"}
        await query.message.reply_text("Отправьте текст сообщения администратору:")
        return
    if section == "faq":
        await faq_command(update, context)
        return
