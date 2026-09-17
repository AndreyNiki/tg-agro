# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot that parses a university's class schedule from a PDF, tracks
the "Верхняя/Нижняя" (Upper/Lower) academic week type via a formula scraped
from the institute's website, sends a daily schedule message with a cropped
screenshot of the day's row band, checks periodically for schedule changes
and auto-broadcasts them, and exposes `/schedule` (day-picker) plus admin
commands for runtime configuration.

Full design rationale: `docs/superpowers/specs/2026-09-16-schedule-bot-design.md`.
Full task-by-task implementation plan (TDD): `docs/superpowers/plans/2026-09-16-schedule-bot-plan.md`.
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
./.venv/bin/python -m pytest tests/test_sync.py::test_sync_with_changed_pdf_notifies_all_subscribers -v

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
module):

1. `schedule_bot/sync.py`'s `check_and_apply_diff` reads the PDF bytes
   exactly once (`pdf_parser._load_pdf_bytes`), writes them to a temp file,
   and calls `pdf_parser.parse_schedule` on that local copy — never on the
   original URL twice. This exists specifically to avoid a TOCTOU bug: if
   parsing and caching each re-fetched the remote file independently, a
   file that changed between the two fetches would leave the cached PDF's
   day-crop coordinates pointing at the wrong content.
2. The new `Schedule` is diffed against the currently `active` row in
   `schedule_versions` (SQLite) via `schedule_bot/diff.py`. `compute_diff(None, ...)`
   always returns `[]` — this is what distinguishes the very first parse
   ("initial", never notifies) from a genuine "no_changes" result.
3. If applying (initial or changed), the bytes are written to
   `pdf_cache_dir/<group>/<timestamp>.pdf`, the new row is inserted as
   `active` (the old row is flipped to `superseded`), and the *previous*
   version's cache file is deleted from disk — this is the only cleanup
   mechanism for `pdf_cache_dir`, so don't remove it without adding another.
4. `schedule_bot/scheduler.py` calls this from two independent APScheduler
   jobs (hourly check, daily send) and `schedule_bot/bot.py`'s
   `refresh_command` calls it via an injected `sync_fn` closure built in
   `main.py` — all three paths must resolve `pdf_source`/`group_name`
   through `scheduler.resolve_sync_settings(conn, config)`, not by reading
   `storage.get_setting` directly, or you'll reintroduce the exact
   fallback-logic duplication bug (I8) that was fixed post-review.

**`pdf_parser.py` is deliberately split into two layers** — this split is
the reason the trickiest part of the system (interpreting a hand-typeset,
irregularly-laid-out PDF table) is unit-testable at all:
- `extract_raw_rows`/`find_group_column_index` — thin `pdfplumber` wrapper,
  only tested against the real fixture PDF (`tests/fixtures/schedule_sample.pdf`).
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

**`bot_data` is the dependency-injection seam.** Every handler in
`bot.py` reads its dependencies (`conn`, `group_name`, `admin_chat_id`,
`week_formula`, `active_schedule_getter`, `active_pdf_path_getter`,
`sync_fn`, `reschedule_daily_job`, `reschedule_hourly_job`) off
`context.bot_data`, never from module-level state. `main.py`'s
`build_application` is the only place that populates it. If you add a
handler that needs a new dependency, add the key there — and if you're
changing what a getter returns, check both `bot.py` (the consumer) and
`main.py` (the producer) agree on the exact shape, since nothing else
enforces that contract at runtime. `bot_data["week_formula"]` is a
`_LiveWeekFormula` proxy object (not a plain `WeekFormula`) specifically so
that the weekly refresh job's in-place mutation of the shared
`WeekFormulaHolder` is visible without needing to re-populate `bot_data`.

**Known accepted limitation:** `/set_group` updates the `group_name`
setting in storage immediately, but `bot_data["group_name"]` (read by
`start_command`/`alert_command`) is only refreshed on the next process
restart — `sync_fn`/`active_schedule_getter`/`active_pdf_path_getter` all
correctly re-read storage live, so this only affects new subscriptions and
`/alert` targeting between a `/set_group` call and a restart. This is
intentional: real multi-group support is out of scope for the current
design (see the spec's "Хранение" section), so `group_name` in `bot_data`
was left as a startup snapshot rather than reopening the handler interface
for a scenario that isn't supported yet. Don't "fix" this without first
deciding whether multi-group support is actually being built — a partial
fix here without the corresponding storage/schema work would be worse than
the current documented limitation.

**Every per-subscriber send loop must isolate failures.** `sync.py`'s
notify loop, `scheduler.py`'s `daily_send_job`, and `bot.py`'s
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
  autouse fixture for the pattern; several places in `tests/test_scheduler.py`
  do not yet follow this and leak schedulers (known, low-impact, not yet
  cleaned up).
