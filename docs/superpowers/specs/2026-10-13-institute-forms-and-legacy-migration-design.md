# Формы обучения в каталоге института и миграция легаси-пользователей

Дата: 2026-10-13

## Контекст и цель

После внедрения модели «институт → institute_links → groups»
(`docs/superpowers/specs/2026-10-09-institutes-and-shared-schedules-design.md`)
выяснились два упущения, которые эта фича ещё не выкатывала в прод, но
нужно закрыть до выкатки:

1. **Каталог института — не прямой список PDF-ссылок.** Страница
   `institute.catalog_url` — это навигационная страница с до трёх
   ссылок по формам обучения («Очная форма обучения», «Очно-заочная
   форма обучения», «Заочная форма обучения»), и только эти подстраницы
   содержат `<a href="*.pdf">`. Какой-то формы может не быть вовсе —
   ссылка просто отсутствует в меню. Пример:
   `https://www.vavilovsar.ru/.../institut-agrobiznesa` →
   `.../institut-agrobiznesa/ochno-zaochnaya-forma-obucheniya` → PDF-ссылки.
   Обратная совместимость с «каталог = прямой список PDF» не нужна —
   фича ещё не в проде.
2. **Нет пути перенести существующих пользователей** (из версии бота
   до институтов, где была одна жёстко заданная группа) в новую модель.
   Нужна одноразовая миграция, включаемая тройкой переменных окружения,
   которая находит/создаёт институт, сканирует его каталог и
   переключает всех текущих участников на конкретную группу.

## 1. Двухуровневое сканирование каталога

### 1.1. `list_form_of_study_links`

Новая функция в `group_links.py`:

```python
def list_form_of_study_links(html: str, base_url: str) -> list[tuple[str, str]]
```

Возвращает `[(url, label), ...]` для каждой ссылки `<a>`, чья
видимая текстовая метка (после снятия тегов и нормализации пробелов —
переиспользуем `_TAG_RE`/`_normalize_label`) содержит подстроку
«форма обучения» (регистронезависимо). На реальных страницах такие
ссылки лежат в блоке `inner-header__menu` вперемешку с другими пунктами
(«Новости» и т.п.), поэтому фильтр именно по тексту метки, а не по
позиции/классу — не зависит от разметки.

### 1.2. Изменение `refresh_institute_catalog`

Текущий поток: fetch `institute.catalog_url` → `list_pdf_links` → если
пусто, лог + один admin-алерт + выход без изменения БД → иначе dedup по
`pdf_url` → two-phase upsert `institute_links`/`groups` → mark stale.

Новый поток вставляет один уровень перед `list_pdf_links`:

1. Fetch `institute.catalog_url` (как сейчас, через
   `fetch_source_page_html` — бросает `SourcePageUnreachableError`,
   это не меняется и пробрасывается вызывающему как сейчас).
2. `form_links = list_form_of_study_links(html, institute.catalog_url)`.
3. Если `form_links` пусто — ведём себя как сейчас при «0 PDF-ссылок»:
   лог warning + один admin-алерт («каталог института X вернул 0
   ссылок на формы обучения»), в БД ничего не трогаем, выходим.
4. Иначе для каждой `(url, label)` из `form_links`:
   - `fetch_source_page_html(url)`, оборачивая в `try/except
     SourcePageUnreachableError` — при ошибке лог warning
     («форма обучения %r недоступна: %s», label, error) и
     **пропускаем именно эту форму**, не прерывая остальные.
   - На успехе — `list_pdf_links(form_html, url)`, результат
     добавляется в общий `raw_entries` (накопительный список, как
     раньше заполнялся одним вызовом `list_pdf_links` на
     `catalog_url`).
5. Если после обхода всех форм `raw_entries` всё равно пусто (все формы
   недоступны или ни одна не содержит PDF-ссылок) — тот же путь «0
   ссылок»: лог + admin-алерт + выход без изменения БД.
6. Дальше — без изменений: dedup по `pdf_url`, two-phase identity
   (`_resolve_link_identity`), `_upsert_groups_for_link`, stale-marking.
   Эта часть пайплайна уже работает на плоском списке `raw_entries` и
   не знает, с одной страницы он собран или с нескольких.

Сигнатура `refresh_institute_catalog(conn, institute, *, notify_admin)`
не меняется — значит `institute_commands.py` (`/add_institute`,
`/refresh_institute_links`) и главный путь миграции (раздел 2) вызывают
её без изменений на своей стороне.

### 1.3. Тестирование

- `list_form_of_study_links`: unit-тесты на синтетический HTML —
  находит 1–3 ссылки по метке, игнорирует ссылки с другими метками
  («Новости»), регистронезависимость, обрезка/нормализация пробелов.
