# Deferred Findings Backlog

> Source: pre-production review conducted 2026-10-13 (three parallel Opus
> reviews covering concurrency/sync, storage/pdf_parser, and bot
> handlers/menu) plus the hardening work in
> `docs/superpowers/plans/2026-10-13-pre-production-hardening-plan.md`.
> Everything below was explicitly marked **out of scope** for that plan —
> lower severity than its 18 tasks, or large enough to warrant its own
> brainstorm/plan rather than folding into a hardening pass. Nothing here
> is fixed yet.

## 1. Message-length / keyboard-size limits for large groups

**Where:** `schedule_bot/classmates.py` (`/classmates`, exclude-member
keyboard), `schedule_bot/change_group_commands.py`
(`_build_all_requests_messages`, `_build_requests_messages`),
`schedule_bot/institute_commands.py` (`format_institute_links`).

**Problem:** Telegram caps a single message at 4096 characters and an
inline keyboard at a limited number of rows/buttons. A group with 100+
members produces a `/classmates` text and/or an "exclude" keyboard that
exceeds one or both limits — the call fails with `BadRequest: Message is
too long` (or a keyboard-shape error), and currently nothing catches it,
so the whole command dies for every member of that group. Same shape for
an institute with a large catalog in `format_institute_links`, and for
"📋 Все заявки" once pending requests pile up.

**Why deferred:** Needs an actual UX decision (paginate the member list?
chunk the message? cap the keyboard and link out?) — not a mechanical
fix, and affects three independent call sites with potentially different
right answers.

## 2. `curl_cffi.Session` never closed in `browser_client.py`

**Where:** `schedule_bot/browser_client.py` — `fetch_url` creates a new
`curl_cffi.requests.Session` per call and never calls `.close()` or uses
it as a context manager.

**Problem:** Slow file-descriptor/socket leak, bounded only by Python's
GC eventually collecting the `Session` object. Not urgent at current
institute/link counts, but worth fixing before link counts grow a lot.

**Why deferred:** Low severity, mechanical one-line fix, but touches a
function every PDF fetch and catalog scan goes through — better done
with its own test coverage for the "do we leak a Session on the retry
path too" case, not bundled into an unrelated hardening task.

## 3. Docker healthcheck doesn't detect a hung event loop or stopped scheduler

**Where:** `Dockerfile:16-17` — healthcheck only checks that
`/app/data/schedule.db` exists.

**Problem:** If the event loop deadlocks, polling dies, or the
`AsyncIOScheduler` silently stops, the container stays "healthy" (the DB
file still exists) and `restart: unless-stopped` never kicks in.

**Why deferred:** Needs a real liveness signal — e.g. a lightweight
scheduler job that touches a heartbeat row/file every N minutes, checked
by the healthcheck script — which is a small feature, not a one-line
fix, and benefits from being designed together with whatever monitoring
is planned for production (if any).

## 4. No cross-institute/global outbound-message rate limiting

**Where:** `schedule_bot/scheduler.py` (`link_daily_send_job`, one per
institute_link, each independently paced via `SEND_PACING_SECONDS`),
`schedule_bot/telegram_utils.py` (`send_with_retry` — one retry only).

**Problem:** The 2026-10-13 hardening plan's Task 5/9/10 fixed
*correctness* (misfire grace, per-group isolation) but not *aggregate
throughput*: if an institute has several links whose daily-send jobs
fire at the same configured `send_time`, each one paces itself to
Telegram's ~30 msg/s ceiling independently — N links sending
simultaneously can still collectively exceed it, triggering cascading
`RetryAfter`, and `send_with_retry`'s single retry means an unlucky
second rate-limit on the same message is a silently dropped send.

**Why deferred:** Fixing this properly means either a global semaphore/
token-bucket shared across every send call in the process, or adopting
PTB's `AIORateLimiter` — a behavioral change big enough to want its own
review rather than being folded into the hardening pass.

## 5. No scheduling jitter for per-institute link jobs

**Where:** `schedule_bot/scheduler.py` (`schedule_link_jobs`'s
`IntervalTrigger(minutes=interval_minutes)`, no `jitter=`).

**Problem:** Every link of one institute is added to the scheduler
within milliseconds of each other at startup/rescan, so their
`IntervalTrigger`s share (almost) the same `next_run_time` — all of an
institute's links poll the university's site in the same second, every
hour. Looks like a small burst/DoS pattern to the target site rather
than organic traffic.

**Why deferred:** Cosmetic/politeness improvement, not a correctness
bug — zero user-facing impact, safe to batch with other scheduler
tuning later (also interacts with Task 4's rate-limiting work above, so
doing them together may be more efficient than separately).

## 6. Unthrottled global error handler under a failure storm

**Where:** `main.py` — `_global_error_handler` (registered via
`application.add_error_handler`).

**Problem:** Every unhandled exception sends one admin `send_message`
plus one user reply, with no dedup/throttle. Under a systemic failure
(bad migration, DB lock, Telegram outage) affecting many updates at
once, this fires one admin message per failing update, which can itself
start hitting `RetryAfter` and burn through `send_with_retry`'s retry
budget — the safety net becomes part of the storm it's supposed to
report on.

**Why deferred:** Same shape as #4 above (outbound rate limiting) — the
2026-10-13 hardening plan's own "out of scope" list already defers
general outbound rate limiting as needing its own reviewed piece of
work; admin-alert throttling is the same kind of change and is best
designed together with it.

## 7. Dead code left behind by the hardening pass

