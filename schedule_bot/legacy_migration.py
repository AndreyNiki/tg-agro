import asyncio
import logging
from datetime import datetime, timezone

from schedule_bot import storage
from schedule_bot.group_commands import validate_and_sync_group_on_first_subscribe
from schedule_bot.group_links import refresh_institute_catalog

logger = logging.getLogger(__name__)

_DONE_SETTING_KEY = "legacy_migration_done_at"


async def run_legacy_migration_if_configured(
    conn, config, notify_admin, sync_institute_link_fn, reschedule_institute_jobs,
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
    try:
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
        logger.warning("legacy_migration: group %r failed PDF validation, migration postponed", group_name)
        await notify_admin(
            f"Миграция легаси-пользователей отложена: группа «{group_name}» не подтвердилась по PDF."
        )
        return

    chat_ids = storage.list_all_member_chat_ids(conn)
    for chat_id in chat_ids:
        storage.reassign_member_group(conn, chat_id, group.id)

    # validate_and_sync_group_on_first_subscribe's own immediate sync ran
    # BEFORE any member was reassigned onto this group, so it was a
    # guaranteed no-op (sync_institute_link skips links with no members —
    # see CLAUDE.md's documented members-before-sync ordering). Now that
    # members exist, trigger the real first sync.
    await sync_institute_link_fn(group.institute_link_id)

    storage.set_setting(conn, _DONE_SETTING_KEY, datetime.now(timezone.utc).isoformat())
    await notify_admin(
        f"Миграция легаси-пользователей выполнена: {len(chat_ids)} участников переключено в группу "
        f"«{group.name}» института «{institute.name}»."
    )