- `refresh_institute_catalog`: новые/изменённые тесты на
  многостраничный сценарий — замокать `fetch_source_page_html` так,
  чтобы первый вызов (каталог) возвращал HTML с 2–3 ссылками форм
  обучения, последующие вызовы — HTML каждой формы с разными PDF-
  ссылками; проверить, что все группы со всех форм попадают в
  `institute_links`/`groups` одного института. Отдельный тест: одна
  форма недоступна (мок бросает `SourcePageUnreachableError`) — скан
  всё равно подтягивает ссылки с оставшихся форм, без admin-алерта по
  самой недоступной форме (только warning в лог — это внутренняя
  устойчивость, не alert-worthy событие per spec 1.3's "link vanished"
  differs from "page unreachable this one scan"). Отдельный тест: 0
  ссылок форм обучения на странице каталога — лог + один алерт, БД не
  тронута (переиспользует существующий тест-паттерн для «0 PDF
  ссылок»).

## 2. Миграция легаси-пользователей

### 2.1. Конфигурация

Три новые переменные окружения, все три обязательны вместе:

```
LEGACY_MIGRATION_INSTITUT=<url каталога института>
LEGACY_MIGRATION_INSTITUT_NAME=<имя института, если его нужно создать>
LEGACY_MIGRATION_GROUP_NAME=<имя группы, на которую переключаем всех>
```

`Config` получает три новых опциональных поля (`str | None`).
`load_config` читает их через `env.get(...)` без `require(...)` —
если хоть одна не задана (`None`/пустая строка), все три
трактуются как «миграция выключена», это проверяется одним хелпером
`Config.legacy_migration` (property или готовый `tuple | None`),
чтобы вызывающему коду (`main.py`) не нужно было повторять логику "все
три или ничего".

### 2.2. Персистентный флаг «уже выполнено»

Новая запись в таблице `settings` (как `weekformula_*`/`weektype_url`):
`legacy_migration_done_at`. Отсутствует → миграция не выполнялась.
Если она есть — миграция пропускается целиком, даже если все три
переменные остаются в `.env`. Это единственный механизм, защищающий
будущие ручные `/change_group` переключения от повторного затирания
миграцией на каждом рестарте бота (решено в брейнсторминге: миграция
выполняется один раз навсегда).

### 2.3. Точка запуска

Внутри `_post_init` в `main.py`, после текущего шага
`_backfill_member_profiles` — это уже `async`-контекст, выполняется
один раз перед началом polling, у него уже есть доступ к `conn`,
`config`, `application.bot`, и к замыканиям
`sync_institute_link_fn`/`reschedule_institute_jobs`, которые
`build_application` создаёт раньше в той же функции.

Новая функция `schedule_bot/legacy_migration.py`:

```python
async def run_legacy_migration_if_configured(
    conn, config, notify_admin, sync_institute_link_fn, reschedule_institute_jobs,
) -> None
```

вызывается из `_post_init` одной строкой. Вынесена в отдельный модуль
(не в `main.py`), чтобы её можно было unit-тестировать без поднятия
целого `Application`.

### 2.4. Алгоритм

Если `config.legacy_migration` is `None` → return немедленно (без
логов — это штатный путь для всех, кто не настраивал миграцию).

Если `storage.get_setting(conn, "legacy_migration_done_at")` не `None`
→ return немедленно.

Иначе:

1. `institute = storage.get_institute_by_catalog_url(conn, catalog_url)`
   (новая функция в `storage.py`, по аналогии с
   `get_institute_link_by_pdf_url`). Если `None` —
   `storage.create_institute(conn, name=institut_name, catalog_url=...,
   check_interval_minutes=config.default_check_interval_minutes,
   send_time=config.default_daily_send_time,
   tz_name=config.default_timezone,
   catalog_refresh_interval_minutes=config.default_catalog_refresh_interval_minutes)`
   — те же дефолты, что берёт `/add_institute`.
2. `await asyncio.to_thread(refresh_institute_catalog, conn, institute,
   notify_admin=...)` — та же функция из раздела 1, блокирующий вызов
   обёрнут как во всех остальных вызывающих местах. Ошибку
   (`SourcePageUnreachableError` и т.п.) ловим, логируем, шлём
   admin-алерт «миграция отложена: каталог института недоступен»,
   **не** ставим флаг «выполнено», return (повтор на следующем
   рестарте).
3. `reschedule_institute_jobs(institute.id)` — институту нужны
   scheduler-джобы, как после обычного `/add_institute`; без этого шага
   свежесозданные `institute_links` останутся несинканными до перезапуска.