**Where:** `schedule_bot/storage.py` — `close_election` (superseded by
the atomic `close_election_if_open`, no production caller left);
`schedule_bot/bot.py` — `_is_valid_positive_int` (unreferenced after the
`parse_positive_int` consolidation).

**Problem:** Both still exist and compile, but invite a future call
site to pick the non-atomic/looser variant by accident (e.g. tab-
completion or copy-paste from an old pattern).

**Why deferred:** Mechanical deletion, zero design decision — just
needs its own small commit with a test-suite run to confirm nothing
(including tests-only usage) still depends on them.

## 8. `legacy_members_backup` loses `subscribed`/`joined_at`, and is invisible to the admin

**Where:** `schedule_bot/storage.py` (`_backup_legacy_member_chat_ids`,
`legacy_members_backup` table — chat_id only) and
`schedule_bot/legacy_migration.py` (`run_legacy_migration_if_configured`
— re-creates every migrated member with `subscribed=True`).

**Problem:** A legacy user who had deliberately unsubscribed before the
destructive schema rebuild starts receiving daily sends again once
migrated — their prior `subscribed=False` is not preserved because it
was never backed up. Separately, if the three `LEGACY_MIGRATION_*` env
vars are never set, the backup table sits populated with no admin
visibility and is never pruned after a successful migration elsewhere.

**Why deferred:** Needs a small schema change (back up
`chat_id, subscribed, joined_at` instead of just `chat_id`) plus a
decision about startup-time admin alerting when the backup is
non-empty but migration is unconfigured — a few lines, but a second
pass at a migration path that should be touched deliberately, not
folded into an unrelated fix.

## 9. Edited-message guard test only checks filter `repr()`, not real dispatch

**Where:** `tests/test_main_wiring.py` —
`test_command_handlers_ignore_edited_messages`.

**Problem:** The test asserts `"MESSAGE" in repr(handler.filters)` for
every registered handler, which would pass for any filter whose repr
merely contains that substring — it never actually dispatches a real
`Update(edited_message=...)` through `application.process_update` to
prove `alert_command`/`notify_command`/`report_command` don't re-run on
an edit. The review-focus item for Task 2 of the hardening plan is
therefore verified only indirectly.

**Why deferred:** Strengthening it is a pure test addition (one
`process_update` dispatch test), no production code change — low risk,
but not urgent since the underlying `filters=filters.UpdateType.MESSAGE`
fix is mechanically correct and covered by PTB's own filter semantics.

## 10. `parse_positive_int` rejects negative chat_ids

**Where:** `schedule_bot/telegram_utils.py` — `parse_positive_int`,
consumed by `bot._apply_set_starosta`.

**Problem:** Telegram group/channel chat_ids are negative; today every
chat_id in this codebase is a user's positive chat_id, so this is a
non-issue, but if the bot is ever extended to assign a group/channel as
"starosta" (unlikely, but `_apply_set_starosta` has no guard against
it), `parse_positive_int` would reject a legitimately negative id with
a misleading "должен быть положительным" message.

**Why deferred:** No current code path produces or expects a negative
chat_id here — fixing it now would be speculative. Worth a one-line
note if the bot ever starts addressing group chats directly.

## 11. `menu.py`'s TTL check doesn't guard `datetime.fromisoformat`

**Where:** `schedule_bot/menu.py` — `pending_action_followup`'s
`started_at` TTL check.

**Problem:** `datetime.fromisoformat(started_at)` raises `ValueError` on
a malformed timestamp. Currently unreachable — `_pending_action` is the
sole writer of `started_at` and always produces a valid ISO string —
but it's the one place in this flow that parses state that *looks*
untrusted (it rides through `context.user_data`, a dict handlers write
into directly).

**Why deferred:** Zero known trigger path; a defensive
`try/except ValueError: treat as expired` is cheap but not worth a
standalone commit until something else touches this function.

## 12. Cosmetic: trailing whitespace from the Task 3 mechanical find/replace

**Where:** `schedule_bot/menu.py` and `schedule_bot/group_commands.py`
— roughly a dozen `safe_edit_message_text(query, ` call sites left a
dangling space before the line break from the automated
`edit_message_text(` → `safe_edit_message_text(query, ` substitution.

**Problem:** Purely cosmetic, but it's the fingerprint of a mechanical
edit and will show up as noise in every future diff that touches those
lines.

**Why deferred:** Whitespace-only change; bundle with the next real
edit to those files rather than a dedicated commit.

## 13. One-time false "Расписание изменилось" broadcast on this deploy

**Where:** Effect of `schedule_bot/pdf_parser.py`'s Task 13 day-matching
fix (2026-10-13 hardening pass) — not a bug in the fix itself.

**Problem:** Because the parser's output changes for any institute whose
day labels previously drifted (and, separately, for the one footer-leak
fix applied during final review), the first sync after this deploy will
diff against every stored `schedule_versions` row and notify every
subscriber of every affected group that "Расписание изменилось" — even
though nothing on the university's site actually changed.

**Why deferred:** Expected and harmless, but worth telling the admin
ahead of the next deploy (or deliberately clearing `schedule_versions`
as part of the rollout) rather than fixing in code — there's no code
change that avoids a real diff between the old (buggy) and new
(correct) parsed output.

---

**Next step when picking any of these up:** brainstorm → plan (per
`CLAUDE.md`'s existing workflow — see
`docs/superpowers/plans/2026-10-13-pre-production-hardening-plan.md` for
the format), since most of the above need a design decision this
backlog entry intentionally doesn't make — #7, #10, #11, and #12 are
mechanical enough to just fix directly when someone's next in the
file; #13 is informational only, nothing to implement.
