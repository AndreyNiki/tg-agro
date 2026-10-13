# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot that parses a university's class schedules from PDFs, tracks
the "Верхняя/Нижняя" (Upper/Lower) academic week type via a formula scraped
from the institute's website, sends a daily schedule message with a cropped
screenshot (header + time column + the group's own column), checks
periodically for schedule changes and auto-broadcasts them, and exposes
`/schedule` (day-picker) plus admin commands for runtime configuration.

The top-level entity is an **institute**: it owns one catalog page URL, a
check interval, a send time, a timezone and a catalog-rescan interval.
Scanning that catalog (never downloading the PDFs) produces
`institute_links` rows (one per PDF url) and, from each link's label,
`groups` rows. Users never create groups or supply PDF urls.

Full design rationale (base system): `docs/superpowers/specs/2026-09-16-schedule-bot-design.md`.
Current data model + sync/screenshot design (binding, supersedes the above
where they differ): `docs/superpowers/specs/2026-10-09-institutes-and-shared-schedules-design.md`.
Task-by-task implementation plans (TDD): `docs/superpowers/plans/2026-09-16-schedule-bot-plan.md`,
`docs/superpowers/plans/2026-10-09-institutes-and-shared-schedules-plan.md`.
User-facing setup/commands: `README.md`.

## Commands

```bash
# Setup (once)
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Run the full test suite
./.venv/bin/python -m pytest -v

# Run a single test file / single test
./.venv/bin/python -m pytest tests/test_pdf_parser_build_schedule.py -v
./.venv/bin/python -m pytest tests/test_sync.py::test_sync_institute_link_notifies_subscribers_not_just_members -v

# Run the bot locally (needs .env populated — see README.md)
set -a; source .env; set +a
./.venv/bin/python main.py

# Docker
docker compose up --build -d
docker compose logs -f
```

There is no separate lint/build step — this is a plain Python package, no
compiled artifacts, no linter configured.

## Architecture

**Data flow for a sync cycle** (this is the thing that requires reading
several files to understand — everything else is a thin, single-purpose
module). The unit of sync work is an `institute_link`, NOT a group:

1. `schedule_bot/sync.py`'s `sync_institute_link(conn, institute_link,
   institute, pdf_cache_dir, notify, *, catalog_rescan_state,
   notify_admin=None)` returns immediately — with zero network traffic —
   if the link `is_stale`, or if none of its groups has a `members` row
   (`list_group_members(..., subscribed_only=False)`: a pinned but
   unsubscribed member still needs `/schedule` to work).
2. Otherwise one `asyncio.to_thread` call does the whole blocking part:
   fetch the bytes once (retried), write them to a temp file, and call
   `pdf_parser.parse_schedules_for_groups(tmp_path, names)`, which runs
   `extract_raw_rows` EXACTLY ONCE and then
   `find_header_column_index` + `build_schedule` per group name. That is
   the real dedup of a shared PDF (fetch *and* parse), and the temp-file
   copy is what avoids a TOCTOU bug: parsing and caching must never
   re-fetch the remote file independently, or a file that changed in
   between would leave the cached PDF's crop coordinates pointing at the
   wrong content. A name whose column isn't found gets `None`, not an
   exception.
3. If the fetch failed entirely (404 / site down), the fallback is one
   catalog rescan for the institute, rate-limited through the
   caller-owned `catalog_rescan_state: dict[int, float]` (institute_id ->
   `time.monotonic()` of the last rescan; allowed again only after
   `institute.check_interval_minutes`). Per-link independent scheduling
   means there is no well-defined "cycle boundary" at which a boolean
   flag could be cleared — hence a self-resetting timestamp. There must
   be exactly ONE such dict per process: `main.py` creates it and passes
   the same object into `build_scheduler` AND into the manual
   `/refresh` closure. Alerts raised by that rescan (spec 1.3's "link
   vanished from the catalog") are collected and forwarded through
   `notify_admin` — never dropped. After the rescan the link's `pdf_url`
   is re-read FROM THE DB (the two-phase upsert may have moved it) and
   the fetch retried once.
4. Per group of the link: `compute_diff` against that group's own active
   `schedule_versions` row. `compute_diff(None, ...)` always returns `[]`
   — this is what distinguishes the very first parse ("initial", never
   notifies) from a genuine "no_changes". On change/initial the bytes are
   written to `pdf_cache_dir/link_<link_id>/<safe_group_name>/<ts>.pdf`
   (deliberately a copy per group — simpler than refcounting a shared
   file), the new row is inserted as `active`, the old one flipped to
   `superseded`, and the previous version's cache file deleted — the only
   cleanup mechanism for `pdf_cache_dir`, so don't remove it without
   adding another.
5. `last_sync_failed_at` granularity is deliberate: a failed fetch sets
   it on EVERY active group of the link (shared cause); a successful
   fetch where only one group's column was missing sets it on that group
   only, and clears it for the others.
6. Callers: `schedule_bot/scheduler.py`'s per-link `link_check_job` /
   `link_daily_send_job`, and `bot.py`'s `refresh_command` via the
   injected `sync_institute_link_fn(institute_link_id)` closure built in
   `main.py`. All of them resolve a group to `group.institute_link_id`
   and take the SAME `asyncio.Lock` out of one `link_locks` dict keyed by
   `institute_link_id`, so one link is never synced concurrently from two
   sources while different links of one institute stay independent.

**Catalog scanning and validate-on-subscribe.**
`group_links.refresh_institute_catalog` scans `institute.catalog_url` TWO
LEVELS deep (blocking throughout — every caller must wrap it in
`asyncio.to_thread`): it first fetches the catalog page and calls
`list_form_of_study_links` to find its up-to-three "форма обучения"
(Очная/Очно-заочная/Заочная) subpage links — not every institute has all
three — then fetches each reachable one and lists its `<a href="*.pdf">`
entries via `list_pdf_links`, combining all of them into one flat list
exactly as if they'd come from a single page. From there it expands group
names from each label ("Б-А-301,302,303" -> three names), filters
obvious noise with `looks_like_group_name`, and upserts links two-phase:
by `(institute_id, pdf_url)` first, then by an exact expanded-name-set
match against links not yet confirmed in this scan (that's a file that
changed address). Links not confirmed get `is_stale = 1` (never deleted)
with one admin alert if they had subscribers — but ONLY on a scan where
every form-of-study page was reachable; if one or more form pages 404'd
or timed out this cycle, stale-marking is skipped entirely for the whole
scan, because a transient fetch failure of one form must never be read
as "that form's links vanished from the catalog" (that would silently
stop their sync for up to a week, the default catalog-rescan interval).
A scan with 0 form-of-study links, or 0 PDF links across every reachable
form, must not touch the DB at all — only log + alert. The scan NEVER
downloads a PDF, so a group it created is `is_verified = 0` ("unproven").
The first user who picks that group triggers
`group_commands.validate_and_sync_group_on_first_subscribe`,
which refuses stale links without any network call, otherwise downloads
the PDF once and validates every sibling group of the link
(`is_verified = 1` / `-1`, `-1` = hidden from every picker + admin
alert), then performs the immediate first sync. Validation MUST use
`find_header_column_index` — the same search as the sync pipeline — or a
group whose name only appears in lesson body text would pass validation
and then fail every later sync forever. The `members` row is written
BEFORE that validation/sync call (and rolled back on failure), because
`sync_institute_link` skips links without members: the other order made
the first subscriber's immediate sync a guaranteed no-op.
`schedule_bot/legacy_migration.py`'s `run_legacy_migration_if_configured`
(wired from `main.py`'s `_post_init`, gated by three required
`LEGACY_MIGRATION_*` env vars + a one-time `settings` flag) reuses this
exact same ordering requirement — the members-reassignment loop runs
BEFORE its own call to `sync_institute_link_fn`, for the identical
reason.

**`pdf_parser.py` is deliberately split into two layers** — this split is
the reason the trickiest part of the system (interpreting a hand-typeset,
irregularly-laid-out PDF table) is unit-testable at all:
- `extract_raw_rows`/`find_group_column_index` — thin `pdfplumber` wrapper,
  only tested against the real fixture PDF (`tests/fixtures/schedule_sample.pdf`).
  `find_group_column_index` scans the whole table and is only the
  primitive; everything that needs a group's column uses
  `find_header_column_index`, which restricts the search to the header
  zone (`rows[:_header_zone_end_index(rows)]`). That boundary is the
  first row with a real time-range label (`_TIME_LABEL_RE`), NOT the
  first row with `day_marker is False`: `day_marker` is just `cells[0] is
  not None`, which is also true for the row where each day's label first
  appears — in the real fixture that row IS Monday's first lesson row, so
  the old boundary let lesson text ("совм. с Б-А-301") be matched as a
  header.
- `build_schedule` — a **pure function** (no I/O) that interprets the raw
  rows: forward-fills day/time labels, splits a cell into upper/lower-week
  variants when a slot has two stacked sub-rows (first = Верхняя, second =
  Нижняя — this convention is nowhere in the PDF itself, it's an external
  fact confirmed by the person who commissioned this), and detects a
  lesson shared across two groups' columns purely geometrically: a cell in
  an *earlier* group's column counts as merged/shared only if its right
  edge reaches the *table's overall* right edge (not just its own column's
  edge). All of this is tested with small hand-built `RawRow` fixtures, not
  real PDFs — if you need to add a new edge case, add a synthetic test
  here, not a new real PDF.

**Week-type formula is not scraped from rendered output** — the
institute's page computes it with a tiny client-side `<script>` (an
opaque-looking `new Date(year, 0, 5)` + parity check). `weektype.py` regex-
extracts the reference date and parity rule from the *script source*, then
replicates the same arithmetic in Python (`WeekFormula.compute`). This
formula, once fetched successfully, is persisted into the `settings` table
(`weekformula_base_month`/`base_day`/`even_upper`) precisely so that a
later failure to reach the site (network down, site redesign) doesn't
prevent the bot from starting — `main.py`'s `_fetch_week_formula_or_fallback`
tries live fetch → falls back to the persisted value → falls back to a
hardcoded default, in that order, and never raises.

**`bot_data` is the dependency-injection seam.** Every handler reads its
dependencies (`conn`, `config`, `admin_chat_id`, `week_formula`,
`active_schedule_getter(group_id)`, `active_pdf_path_getter(group_id)`,
`sync_institute_link_fn(institute_link_id)`,
`reschedule_institute_link_jobs(institute_link_id)`,
`reschedule_institute_jobs(institute_id)`) off `context.bot_data`, never
from module-level state. `main.py`'s
`build_application` is the only place that populates it. If you add a
handler that needs a new dependency, add the key there — and if you're
changing what a getter returns, check both `bot.py` (the consumer) and
`main.py` (the producer) agree on the exact shape, since nothing else
enforces that contract at runtime. `bot_data["week_formula"]` is a
`_LiveWeekFormula` proxy object (not a plain `WeekFormula`) specifically so
that the weekly refresh job's in-place mutation of the shared
`WeekFormulaHolder` is visible without needing to re-populate `bot_data`.

**Groups have no settings of their own.** `groups` rows carry only
`institute_id`, `institute_link_id`, `name`, `is_verified`,
`starosta_chat_id`, `last_starosta_change_at`, `is_closed`,
`last_sync_failed_at`, `created_at` — uniqueness is `UNIQUE(institute_id,
name)`, so `storage.get_group_by_name(conn, institute_id, name)` takes
the institute. There is no `pdf_source_url`/`send_time`/
`check_interval_minutes`/`timezone` on a group and no
`/set_pdf_source`/`/set_send_time`/`/set_check_interval` command: those
values are resolved through the institute (`/set_institute_*`,
`institute_commands.py`, and the "🏫 Институты" menu branch). There is
also no user-facing group-creation dialog any more — `group_commands.py`
is the `/start` institute->group picker plus the validate-on-subscribe
logic described above. Scheduler jobs are per-LINK
(`link_check_<id>`, `link_daily_send_<id>`) plus one
`institute_catalog_refresh_<institute_id>` per institute; after any
catalog rescan (manual command, menu button, or the background job) the
newly discovered links must be given their jobs via
`reschedule_institute_jobs` / `make_links_rescheduler`, or they stay
unsynced until a restart.

**Blocking I/O never runs on the event loop.** `concurrent_updates(True)`
means one blocking call stalls every other user's update. Both
`group_links.fetch_source_page_html` (~15s timeout, reached via
`refresh_institute_catalog`) and PDF download+parse are blocking, so
every call site wraps them in `asyncio.to_thread` — `sync.py`,
`scheduler.py`, `institute_commands.py` (`/add_institute`,
`/refresh_institute_links`), `menu.py` (rescan button) and
`group_commands.py` (subscribe-time validation). Because of this the
production sqlite connection is opened with `check_same_thread=False`
(see `main.py`); tests that exercise a to_thread path must do the same.

**Every per-subscriber send loop must isolate failures.** `sync.py`'s
notify loop, `scheduler.py`'s `link_daily_send_job`, and `bot.py`'s
`alert_command` all wrap each individual `send_message`/`send_photo` call
in try/except and continue to the next subscriber — one blocked/invalid
chat_id must never prevent the rest of the subscribers from being
notified. If you add a new broadcast loop, match this pattern.

## Testing conventions

- `tests/fixtures/schedule_sample.pdf` is the real, currently-published
  schedule PDF — every expected value in the `pdf_parser` integration
  tests was derived by manually tracing this exact file's geometry, not
  invented. Treat a failing assertion there as a code bug first, not a bad
  fixture.
- `build_schedule`'s tests use small, hand-built `RawRow`/`Cell` objects
  with deliberately *constant* cell-list lengths per row within a table —
  this matches a real `pdfplumber` invariant (row length always equals
  column count). If you write a new synthetic test with inconsistent row
  lengths across a single time slot, you're testing something that cannot
  happen in production.
- Any test that calls `scheduler.build_scheduler` (directly or via
  `main.build_application`) starts a real `AsyncIOScheduler`. Shut it down
  in the test (or via a fixture) — see `tests/test_main_wiring.py`'s
  autouse fixture for the pattern. In an `async` test, shut the scheduler
  down INSIDE the test: the AsyncIOScheduler is bound to the
  pytest-asyncio event loop, which is already closed by the time a
  sync fixture's teardown runs (`RuntimeError: Event loop is closed`).
  Several places in `tests/test_scheduler.py`
  do not yet follow this and leak schedulers (known, low-impact, not yet
  cleaned up).