4. `group = storage.get_group_by_name(conn, institute.id, group_name)`.
   `None` → лог + admin-алерт «миграция отложена: группа %r не найдена
   в каталоге института %r», флаг не ставим, return.
5. `ok = await validate_and_sync_group_on_first_subscribe(conn, group,
   config.pdf_cache_dir, notify_admin, sync_institute_link_fn)` —
   переиспользуем существующую функцию из `group_commands.py` as-is:
   тот же путь валидации по PDF, что проходит обычный первый
   подписчик. `False` → группа не подтвердилась по PDF или её ссылка
   stale/недоступна → лог + admin-алерт «миграция отложена: группа %r
   не подтвердилась», флаг не ставим, return (повтор на следующем
   рестарте — этот риск принят явно: до первого успешного запуска
   миграции никто не трогается).
6. Успех → `chat_ids = storage.list_all_member_chat_ids(conn)`; для
   каждого — новая `storage.reassign_member_group(conn, chat_id,
   group.id)` (`UPDATE members SET group_id = ? WHERE chat_id = ?` —
   в отличие от `upsert_member` не трогает `subscribed`/`joined_at`,
   и не нужен `ON CONFLICT`, потому что строка уже существует для
   каждого из этих chat_id по построению `list_all_member_chat_ids`).
7. `storage.set_setting(conn, "legacy_migration_done_at",
   datetime.now(timezone.utc).isoformat())`.
8. Один итоговый admin-алерт: «Миграция выполнена: N участников
   переключено в группу «{group.name}» института «{institute.name}»».

Каждый шаг с сетевым/БД побочным эффектом, который может провалиться,
оставляет систему в состоянии, из которого следующий рестарт с теми же
переменными может безопасно повторить попытку — институт/ссылки/группы
уже на месте, `refresh_institute_catalog` идемпотентна (two-phase
upsert), `get_institute_by_catalog_url` не создаст дубликат.

### 2.5. Осознанно не делаем

- Не уведомляем переключённых пользователей сообщением в Telegram —
  не было запрошено, миграция происходит молча.
- Не трогаем `group_change_requests`/`group_exclusions`.
- Не снимаем переменные `LEGACY_MIGRATION_*` из `.env` автоматически —
  это on-disk конфиг, флаг в `settings` — единственный guard.

### 2.6. Тестирование

- `storage.get_institute_by_catalog_url`, `storage.reassign_member_group`:
  прямые unit-тесты (создать/найти, обновление `group_id` без побочных
  эффектов на `subscribed`/`joined_at`).
- `run_legacy_migration_if_configured`: таблица сценариев с моками
  `refresh_institute_catalog`/`validate_and_sync_group_on_first_subscribe`/
  `sync_institute_link_fn`:
  - все три переменные не заданы → no-op, ничего не вызывается.
  - флаг `legacy_migration_done_at` уже стоит → no-op, даже если
    переменные заданы.
  - институт уже существует (найден по `catalog_url`) → не создаётся
    повторно.
  - институт не существует → создаётся с переданными дефолтами.
  - группа не найдена после скана каталога → алерт, флаг не ставится.
  - `validate_and_sync_group_on_first_subscribe` возвращает `False` →
    алерт, флаг не ставится, никто не переключён.
  - успешный путь → все существующие `members` переключены на
    целевую группу, `subscribed`/`joined_at` сохранены, флаг
    выставлен, ровно один финальный admin-алерт с корректным N.
  - второй вызов после успешного первого (флаг уже стоит) → no-op,
    повторных переключений/алертов нет.

## 3. Затронутые файлы

- `schedule_bot/group_links.py` — `list_form_of_study_links`,
  изменение `refresh_institute_catalog`.
- `schedule_bot/storage.py` — `get_institute_by_catalog_url`,
  `reassign_member_group`. `get_setting`/`set_setting` уже существуют
  как обобщённые геттер/сеттер произвольных ключей `settings`
  (используются сейчас для `weekformula_*`/`weektype_url`) —
  `legacy_migration_done_at` переиспользует их без изменений.
- `schedule_bot/legacy_migration.py` — новый модуль,
  `run_legacy_migration_if_configured`.
- `schedule_bot/config.py` — три новых опциональных поля +
  `legacy_migration` helper.
- `main.py` — вызов из `_post_init`.
- `.env.example` — документирует три новые переменные (закомментированы
  по умолчанию, с пояснением «все три обязательны вместе, иначе
  миграция выключена»).
- Тесты: `tests/test_group_links.py` и/или
  `tests/test_group_links_catalog.py` (существующие файлы покрывают
  `list_pdf_links`/`refresh_institute_catalog` соответственно), новый
  `tests/test_legacy_migration.py`, `tests/test_storage.py`.
