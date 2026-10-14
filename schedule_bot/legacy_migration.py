import asyncio
import logging
from datetime import datetime, timezone

from schedule_bot import storage
from schedule_bot.group_commands import validate_and_sync_group_on_first_subscribe
from schedule_bot.group_links import refresh_institute_catalog
from schedule_bot.sync import catalog_lock_for
from schedule_bot.telegram_utils import pluralize_russian

logger = logging.getLogger(__name__)

_DONE_SETTING_KEY = "legacy_migration_done_at"


async def warn_if_legacy_backup_unconfigured(conn, config, notify_admin) -> None:
    """If LEGACY_MIGRATION_* was never configured, legacy_members_backup
    just sits there invisibly — this surfaces it as a startup reminder
    instead of silently never telling the admin those pre-upgrade
    subscribers are still unmigrated."""
    if config.legacy_migration is not None:
        return
    chat_ids = storage.list_legacy_member_chat_ids_backup(conn)
    if not chat_ids:
        return
    await notify_admin(
        f"В базе осталось {len(chat_ids)} несмигрированных легаси-пользователей "
        "(из версии бота до появления институтов), а переменные LEGACY_MIGRATION_* не настроены. "
        "Настройте их для миграции, либо это ожидаемо и можно игнорировать."
    )


async def run_legacy_migration_if_configured(
    conn, config, notify_admin, sync_institute_link_fn, reschedule_institute_jobs,
    *, catalog_locks: dict | None = None,
) -> None:
    """One-time migration of every existing `members` row onto a single
    target group, gated by three required env vars surfaced as
    `config.legacy_migration`. Runs at most once ever — guarded by the
    `legacy_migration_done_at` settings key, so a later manual
    /change_group by a migrated user is never reverted by a restart that
    still has the env vars set. Any failure along the way (catalog
    unreachable, target group missing, PDF validation failing) leaves
    the flag unset so the next restart retries from scratch."""
    legacy_migration = config.legacy_migration
    if legacy_migration is None:
        return
    if storage.get_setting(conn, _DONE_SETTING_KEY) is not None:
        return

    catalog_url, institute_name, group_name = legacy_migration

    institute = storage.get_institute_by_catalog_url(conn, catalog_url)
    if institute is None:
        institute_id = storage.create_institute(
            conn, name=institute_name, catalog_url=catalog_url,
            check_interval_minutes=config.default_check_interval_minutes,
            send_time=config.default_daily_send_time, tz_name=config.default_timezone,
            catalog_refresh_interval_minutes=config.default_catalog_refresh_interval_minutes,
        )
        institute = storage.get_institute(conn, institute_id)

    alerts: list[str] = []
    lock = catalog_lock_for(catalog_locks, institute.id) if catalog_locks is not None else None
    try:
        if lock is None:
            await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
        else:
            async with lock:
                await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
    except Exception as error:
        logger.warning("legacy_migration: catalog scan failed for institute_id=%s: %s", institute.id, error)
        await notify_admin(
            f"Миграция легаси-пользователей отложена: каталог института «{institute.name}» недоступен: {error}"
        )
        return
    for text in alerts:
        await notify_admin(text)

    reschedule_institute_jobs(institute.id)

    group = storage.get_group_by_name(conn, institute.id, group_name)
    if group is None:
        logger.warning(
            "legacy_migration: group %r not found in institute_id=%s after catalog scan", group_name, institute.id
        )
        await notify_admin(
            f"Миграция легаси-пользователей отложена: группа «{group_name}» не найдена в каталоге института «{institute.name}»."
        )
        return

    ok = await validate_and_sync_group_on_first_subscribe(
        conn, group, config.pdf_cache_dir, notify_admin, sync_institute_link_fn,
    )
    if not ok:
        # validate_and_sync_group_on_first_subscribe short-circuits
        # silently (no network call, no alert of its own) once
        # is_verified is already -1 from an earlier failed attempt — so
        # on every later restart this is the ONLY alert the admin sees.
        # Re-check the group's current state to say why, instead of
        # repeating a bare generic message forever.
        group_after = storage.get_group_by_name(conn, institute.id, group_name)
        if group_after is not None and group_after.is_verified == -1:
            logger.warning(
                "legacy_migration: group %r already has is_verified=-1 from a previous failed validation, migration postponed",
                group_name,
            )
            await notify_admin(
                f"Миграция легаси-пользователей отложена: группа «{group_name}» ранее не подтвердилась по PDF "
                f"(is_verified=-1) — проверьте название группы или PDF института вручную."
            )
        else:
            logger.warning("legacy_migration: group %r failed PDF validation, migration postponed", group_name)
            await notify_admin(
                f"Миграция легаси-пользователей отложена: группа «{group_name}» не подтвердилась по PDF."
            )
        return

    # Only genuinely still-unmigrated legacy users move: a chat_id that
    # re-subscribed via /start after the destructive schema rebuild (its
    # own fresh `members` row already exists) must never be silently
    # reassigned onto the legacy target group, overwriting their own
    # deliberate choice. `upsert_member` (not `reassign_member_group`) is
    # required because these chat_ids have no `members` row at all —
    # reassign_member_group's UPDATE would silently affect zero rows.
    backed_up = {row.chat_id: row for row in storage.list_legacy_members_backup(conn)}
    already_subscribed_chat_ids = set(storage.list_all_member_chat_ids(conn))
    chat_ids = set(backed_up) - already_subscribed_chat_ids
    now = datetime.now(timezone.utc).isoformat()
    for chat_id in chat_ids:
        backup_row = backed_up[chat_id]
        storage.upsert_member(
            conn, chat_id, group.id, joined_at=backup_row.joined_at or now, subscribed=backup_row.subscribed,
        )
    # Prune every backed-up chat_id, not just the ones just migrated:
    # one already re-subscribed via /start (excluded from chat_ids above)
    # is just as resolved — it already has a real `members` row — and
    # leaving it in the backup would make a later
    # warn_if_legacy_backup_unconfigured alert about it forever.
    storage.prune_legacy_members_backup(conn, list(backed_up))

    # validate_and_sync_group_on_first_subscribe's own immediate sync ran
    # BEFORE any member was reassigned onto this group, so it was a
    # guaranteed no-op (sync_institute_link skips links with no members —
    # see CLAUDE.md's documented members-before-sync ordering). Now that
    # members exist, trigger the real first sync.
    await sync_institute_link_fn(group.institute_link_id)

    storage.set_setting(conn, _DONE_SETTING_KEY, datetime.now(timezone.utc).isoformat())
    noun = pluralize_russian(len(chat_ids), "участник", "участника", "участников")
    await notify_admin(
        f"Миграция легаси-пользователей выполнена: {len(chat_ids)} {noun} переключено в группу "
        f"«{group.name}» института «{institute.name}»."
    )
