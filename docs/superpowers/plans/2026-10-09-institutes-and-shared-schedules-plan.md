# Институты, общие PDF-файлы и кроп колонки группы — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить модель "группа как самостоятельная сущность со своим PDF-источником" на модель "институт → каталог PDF-ссылок → группы", с дедупликацией скачивания/парсинга общего PDF между группами и кропом скриншота до колонки времени + колонки нужной группы.

**Architecture:** Новые таблицы `institutes`/`institute_links` встают выше `groups` в иерархии; `groups` перестаёт хранить свои `pdf_source_url`/`send_time`/`check_interval_minutes`/`timezone` — всё выводится через `institute_id`/`institute_link_id`. Фоновое сканирование каталога (`group_links.refresh_institute_catalog`) заполняет `institute_links`/`groups`, не скачивая PDF. Конвейер сверки работает на уровне ссылки (`sync.sync_institute_link`), скачивает и парсит PDF один раз, раздаёт результат всем активным группам этой ссылки. Планировщик заводит job'ы на уровне ссылки (не института), чтобы не накапливать задержку. Геометрия PDF (`pdf_parser.py`) и скриншот (`screenshot.py`) переходят от "вся таблица" к "колонка времени + колонка группы (+ шапка той же ширины)".

**Tech Stack:** Python 3, `python-telegram-bot`, `APScheduler`, `pdfplumber` (парсинг), `PyMuPDF`/`fitz` (кроп скриншота), `sqlite3`, `pytest` + `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-10-09-institutes-and-shared-schedules-design.md`

## Global Constraints

- Схема `groups`/`members`/`schedule_versions` **пересоздаётся, не мигрируется** — фича ещё не в проде, старых данных сохранять не нужно (спец, раздел 1.0). `storage.migrate_legacy_single_group` и таблица `subscribers` — мёртвый код для ещё более старой модели; удаляются целиком этой же фичей, вместе с `Config.legacy_group_source_page_url` и env `LEGACY_GROUP_SOURCE_PAGE_URL`.
- `institutes.id`/`institute_links.id` защищены от удаления живых групп через `ON DELETE RESTRICT` (спец, 1.1).
- Ключ идентичности `institute_links` — двухфазный: `(institute_id, pdf_url)`, затем (если `pdf_url` новый) совпадение множества развёрнутых имён групп (спец, 1.2).
- Устаревшие ссылки (`is_stale=1`) никогда не триггерят сетевые запросы — ни в каталожном скане, ни в PDF-сверке (спец, 1.3).
- `groups.is_verified`: `0` = непроверена, `1` = подтверждена по PDF, `-1` = не найдена в PDF (скрыта из пикера). Валидация происходит **только** по реальному запросу подписки, не на весь каталог (спец, 1.5).
- Дедуплицируется не только скачивание байт PDF, но и его `pdfplumber`-разбор (`extract_raw_rows`) — один вызов на ссылку за цикл сверки, независимо от числа подписанных на неё групп (спец, 3.1).
- Планировщик заводит job'ы **на уровне ссылки**, не института — `check_interval_minutes`/`send_time` у института общие, но исполнение per-link, чтобы институт с десятками ссылок не накапливал задержку (спец, 4.1).
- Шапка скриншота вырезается **тем же** x-диапазоном, что и тело дня — не отдельной геометрией (спец, 5.4).
- Алерт администратору на ошибку рендера изображения — только с broadcast-путей (рассылка), один раз за прогон; интерактивный `/schedule`/день — только лог (спец, 5.7).

## Review Focus

- Каталог института вернул 0 ссылок (битая страница / редизайн сайта) — не должен обнулить существующие привязки `institute_links`/`groups`; должен залогироваться и прислать админу один алерт, а БД не трогается (спец, раздел 2, шаг 5).
- Один `pdf_url` используется двумя подписанными группами — PDF должен быть скачан и распарсен (`extract_raw_rows`) ровно один раз за цикл сверки, не по разу на группу (спец, 3.1/3.2).
- Ссылка с `is_stale=1` не должна порождать ни одного сетевого запроса ни при плановой, ни при ручной сверке (спец, 1.3/3.2).
- Первый пользователь подписывается на группу, чьё развёрнутое имя из подписи ссылки не совпадает ни с одним столбцом реального PDF — группа должна получить `is_verified=-1`, скрыться из пикера, администратор должен получить алерт, а подписка не должна создаться (спец, 1.5).
- День с "общей парой", растянутой до правого края таблицы — ширина шапки на итоговом изображении должна точно совпасть с шириной тела (не более узкая шапка, выставленная по одной лишь целевой группе) (спец, 5.4).

---

## Фаза 1 — Схема данных

### Task 1: Деструктивный rebuild `groups`/`members`/`schedule_versions`, новые таблицы `institutes`/`institute_links`

**Files:**
- Modify: `schedule_bot/storage.py:1-130` (`init_db`, удаление `migrate_legacy_single_group`/`_rebuild_schedule_versions_for_migration`)
- Modify: `schedule_bot/config.py` (удалить `legacy_group_source_page_url`)
- Modify: `main.py:99-106` (удалить вызов `storage.migrate_legacy_single_group`)
- Test: `tests/test_storage.py` (заменить фикстуру `_make_group`, удалить тесты миграции legacy-группы, добавить тесты rebuild'а)
- Test: `tests/test_config.py` (удалить тесты `legacy_group_source_page_url`)

**Interfaces:**
- Produces: `storage.init_db(conn)` создаёт таблицы `institutes(id, name, catalog_url, check_interval_minutes, send_time, timezone, catalog_refresh_interval_minutes, last_catalog_sync_at, created_at)`, `institute_links(id, institute_id, pdf_url, group_names_json, raw_label, is_stale, last_seen_at, created_at)`, пересобранную `groups(id, institute_id, institute_link_id, name, is_verified, starosta_chat_id, last_starosta_change_at, is_closed, last_sync_failed_at, created_at, UNIQUE(institute_id, name))`.
- Consumes: ничего (это основание пирамиды).

- [ ] **Step 1: Написать падающий тест на rebuild при отсутствии `institute_id`**

```python
# tests/test_storage.py
def test_init_db_rebuilds_old_shape_groups_table(tmp_path):
    db_path = tmp_path / "test.db"
    connection = sqlite3.connect(str(db_path))
    connection.executescript(
        """
        CREATE TABLE groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            source_page_url TEXT NOT NULL,
            pdf_source_url TEXT NOT NULL,
            send_time TEXT NOT NULL,
            check_interval_minutes INTEGER NOT NULL,
            timezone TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO groups (name, source_page_url, pdf_source_url, send_time, check_interval_minutes, timezone, created_at)
        VALUES ('old-group', 'u', 'p', '09:00', 60, 'Europe/Moscow', 't');
        """
    )
    connection.commit()

    storage.init_db(connection)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(groups)").fetchall()}
    assert "institute_id" in columns
    assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
```

- [ ] **Step 2: Запустить и убедиться, что падает**

Run: `./.venv/bin/python -m pytest tests/test_storage.py::test_init_db_rebuilds_old_shape_groups_table -v`
Expected: FAIL (`institute_id` column missing — old `init_db` just leaves the old-shape table alone).

- [ ] **Step 3: Переписать `init_db`**

```python
# schedule_bot/storage.py — replace the top of init_db and remove migrate_legacy_single_group entirely

def _needs_institute_schema_rebuild(conn: sqlite3.Connection) -> bool:
    info = conn.execute("PRAGMA table_info(groups)").fetchall()
    if not info:
        return False  # table doesn't exist yet; CREATE TABLE IF NOT EXISTS below handles it
    columns = {row[1] for row in info}
    return "institute_id" not in columns


def _rebuild_groups_members_schedule_versions(conn: sqlite3.Connection) -> None:
    # FK-safe drop order: schedule_versions/members reference groups(id).
    conn.executescript(
        """
        DROP TABLE IF EXISTS schedule_versions;
        DROP TABLE IF EXISTS members;
        DROP TABLE IF EXISTS groups;
        """
    )
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    if _needs_institute_schema_rebuild(conn):
        _rebuild_groups_members_schedule_versions(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS institutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            catalog_url TEXT NOT NULL,
            check_interval_minutes INTEGER NOT NULL,
            send_time TEXT NOT NULL,
            timezone TEXT NOT NULL,
            catalog_refresh_interval_minutes INTEGER NOT NULL,
            last_catalog_sync_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS institute_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            institute_id INTEGER NOT NULL REFERENCES institutes(id) ON DELETE RESTRICT,
            pdf_url TEXT NOT NULL,
            group_names_json TEXT NOT NULL,
            raw_label TEXT NOT NULL,
            is_stale INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(institute_id, pdf_url)
        );

        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            institute_id INTEGER NOT NULL REFERENCES institutes(id) ON DELETE RESTRICT,
            institute_link_id INTEGER NOT NULL REFERENCES institute_links(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            is_verified INTEGER NOT NULL DEFAULT 0,
            starosta_chat_id INTEGER,
            last_starosta_change_at TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            last_sync_failed_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(institute_id, name)
        );

        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER PRIMARY KEY,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            telegram_username TEXT,
            display_name TEXT,
            subscribed INTEGER NOT NULL DEFAULT 1,
            joined_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schedule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            payload_json TEXT NOT NULL,
            pdf_cache_path TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS elections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL REFERENCES groups(id),
            kind TEXT NOT NULL,
            initiated_by_chat_id INTEGER NOT NULL,
            candidate_chat_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS election_approvals (
            election_id INTEGER NOT NULL REFERENCES elections(id),
            chat_id INTEGER NOT NULL,
            approved_at TEXT NOT NULL,
            UNIQUE(election_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS group_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            from_group_id INTEGER REFERENCES groups(id),
            to_group_id INTEGER NOT NULL REFERENCES groups(id),
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS group_exclusions (
            group_id INTEGER NOT NULL REFERENCES groups(id),
            chat_id INTEGER NOT NULL,
            excluded_at TEXT NOT NULL,
            excluded_by_chat_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()
    _migrate_group_change_requests_nullable_from_group(conn)
```

Delete these functions entirely (dead code for a feature not yet in prod, per Global Constraints): `_ensure_column`, `_column_exists`, `migrate_legacy_single_group`, `_rebuild_schedule_versions_for_migration`. Keep `_migrate_group_change_requests_nullable_from_group` (unrelated to this feature, still needed).

- [ ] **Step 4: Запустить тест и убедиться, что проходит**

Run: `./.venv/bin/python -m pytest tests/test_storage.py::test_init_db_rebuilds_old_shape_groups_table -v`
Expected: PASS

- [ ] **Step 5: Удалить `legacy_group_source_page_url` из `Config`**

```python
# schedule_bot/config.py
@dataclass
class Config:
    telegram_token: str
    admin_chat_id: int
    db_path: str
    pdf_cache_dir: str
    default_weektype_url: str
    default_daily_send_time: str
    default_check_interval_minutes: int
    default_timezone: str
    default_catalog_refresh_interval_minutes: int


def load_config(env: Mapping[str, str]) -> Config:
    def require(key: str) -> str:
        value = env.get(key)
        if not value:
            raise ValueError(f"Missing required env var: {key}")
        return value

    return Config(
        telegram_token=require("TELEGRAM_BOT_TOKEN"),
        admin_chat_id=int(require("ADMIN_CHAT_ID")),
        pdf_cache_dir=env.get("PDF_CACHE_DIR", "./data/pdf_cache"),
        db_path=env.get("DB_PATH", "./data/schedule.db"),
        default_weektype_url=env.get(
            "WEEKTYPE_URL", "https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii"
        ),
        default_daily_send_time=env.get("DAILY_SEND_TIME", "09:00"),
        default_check_interval_minutes=int(env.get("CHECK_INTERVAL_MINUTES", "60")),
        default_timezone=env.get("TIMEZONE", "Europe/Moscow"),
        default_catalog_refresh_interval_minutes=int(
            env.get("CATALOG_REFRESH_INTERVAL_MINUTES", "10080")  # 1 week
        ),
    )
```

Remove the two `legacy_group_source_page_url`-related tests from `tests/test_config.py` and add:

```python
def test_load_config_defaults_catalog_refresh_interval_to_one_week():
    config = load_config(_base_env())
    assert config.default_catalog_refresh_interval_minutes == 10080
```

- [ ] **Step 6: Удалить вызов миграции из `main.py`**

```python
# main.py:104-106 — replace
storage.ensure_default_settings(conn, {"weektype_url": config.default_weektype_url})
# (migrate_legacy_single_group call deleted — function no longer exists)
```

- [ ] **Step 7: Обновить фикстуру `_make_group` и удалить legacy-тесты в `tests/test_storage.py`**

Replace every call-site helper in `tests/test_storage.py` with:

```python
def _make_institute(conn, name="ИТ"):
    return storage.create_institute(
        conn, name=name, catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def _make_link(conn, institute_id, pdf_url="https://example.com/a.pdf", group_names=None):
    return storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=pdf_url,
        group_names=group_names or ["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
    )


def _make_group(conn, name="Б-Мв-101", institute_id=None, institute_link_id=None):
    if institute_id is None:
        institute_id = _make_institute(conn)
    if institute_link_id is None:
        institute_link_id = _make_link(conn, institute_id, group_names=[name])
    return storage.create_group(conn, institute_id=institute_id, institute_link_id=institute_link_id, name=name)
```

Delete `_seed_legacy_single_group_db` and every test named `test_migrate_legacy_single_group_*` (lines ~229-363 of the original file) — the function they test no longer exists. Update every remaining `storage.create_group(...)` call-site in the file (`test_create_group_returns_new_group_with_defaults`, `test_create_group_rejects_duplicate_name`, `test_get_group_by_name_returns_none_when_absent`, `test_list_groups_orders_alphabetically_and_paginates`, `test_list_all_groups_returns_every_group_unpaginated`, `test_update_group_pdf_source_url` → delete (function removed), `test_update_group_send_time_and_check_interval` → delete (functions removed), etc.) to use the new fixtures and new `create_group(conn, *, institute_id, institute_link_id, name)` signature. Task 4 below adds the new groups-CRUD tests that replace the deleted ones.

- [ ] **Step 8: Прогнать весь файл**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: Will still show failures for tests referencing not-yet-written `storage.create_institute`/`create_institute_link` — that's expected; Task 2/3/4 below add those. For this task, just confirm `test_init_db_rebuilds_old_shape_groups_table` and any test NOT touching institutes/new group columns passes, and commit with the rest marked xfail-by-construction (they'll be fixed in Tasks 2-4 of this same session before the suite is expected to go green).

- [ ] **Step 9: Commit**

```bash
git add schedule_bot/storage.py schedule_bot/config.py main.py tests/test_storage.py tests/test_config.py
git commit -m "feat: rebuild groups/members/schedule_versions schema for institutes model"
```

---

### Task 2: `storage.py` — CRUD институтов

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `institutes` table from Task 1.
- Produces: `InstituteRow` dataclass; `create_institute`, `get_institute`, `list_institutes`, `update_institute_catalog_url`, `update_institute_check_interval`, `update_institute_send_time`, `update_institute_catalog_refresh_interval`, `set_institute_last_catalog_sync_at`.

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_storage.py
def test_create_institute_returns_row_with_defaults(conn):
    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    institute = storage.get_institute(conn, institute_id)
    assert institute.name == "ИТ"
    assert institute.catalog_url == "https://example.com/catalog"
    assert institute.last_catalog_sync_at is None


def test_create_institute_rejects_duplicate_name(conn):
    storage.create_institute(
        conn, name="ИТ", catalog_url="u", check_interval_minutes=60,
        send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_institute(
            conn, name="ИТ", catalog_url="u2", check_interval_minutes=60,
            send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
        )


def test_list_institutes_orders_alphabetically(conn):
    storage.create_institute(conn, name="Б", catalog_url="u", check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080)
    storage.create_institute(conn, name="А", catalog_url="u", check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080)
    assert [i.name for i in storage.list_institutes(conn)] == ["А", "Б"]


def test_update_institute_fields(conn):
    institute_id = _make_institute(conn)
    storage.update_institute_catalog_url(conn, institute_id, "https://new.example.com")
    storage.update_institute_check_interval(conn, institute_id, 30)
    storage.update_institute_send_time(conn, institute_id, "08:00")
    storage.update_institute_catalog_refresh_interval(conn, institute_id, 1440)
    institute = storage.get_institute(conn, institute_id)
    assert institute.catalog_url == "https://new.example.com"
    assert institute.check_interval_minutes == 30
    assert institute.send_time == "08:00"
    assert institute.catalog_refresh_interval_minutes == 1440


def test_set_institute_last_catalog_sync_at(conn):
    institute_id = _make_institute(conn)
    storage.set_institute_last_catalog_sync_at(conn, institute_id, "2026-10-09T00:00:00+00:00")
    assert storage.get_institute(conn, institute_id).last_catalog_sync_at == "2026-10-09T00:00:00+00:00"


def test_get_institute_returns_none_for_missing_id(conn):
    assert storage.get_institute(conn, 999) is None
```

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -k institute -v`
Expected: FAIL (`AttributeError: module 'schedule_bot.storage' has no attribute 'create_institute'`)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/storage.py

@dataclass
class InstituteRow:
    id: int
    name: str
    catalog_url: str
    check_interval_minutes: int
    send_time: str
    timezone: str
    catalog_refresh_interval_minutes: int
    last_catalog_sync_at: str | None
    created_at: str


_INSTITUTE_COLUMNS = (
    "id, name, catalog_url, check_interval_minutes, send_time, timezone, "
    "catalog_refresh_interval_minutes, last_catalog_sync_at, created_at"
)


def create_institute(
    conn: sqlite3.Connection, *, name: str, catalog_url: str, check_interval_minutes: int,
    send_time: str, tz_name: str, catalog_refresh_interval_minutes: int,
) -> int:
    cursor = conn.execute(
        "INSERT INTO institutes (name, catalog_url, check_interval_minutes, send_time, "
        "timezone, catalog_refresh_interval_minutes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, catalog_url, check_interval_minutes, send_time, tz_name,
         catalog_refresh_interval_minutes, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cursor.lastrowid


def get_institute(conn: sqlite3.Connection, institute_id: int) -> InstituteRow | None:
    row = conn.execute(
        f"SELECT {_INSTITUTE_COLUMNS} FROM institutes WHERE id = ?", (institute_id,)
    ).fetchone()
    return InstituteRow(*row) if row else None


def list_institutes(conn: sqlite3.Connection) -> list[InstituteRow]:
    rows = conn.execute(f"SELECT {_INSTITUTE_COLUMNS} FROM institutes ORDER BY name").fetchall()
    return [InstituteRow(*row) for row in rows]


def update_institute_catalog_url(conn: sqlite3.Connection, institute_id: int, catalog_url: str) -> None:
    conn.execute("UPDATE institutes SET catalog_url = ? WHERE id = ?", (catalog_url, institute_id))
    conn.commit()


def update_institute_check_interval(conn: sqlite3.Connection, institute_id: int, minutes: int) -> None:
    conn.execute("UPDATE institutes SET check_interval_minutes = ? WHERE id = ?", (minutes, institute_id))
    conn.commit()


def update_institute_send_time(conn: sqlite3.Connection, institute_id: int, send_time: str) -> None:
    conn.execute("UPDATE institutes SET send_time = ? WHERE id = ?", (send_time, institute_id))
    conn.commit()


def update_institute_catalog_refresh_interval(conn: sqlite3.Connection, institute_id: int, minutes: int) -> None:
    conn.execute("UPDATE institutes SET catalog_refresh_interval_minutes = ? WHERE id = ?", (minutes, institute_id))
    conn.commit()


def set_institute_last_catalog_sync_at(conn: sqlite3.Connection, institute_id: int, value: str) -> None:
    conn.execute("UPDATE institutes SET last_catalog_sync_at = ? WHERE id = ?", (value, institute_id))
    conn.commit()
```

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -k institute -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: storage CRUD for institutes"
```

---

### Task 3: `storage.py` — CRUD `institute_links` и переходы `is_stale`

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `InstituteRow`/`create_institute` from Task 2.
- Produces: `InstituteLinkRow`; `create_institute_link`, `get_institute_link`, `get_institute_link_by_pdf_url`, `list_institute_links`, `update_institute_link_group_names` (bundles `group_names_json`/`raw_label`/`last_seen_at`/`is_stale=0`), `update_institute_link_pdf_url`, `mark_institute_link_seen`, `mark_stale_links_not_in`.

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_storage.py
def test_create_institute_link_round_trips_group_names(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="https://example.com/a.pdf",
        group_names=["Б-А-301", "Б-А-302"], raw_label="Б-А-301,302", last_seen_at="t",
    )
    link = storage.get_institute_link(conn, link_id)
    assert link.pdf_url == "https://example.com/a.pdf"
    assert json.loads(link.group_names_json) == ["Б-А-301", "Б-А-302"]
    assert link.is_stale is False


def test_create_institute_link_rejects_duplicate_pdf_url_for_same_institute(conn):
    institute_id = _make_institute(conn)
    storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["B"], raw_label="B", last_seen_at="t")


def test_get_institute_link_by_pdf_url_scoped_to_institute(conn):
    institute_a = _make_institute(conn, "А")
    institute_b = _make_institute(conn, "Б")
    storage.create_institute_link(conn, institute_id=institute_a, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t")
    assert storage.get_institute_link_by_pdf_url(conn, institute_b, "u") is None
    assert storage.get_institute_link_by_pdf_url(conn, institute_a, "u") is not None


def test_list_institute_links_includes_stale_by_default(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])
    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].is_stale is True
    assert storage.list_institute_links(conn, institute_id, include_stale=False) == []


def test_update_institute_link_group_names_resets_stale_and_bumps_last_seen(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t1")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])
    storage.update_institute_link_group_names(conn, link_id, group_names=["A", "B"], raw_label="A,B", last_seen_at="t2")
    link = storage.get_institute_link(conn, link_id)
    assert json.loads(link.group_names_json) == ["A", "B"]
    assert link.raw_label == "A,B"
    assert link.last_seen_at == "t2"
    assert link.is_stale is False


def test_update_institute_link_pdf_url_keeps_id_stable(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="old.pdf", group_names=["A"], raw_label="A", last_seen_at="t")
    storage.update_institute_link_pdf_url(conn, link_id, "new.pdf")
    link = storage.get_institute_link(conn, link_id)
    assert link.id == link_id
    assert link.pdf_url == "new.pdf"


def test_mark_institute_link_seen_resets_stale_without_touching_names(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t1")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])
    storage.mark_institute_link_seen(conn, link_id, "t2")
    link = storage.get_institute_link(conn, link_id)
    assert link.is_stale is False
    assert link.last_seen_at == "t2"
    assert json.loads(link.group_names_json) == ["A"]


def test_mark_stale_links_not_in_preserves_confirmed_ones(conn):
    institute_id = _make_institute(conn)
    kept = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u1", group_names=["A"], raw_label="A", last_seen_at="t")
    dropped = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u2", group_names=["B"], raw_label="B", last_seen_at="t")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[kept])
    assert storage.get_institute_link(conn, kept).is_stale is False
    assert storage.get_institute_link(conn, dropped).is_stale is True
```

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -k institute_link -v`
Expected: FAIL

- [ ] **Step 3: Реализация**

```python
# schedule_bot/storage.py
import json  # add to top-level imports


@dataclass
class InstituteLinkRow:
    id: int
    institute_id: int
    pdf_url: str
    group_names_json: str
    raw_label: str
    is_stale: bool
    last_seen_at: str
    created_at: str


_INSTITUTE_LINK_COLUMNS = (
    "id, institute_id, pdf_url, group_names_json, raw_label, is_stale, last_seen_at, created_at"
)


def _institute_link_row_from_tuple(row) -> InstituteLinkRow:
    *rest, is_stale, last_seen_at, created_at = row
    return InstituteLinkRow(*rest, bool(is_stale), last_seen_at, created_at)


def create_institute_link(
    conn: sqlite3.Connection, *, institute_id: int, pdf_url: str,
    group_names: list[str], raw_label: str, last_seen_at: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO institute_links (institute_id, pdf_url, group_names_json, raw_label, "
        "last_seen_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (institute_id, pdf_url, json.dumps(group_names), raw_label, last_seen_at,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cursor.lastrowid


def get_institute_link(conn: sqlite3.Connection, link_id: int) -> InstituteLinkRow | None:
    row = conn.execute(
        f"SELECT {_INSTITUTE_LINK_COLUMNS} FROM institute_links WHERE id = ?", (link_id,)
    ).fetchone()
    return _institute_link_row_from_tuple(row) if row else None


def get_institute_link_by_pdf_url(conn: sqlite3.Connection, institute_id: int, pdf_url: str) -> InstituteLinkRow | None:
    row = conn.execute(
        f"SELECT {_INSTITUTE_LINK_COLUMNS} FROM institute_links WHERE institute_id = ? AND pdf_url = ?",
        (institute_id, pdf_url),
    ).fetchone()
    return _institute_link_row_from_tuple(row) if row else None


def list_institute_links(conn: sqlite3.Connection, institute_id: int, *, include_stale: bool = True) -> list[InstituteLinkRow]:
    if include_stale:
        rows = conn.execute(
            f"SELECT {_INSTITUTE_LINK_COLUMNS} FROM institute_links WHERE institute_id = ? ORDER BY id",
            (institute_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_INSTITUTE_LINK_COLUMNS} FROM institute_links WHERE institute_id = ? AND is_stale = 0 ORDER BY id",
            (institute_id,),
        ).fetchall()
    return [_institute_link_row_from_tuple(row) for row in rows]


def update_institute_link_group_names(
    conn: sqlite3.Connection, link_id: int, *, group_names: list[str], raw_label: str, last_seen_at: str
) -> None:
    conn.execute(
        "UPDATE institute_links SET group_names_json = ?, raw_label = ?, last_seen_at = ?, is_stale = 0 WHERE id = ?",
        (json.dumps(group_names), raw_label, last_seen_at, link_id),
    )
    conn.commit()


def update_institute_link_pdf_url(conn: sqlite3.Connection, link_id: int, pdf_url: str) -> None:
    conn.execute("UPDATE institute_links SET pdf_url = ? WHERE id = ?", (pdf_url, link_id))
    conn.commit()


def mark_institute_link_seen(conn: sqlite3.Connection, link_id: int, last_seen_at: str) -> None:
    conn.execute(
        "UPDATE institute_links SET last_seen_at = ?, is_stale = 0 WHERE id = ?", (last_seen_at, link_id)
    )
    conn.commit()


def mark_stale_links_not_in(conn: sqlite3.Connection, institute_id: int, confirmed_link_ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in confirmed_link_ids) or "NULL"
    conn.execute(
        f"UPDATE institute_links SET is_stale = 1 WHERE institute_id = ? AND id NOT IN ({placeholders})",
        (institute_id, *confirmed_link_ids),
    )
    conn.commit()
```

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -k institute_link -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: storage CRUD for institute_links with is_stale lifecycle"
```

---

### Task 4: `storage.py` — новый `groups` CRUD (institute-scoped)

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: Tasks 2-3 (`institutes`/`institute_links`).
- Produces: `GroupRow(id, institute_id, institute_link_id, name, is_verified, starosta_chat_id, last_starosta_change_at, is_closed, last_sync_failed_at, created_at)`; `create_group(conn, *, institute_id, institute_link_id, name) -> int`; `get_group`; `get_group_by_name(conn, institute_id, name)`; `list_groups_for_link(conn, institute_link_id)`; `list_groups(conn, institute_id, *, limit, offset, include_unverified=False)`; `count_groups(conn, institute_id, *, include_unverified=False)`; `update_group_is_verified`; `update_group_institute_link_id`; `set_group_last_sync_failed_at` (unchanged signature); starosta/closed setters (unchanged signatures, just new column list).

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_storage.py — replace all old GroupRow-shape tests with these
def test_create_group_returns_new_group_with_defaults(conn):
    institute_id = _make_institute(conn)
    link_id = _make_link(conn, institute_id)
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Б-Мв-101")
    group = storage.get_group(conn, group_id)
    assert group.name == "Б-Мв-101"
    assert group.institute_id == institute_id
    assert group.institute_link_id == link_id
    assert group.is_verified == 0
    assert group.last_sync_failed_at is None


def test_create_group_rejects_duplicate_name_within_institute(conn):
    institute_id = _make_institute(conn)
    link_id = _make_link(conn, institute_id)
    storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Б-Мв-101")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Б-Мв-101")


def test_create_group_allows_same_name_in_different_institutes(conn):
    institute_a = _make_institute(conn, "А")
    institute_b = _make_institute(conn, "Б")
    link_a = _make_link(conn, institute_a)
    link_b = _make_link(conn, institute_b)
    storage.create_group(conn, institute_id=institute_a, institute_link_id=link_a, name="Б-Мв-101")
    storage.create_group(conn, institute_id=institute_b, institute_link_id=link_b, name="Б-Мв-101")  # must not raise


def test_get_group_by_name_scoped_to_institute(conn):
    institute_a = _make_institute(conn, "А")
    institute_b = _make_institute(conn, "Б")
    link_a = _make_link(conn, institute_a)
    storage.create_group(conn, institute_id=institute_a, institute_link_id=link_a, name="Б-Мв-101")
    assert storage.get_group_by_name(conn, institute_a, "Б-Мв-101") is not None
    assert storage.get_group_by_name(conn, institute_b, "Б-Мв-101") is None


def test_list_groups_for_link_returns_only_that_links_groups(conn):
    institute_id = _make_institute(conn)
    link_a = _make_link(conn, institute_id, pdf_url="a.pdf", group_names=["A"])
    link_b = _make_link(conn, institute_id, pdf_url="b.pdf", group_names=["B"])
    storage.create_group(conn, institute_id=institute_id, institute_link_id=link_a, name="A")
    storage.create_group(conn, institute_id=institute_id, institute_link_id=link_b, name="B")
    assert [g.name for g in storage.list_groups_for_link(conn, link_a)] == ["A"]


def test_list_groups_excludes_unverified_minus_one_by_default(conn):
    institute_id = _make_institute(conn)
    link_id = _make_link(conn, institute_id)
    good = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Good")
    bad = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Bad")
    storage.update_group_is_verified(conn, bad, -1)
    names = [g.name for g in storage.list_groups(conn, institute_id, limit=10, offset=0)]
    assert names == ["Good"]
    assert storage.count_groups(conn, institute_id) == 1


def test_update_group_is_verified(conn):
    institute_id = _make_institute(conn)
    link_id = _make_link(conn, institute_id)
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="A")
    storage.update_group_is_verified(conn, group_id, 1)
    assert storage.get_group(conn, group_id).is_verified == 1


def test_update_group_institute_link_id_reassigns(conn):
    institute_id = _make_institute(conn)
    link_a = _make_link(conn, institute_id, pdf_url="a.pdf", group_names=["A"])
    link_b = _make_link(conn, institute_id, pdf_url="b.pdf", group_names=["A"])
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_a, name="A")
    storage.update_group_institute_link_id(conn, group_id, link_b)
    assert storage.get_group(conn, group_id).institute_link_id == link_b
```

Also keep (unchanged behavior, new fixture) the existing starosta/closed/election/members/exclusion tests from the original file — just route them through `_make_group`/`_make_institute`/`_make_link`.

- [ ] **Step 2: Запустить, убедиться, что падает**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: FAIL on every test using the old `create_group` signature.

- [ ] **Step 3: Реализация**

```python
# schedule_bot/storage.py — replace the GroupRow/create_group/get_group/... block

@dataclass
class GroupRow:
    id: int
    institute_id: int
    institute_link_id: int
    name: str
    is_verified: int
    starosta_chat_id: int | None
    last_starosta_change_at: str | None
    is_closed: bool
    last_sync_failed_at: str | None
    created_at: str


_GROUP_COLUMNS = (
    "id, institute_id, institute_link_id, name, is_verified, starosta_chat_id, "
    "last_starosta_change_at, is_closed, last_sync_failed_at, created_at"
)


def _group_row_from_tuple(row) -> GroupRow:
    (group_id, institute_id, institute_link_id, name, is_verified, starosta_chat_id,
     last_starosta_change_at, is_closed, last_sync_failed_at, created_at) = row
    return GroupRow(
        group_id, institute_id, institute_link_id, name, is_verified, starosta_chat_id,
        last_starosta_change_at, bool(is_closed), last_sync_failed_at, created_at,
    )


def create_group(conn: sqlite3.Connection, *, institute_id: int, institute_link_id: int, name: str) -> int:
    cursor = conn.execute(
        "INSERT INTO groups (institute_id, institute_link_id, name, created_at) VALUES (?, ?, ?, ?)",
        (institute_id, institute_link_id, name, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cursor.lastrowid


def get_group(conn: sqlite3.Connection, group_id: int) -> GroupRow | None:
    row = conn.execute(f"SELECT {_GROUP_COLUMNS} FROM groups WHERE id = ?", (group_id,)).fetchone()
    return _group_row_from_tuple(row) if row else None


def get_group_by_name(conn: sqlite3.Connection, institute_id: int, name: str) -> GroupRow | None:
    row = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE institute_id = ? AND name = ?", (institute_id, name)
    ).fetchone()
    return _group_row_from_tuple(row) if row else None


def list_groups_for_link(conn: sqlite3.Connection, institute_link_id: int) -> list[GroupRow]:
    rows = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE institute_link_id = ? ORDER BY name", (institute_link_id,)
    ).fetchall()
    return [_group_row_from_tuple(row) for row in rows]


def list_groups(
    conn: sqlite3.Connection, institute_id: int, *, limit: int, offset: int, include_unverified: bool = False
) -> list[GroupRow]:
    verified_clause = "" if include_unverified else "AND is_verified != -1"
    rows = conn.execute(
        f"SELECT {_GROUP_COLUMNS} FROM groups WHERE institute_id = ? {verified_clause} "
        "ORDER BY name LIMIT ? OFFSET ?",
        (institute_id, limit, offset),
    ).fetchall()
    return [_group_row_from_tuple(row) for row in rows]


def list_all_groups(conn: sqlite3.Connection) -> list[GroupRow]:
    rows = conn.execute(f"SELECT {_GROUP_COLUMNS} FROM groups ORDER BY name").fetchall()
    return [_group_row_from_tuple(row) for row in rows]


def count_groups(conn: sqlite3.Connection, institute_id: int, *, include_unverified: bool = False) -> int:
    verified_clause = "" if include_unverified else "AND is_verified != -1"
    return conn.execute(
        f"SELECT COUNT(*) FROM groups WHERE institute_id = ? {verified_clause}", (institute_id,)
    ).fetchone()[0]


def update_group_is_verified(conn: sqlite3.Connection, group_id: int, value: int) -> None:
    conn.execute("UPDATE groups SET is_verified = ? WHERE id = ?", (value, group_id))
    conn.commit()


def update_group_institute_link_id(conn: sqlite3.Connection, group_id: int, institute_link_id: int) -> None:
    conn.execute("UPDATE groups SET institute_link_id = ? WHERE id = ?", (institute_link_id, group_id))
    conn.commit()
```

Keep unchanged (just update docstring/behavior is identical, only `_GROUP_COLUMNS` shape changed under them): `set_group_last_sync_failed_at`, `update_group_starosta`, `set_group_last_starosta_change_at`, `update_group_closed`, `get_group_where_starosta`. Delete `update_group_pdf_source_url`, `update_group_send_time`, `update_group_check_interval` (attributes no longer exist on `groups`).

- [ ] **Step 4: Запустить весь файл**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: PASS (this closes out the Phase 1 schema work — every remaining failure at this point belongs to other test files touched in later tasks).

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: institute-scoped groups CRUD, drop group-level pdf/send-time/interval setters"
```

---

## Фаза 2 — Сканирование каталога института

### Task 5: `group_links.py` — `list_pdf_links`

**Files:**
- Modify: `schedule_bot/group_links.py`
- Test: `tests/test_group_links.py`

**Interfaces:**
- Consumes: existing `_PDF_LINK_RE`/`_TAG_RE` module constants.
- Produces: `list_pdf_links(html: str, base_url: str) -> list[tuple[str, str]]` returning `[(pdf_url, raw_label), ...]` for every `<a href="*.pdf">`.

- [ ] **Step 1: Падающий тест**

```python
# tests/test_group_links.py
def test_list_pdf_links_returns_every_pdf_link_with_normalized_label():
    html = """
    <a href="/files/a.pdf">  Б-А-301,302,303  </a>
    <a href="/files/b.pdf">Б-Эв-101, Б-Мв-101</a>
    """
    links = list_pdf_links(html, "https://example.com/catalog")
    assert links == [
        ("https://example.com/files/a.pdf", "Б-А-301,302,303"),
        ("https://example.com/files/b.pdf", "Б-Эв-101, Б-Мв-101"),
    ]


def test_list_pdf_links_collapses_internal_whitespace_but_keeps_case():
    html = '<a href="/x.pdf">Б-А-301,\xa0 302</a>'
    links = list_pdf_links(html, "https://example.com/catalog")
    assert links == [("https://example.com/x.pdf", "Б-А-301, 302")]
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_group_links.py -k list_pdf_links -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/group_links.py
_WHITESPACE_RE = re.compile(r"[\s\xa0]+")


def _normalize_label(raw_label: str) -> str:
    return _WHITESPACE_RE.sub(" ", raw_label).strip()


def list_pdf_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Returns [(pdf_url, raw_label), ...] for every <a href="*.pdf"> on
    html, without filtering by group name. raw_label is whitespace-
    normalized (collapsed, stripped) but NOT casefolded — case must
    survive to match PDF column headers later (see group_commands
    validation)."""
    results = []
    for href, inner_html in _PDF_LINK_RE.findall(html):
        visible_text = _TAG_RE.sub("", inner_html)
        results.append((urljoin(base_url, href), _normalize_label(visible_text)))
    return results
```

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_group_links.py -k list_pdf_links -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_links.py tests/test_group_links.py
git commit -m "feat: list_pdf_links extracts every catalog PDF link with normalized label"
```

---

### Task 6: `group_links.py` — `expand_group_names`

**Files:**
- Modify: `schedule_bot/group_links.py`
- Test: `tests/test_group_links.py`

**Interfaces:**
- Produces: `expand_group_names(raw_label: str) -> list[str]`.

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_group_links.py
def test_expand_group_names_fills_prefix_for_trailing_digit_fragments():
    assert expand_group_names("Б-А-301,302,303") == ["Б-А-301", "Б-А-302", "Б-А-303"]


def test_expand_group_names_keeps_full_names_as_is():
    assert expand_group_names("Б-Эв-101, Б-Мв-101") == ["Б-Эв-101", "Б-Мв-101"]


def test_expand_group_names_mixes_prefix_fill_and_full_names():
    assert expand_group_names("Б-А-301,302, Б-Мв-101") == ["Б-А-301", "Б-А-302", "Б-Мв-101"]


def test_expand_group_names_returns_empty_for_leading_number_with_no_prefix():
    assert expand_group_names("301,302") == []


def test_expand_group_names_ignores_surrounding_whitespace_around_commas():
    assert expand_group_names(" Б-А-301 , 302 ") == ["Б-А-301", "Б-А-302"]
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_group_links.py -k expand_group_names -v`
Expected: FAIL

- [ ] **Step 3: Реализация**

```python
# schedule_bot/group_links.py
import logging

logger = logging.getLogger(__name__)

_TRAILING_DIGITS = re.compile(r"\d+$")


def expand_group_names(raw_label: str) -> list[str]:
    """Splits raw_label on commas (surrounding whitespace ignored). A
    fragment that is purely digits is prefixed with the previous full
    name's non-digit prefix in the same label ("Б-А-301,302,303" ->
    ["Б-А-301", "Б-А-302", "Б-А-303"]). A fragment containing letters is
    a standalone full name. If the very first fragment is purely
    digits (no previous name to borrow a prefix from), the whole label
    is invalid and skipped with a logged warning — this format isn't
    something this rule was calibrated for (see the "Known limitation"
    note in the design spec, section 2)."""
    fragments = [f.strip() for f in raw_label.split(",")]
    names: list[str] = []
    prefix: str | None = None
    for fragment in fragments:
        if not fragment:
            continue
        if fragment.isdigit():
            if prefix is None:
                logger.warning(
                    "group_links: raw_label %r starts with a bare digit fragment, no prefix to borrow — skipping", raw_label
                )
                return []
            names.append(f"{prefix}{fragment}")
        else:
            names.append(fragment)
            prefix = _TRAILING_DIGITS.sub("", fragment)
    return names
```

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_group_links.py -k expand_group_names -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_links.py tests/test_group_links.py
git commit -m "feat: expand_group_names handles comma-separated prefix-digit shorthand"
```

---

### Task 7: `group_links.py` — текстовый фильтр-эвристика + `refresh_institute_catalog`

**Files:**
- Modify: `schedule_bot/group_links.py`
- Modify: `schedule_bot/storage.py` (нет изменений, только используется)
- Test: `tests/test_group_links_catalog.py` (новый файл)

**Interfaces:**
- Consumes: `list_pdf_links`, `expand_group_names` (Tasks 5-6); `storage.{create_institute_link, get_institute_link_by_pdf_url, update_institute_link_group_names, update_institute_link_pdf_url, mark_institute_link_seen, mark_stale_links_not_in, list_institute_links, create_group, get_group_by_name, update_group_institute_link_id}` (Tasks 3-4); `group_links.fetch_source_page_html` (existing).
- Produces: `looks_like_group_name(name: str) -> bool`; `refresh_institute_catalog(conn, institute, *, notify_admin: Callable[[str], None]) -> None`.

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_group_links_catalog.py (new file)
import json
import sqlite3

import pytest

from schedule_bot import storage
from schedule_bot.group_links import looks_like_group_name, refresh_institute_catalog


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_institute(conn):
    return storage.create_institute(
        conn, name="ИТ", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def test_looks_like_group_name_accepts_letter_dash_digits():
    assert looks_like_group_name("Б-А-301") is True
    assert looks_like_group_name("Б-Мв-101") is True


def test_looks_like_group_name_rejects_free_text():
    assert looks_like_group_name("Приказ №12 от 01.09") is False
    assert looks_like_group_name("1 курс БЭ, БМ оз") is False


def test_refresh_institute_catalog_creates_links_and_groups(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    html = '<a href="/a.pdf">Б-А-301,302</a>'
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: html)

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert json.loads(links[0].group_names_json) == ["Б-А-301", "Б-А-302"]
    groups = storage.list_groups_for_link(conn, links[0].id)
    assert {g.name for g in groups} == {"Б-А-301", "Б-А-302"}
    assert storage.get_institute(conn, institute_id).last_catalog_sync_at is not None


def test_refresh_institute_catalog_dedups_same_pdf_url_two_labels_in_one_scan(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    html = '<a href="/a.pdf">Б-А-301</a><a href="/a.pdf">Б-А-302</a>'
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: html)

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert set(json.loads(links[0].group_names_json)) == {"Б-А-301", "Б-А-302"}


def test_refresh_institute_catalog_same_pdf_url_second_scan_updates_names(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: '<a href="/a.pdf">Б-А-301</a>')
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    first_link_id = storage.list_institute_links(conn, institute_id)[0].id

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: '<a href="/a.pdf">Б-А-301,302</a>')
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].id == first_link_id
    assert set(json.loads(links[0].group_names_json)) == {"Б-А-301", "Б-А-302"}


def test_refresh_institute_catalog_new_pdf_url_same_names_is_treated_as_moved_link(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: '<a href="/old.pdf">Б-А-301</a>')
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    first_link_id = storage.list_institute_links(conn, institute_id)[0].id
    group_id = storage.list_groups_for_link(conn, first_link_id)[0].id

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: '<a href="/new.pdf">Б-А-301</a>')
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].id == first_link_id  # same row, pdf_url updated in place
    assert links[0].pdf_url == "https://example.com/new.pdf"
    assert storage.get_group(conn, group_id).institute_link_id == first_link_id  # group binding untouched


def test_refresh_institute_catalog_marks_unconfirmed_links_stale_and_alerts_once(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: '<a href="/a.pdf">Б-А-301</a>')
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    link_id = storage.list_institute_links(conn, institute_id)[0].id
    group_id = storage.list_groups_for_link(conn, link_id)[0].id
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    alerts = []
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: "<html></html>")
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    assert storage.get_institute_link(conn, link_id).is_stale is True
    assert len(alerts) == 1
    assert str(institute_id) in alerts[0] or "пропала из каталога" in alerts[0]


def test_refresh_institute_catalog_zero_links_does_not_touch_existing_data_but_alerts(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: '<a href="/a.pdf">Б-А-301</a>')
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    alerts = []
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: "<html>empty, no pdfs</html>")
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].is_stale is False  # nothing was touched
    assert any("0 ссылок" in alert for alert in alerts)


def test_refresh_institute_catalog_filters_out_non_group_looking_labels(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    html = '<a href="/a.pdf">1 курс БЭ, БМ оз</a>'
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: html)

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    # no valid group-looking fragment in this label -> no groups created,
    # but the link itself IS still recorded with an empty group list, so
    # /institute_links can show it as "0 groups" (spec, section 2).
    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert json.loads(links[0].group_names_json) == []
    assert storage.list_groups_for_link(conn, links[0].id) == []


def test_refresh_institute_catalog_conflict_keeps_existing_binding(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    html = '<a href="/a.pdf">Б-А-301</a><a href="/b.pdf">Б-А-301</a>'
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: html)
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    link_a = storage.get_institute_link_by_pdf_url(conn, institute_id, "https://example.com/a.pdf")
    group = storage.get_group_by_name(conn, institute_id, "Б-А-301")
    assert group.institute_link_id == link_a.id  # first link by page order wins, and is stable on rescans

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    group_after = storage.get_group_by_name(conn, institute_id, "Б-А-301")
    assert group_after.institute_link_id == link_a.id  # unchanged — binding isn't re-decided every scan
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_group_links_catalog.py -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/group_links.py
from datetime import datetime, timezone
from typing import Callable

from schedule_bot import storage

_GROUP_NAME_RE = re.compile(r"^[А-ЯA-Zа-яa-z]+(-[А-ЯA-Zа-яa-z]+)*-\d+$")


def looks_like_group_name(name: str) -> bool:
    """Cheap heuristic filter for the "Letter(s)-Letter(s)-Digits" shape
    ("Б-А-301", "Б-Мв-101") — filters out obvious catalog noise (orders,
    guidelines) at name-expansion time, before a `groups` row is ever
    created. Does NOT replace PDF-based validation (group_commands.py);
    see design spec section 1.5/2."""
    return bool(_GROUP_NAME_RE.match(name.strip()))


def _resolve_link_identity(conn, institute_id: int, pdf_url: str, group_names: list[str]):
    """Two-phase match per design spec section 1.2. Returns the matched
    InstituteLinkRow, or None if this is a brand-new link."""
    existing_by_url = storage.get_institute_link_by_pdf_url(conn, institute_id, pdf_url)
    if existing_by_url is not None:
        return existing_by_url

    candidate_names = set(group_names)
    for link in storage.list_institute_links(conn, institute_id):
        if set(json.loads(link.group_names_json)) == candidate_names:
            return link
    return None


def _upsert_groups_for_link(conn, institute_id: int, link_id: int, group_names: list[str], confirmed_group_ids: set[int]) -> None:
    for name in group_names:
        existing = storage.get_group_by_name(conn, institute_id, name)
        if existing is None:
            group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)
            confirmed_group_ids.add(group_id)
            continue
        confirmed_group_ids.add(existing.id)
        if existing.institute_link_id != link_id:
            # Conflict rule (spec 1.4): only reassign if this group hasn't
            # already been seen under a DIFFERENT link earlier in THIS scan.
            if existing.id not in confirmed_group_ids:
                storage.update_group_institute_link_id(conn, existing.id, link_id)
            else:
                logger.warning(
                    "group_links: group %r appears under two links in one scan; keeping its first binding", name
                )


def refresh_institute_catalog(conn, institute, *, notify_admin: Callable[[str], None]) -> None:
    html = fetch_source_page_html(institute.catalog_url)
    raw_entries = list_pdf_links(html, institute.catalog_url)

    if not raw_entries:
        logger.warning("group_links: catalog for institute %r returned 0 links", institute.name)
        notify_admin(f"Каталог института {institute.name} (id={institute.id}) вернул 0 ссылок")
        return

    # Dedup same pdf_url appearing under two different <a> tags in one scan.
    labels_by_url: dict[str, list[str]] = {}
    for pdf_url, raw_label in raw_entries:
        labels_by_url.setdefault(pdf_url, []).append(raw_label)

    confirmed_link_ids: list[int] = []
    now = datetime.now(timezone.utc).isoformat()
    seen_group_names_this_scan: set[int] = set()

    for pdf_url, raw_labels in labels_by_url.items():
        combined_raw_label = ", ".join(raw_labels)
        expanded = [
            name for raw_label in raw_labels for name in expand_group_names(raw_label) if looks_like_group_name(name)
        ]

        matched = _resolve_link_identity(conn, institute.id, pdf_url, expanded)
        if matched is None:
            link_id = storage.create_institute_link(
                conn, institute_id=institute.id, pdf_url=pdf_url,
                group_names=expanded, raw_label=combined_raw_label, last_seen_at=now,
            )
        elif matched.pdf_url != pdf_url:
            storage.update_institute_link_pdf_url(conn, matched.id, pdf_url)
            storage.update_institute_link_group_names(
                conn, matched.id, group_names=expanded, raw_label=combined_raw_label, last_seen_at=now
            )
            link_id = matched.id
        else:
            storage.update_institute_link_group_names(
                conn, matched.id, group_names=expanded, raw_label=combined_raw_label, last_seen_at=now
            )
            link_id = matched.id

        confirmed_link_ids.append(link_id)
        _upsert_groups_for_link(conn, institute.id, link_id, expanded, seen_group_names_this_scan)

    previously_live_link_ids = {
        link.id for link in storage.list_institute_links(conn, institute.id, include_stale=False)
    }
    newly_stale_ids = previously_live_link_ids - set(confirmed_link_ids)

    storage.mark_stale_links_not_in(conn, institute.id, confirmed_link_ids)
    storage.set_institute_last_catalog_sync_at(conn, institute.id, now)

    for link_id in newly_stale_ids:
        affected_subscriber_count = sum(
            len(storage.list_group_members(conn, group.id, subscribed_only=False))
            for group in storage.list_groups_for_link(conn, link_id)
        )
        if affected_subscriber_count > 0:
            notify_admin(
                f"Ссылка института {institute.name} (id={institute.id}) пропала из каталога, "
                f"подписчиков затронуто: {affected_subscriber_count}"
            )
```

Add `import json` and `import logging` to the top of `group_links.py` if not already present (the `logger` variable from Task 6 already added `logging`). Delete `resolve_group_pdf_source` — no longer used anywhere once Tasks 12/16 land (kept temporarily is fine, but remove its now-dead test cases together with this task since `sync.py`/`group_commands.py` stop importing it in Tasks 12/16 — track removal there, not here, to avoid breaking currently-passing tests mid-task).

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_group_links_catalog.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_links.py tests/test_group_links_catalog.py
git commit -m "feat: refresh_institute_catalog scans a catalog page into institute_links/groups"
```

---

Из-за объёма документа следующие фазы (геометрия PDF/скриншота, конвейер синка, планировщик, админ-команды/меню/main.py-wiring, финальная чистка тестов) продолжаются в отдельном файле.

**Продолжение:** `docs/superpowers/plans/2026-10-09-institutes-and-shared-schedules-plan-part2.md`
