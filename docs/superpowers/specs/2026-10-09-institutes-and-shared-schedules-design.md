# Институты, общие PDF-файлы и кроп одной колонки на скриншоте

Дата: 2026-10-09

## Контекст и цель

Сейчас каждая `group` — это самостоятельная сущность с собственными
`source_page_url`/`pdf_source_url`/`send_time`/`check_interval_minutes`/
`timezone`. Любой пользователь может создать новую группу вручную,
указав страницу-источник, и бот сам находит и валидирует колонку с её
названием в PDF (`group_commands.py`). Эта логика ещё не была выкачена
в прод, поэтому переопределяем модель без миграции старых данных.

Цель — ввести верхнеуровневую сущность **институт**:

- У института есть одна ссылка на страницу-каталог, на которой
  перечислены ссылки на PDF-расписания (возможно, несколько групп —
  через запятую — на одну ссылку).
- Бот фонового сканирует эту страницу (периодичность настраивается),
  извлекает все PDF-ссылки и разворачивает имена групп из текста
  ссылки (включая кейс "Б-А-301,302,303").
- Обычный пользователь больше не создаёт группы/ссылки сам — только
  выбирает институт, затем готовую группу.
- Если несколько групп, у которых есть подписчики, в итоге делят один
  и тот же PDF-файл (одна ссылка в каталоге покрывает несколько
  групп), файл скачивается и парсится **один раз**, а не по разу на
  каждую группу.
- Сверка PDF происходит только для групп, у которых есть хотя бы один
  подписчик.
- Скриншот дня теперь показывает не всю таблицу целиком, а только
  колонку времени + колонку нужной группы (и, если у группы в этот
  день "общая пара" с кем-то — колонку(и) тех групп тоже), плюс шапку
  с заголовками этих же колонок сверху.

## 1. Модель данных

### Новая таблица `institutes`

```sql
CREATE TABLE institutes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    catalog_url TEXT NOT NULL,
    check_interval_minutes INTEGER NOT NULL,
    send_time TEXT NOT NULL,
    timezone TEXT NOT NULL,
    catalog_refresh_interval_minutes INTEGER NOT NULL,
    last_catalog_sync_at TEXT,
    created_at TEXT NOT NULL
)
```

`check_interval_minutes`/`send_time`/`timezone` общие для всех
ссылок/групп института (ранее — атрибуты группы). Отдельный параметр
`catalog_refresh_interval_minutes` управляет периодичностью
фонового пересканирования каталога (изначально "раз в неделю",
настраивается администратором).

### Новая таблица `institute_links`

```sql
CREATE TABLE institute_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    institute_id INTEGER NOT NULL REFERENCES institutes(id),
    pdf_url TEXT NOT NULL,
    raw_label TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(institute_id, raw_label)
)
```

`pdf_url` — уже финальный, абсолютный адрес `.pdf`-файла (страница
каталога содержит прямые `<a href="*.pdf">`, см. существующий
`group_links.resolve_pdf_link`, который уже вытаскивает `.pdf`-ссылки
из HTML по тому же паттерну). `raw_label` — исходный видимый текст
ссылки ("Б-А-301,302,303"); используется и для разворачивания имён
групп, и как **стабильный ключ** (`institute_id, raw_label`) для
апдейта `pdf_url` при пересканировании, если сам адрес файла
поменялся, но логический набор групп — тот же.

### Изменения в `groups`

Убираем: `source_page_url`, `pdf_source_url`, `send_time`,
`check_interval_minutes`, `timezone` — эти атрибуты теперь выводятся
через `institute_id`/`institute_link_id`.

Добавляем: `institute_id INTEGER NOT NULL REFERENCES institutes(id)`,
`institute_link_id INTEGER NOT NULL REFERENCES institute_links(id)`,
уникальность меняется с `name UNIQUE` на `UNIQUE(institute_id, name)`.

Остальные колонки (`starosta_chat_id`, `last_starosta_change_at`,
`is_closed`, `last_sync_failed_at`, `created_at`) не меняются.

Группы теперь создаются и обновляются **только** автоматическим
сканированием каталога (upsert по `(institute_id, name)`), никогда —
напрямую пользователем или вручную администратором. Группы, пропавшие
из каталога при пересканировании, не удаляются и не закрываются
автоматически — расхождение только логируется; закрытие — ручное
решение администратора через существующий `is_closed`.

### `members`, `schedule_versions`

Не меняются структурно. `schedule_versions.group_id` остаётся
обязательной ссылкой на конкретную группу (каждая группа хранит свою
активную версию расписания и свой путь кэша отдельно — см. раздел 3 про
то, почему это сознательно не шарится).

## 2. Сканирование каталога института

Переиспользуем существующий `group_links.py` (`_PDF_LINK_RE`,
`_TAG_RE`), добавляя новую функцию, не привязанную к одному имени
группы:

```python
def list_pdf_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Возвращает [(pdf_url, raw_label), ...] для каждой <a href="*.pdf">
    на странице, без фильтрации по конкретной группе."""
```

Новая функция разворачивания имён (правило подтверждено):

```python
def expand_group_names(raw_label: str) -> list[str]:
    """Разбивает `raw_label` по запятым (с опциональными пробелами).
    Если очередной фрагмент — чисто число, он наследует буквенный
    префикс предыдущего полного имени в этой же строке
    ("Б-А-301,302,303" -> ["Б-А-301", "Б-А-302", "Б-А-303"]).
    Иначе фрагмент — самостоятельное полное имя
    ("Б-Эв-101, Б-Мв-101" -> ["Б-Эв-101", "Б-Мв-101"])."""
```

Новая функция `refresh_institute_catalog(conn, institute)`:

1. Фетчит `institute.catalog_url` (как и `fetch_source_page_html`).
2. Для каждого `(pdf_url, raw_label)` из `list_pdf_links(...)`:
   - `expand_group_names(raw_label)` -> список имён групп;
   - upsert строки `institute_links` по ключу `(institute_id, raw_label)`
     (обновляя `pdf_url`/`last_seen_at`, если ссылка уже существовала);
   - upsert строк `groups` по `(institute_id, name)` для каждого
     развёрнутого имени, привязывая `institute_link_id` к этой ссылке.
3. Обновляет `institutes.last_catalog_sync_at`.

Вызывается вручную (`/refresh_institute <id>`) и фоново по
`catalog_refresh_interval_minutes` (раздел 4). Также вызывается как
единственный fallback при 404 на `pdf_url` во время сверки (раздел 3)
— вместо точечного поиска ссылки по имени конкретной группы
(старый `resolve_group_pdf_source`, который может давать ложные
совпадения на строках вида "Б-А-301,302,303" при точечном
substring-поиске "302"). Эта функция и связанные с ней
`resolve_group_pdf_source`/`resolve_pdf_link` для пользовательского
создания групп больше не используются и удаляются вместе с
`group_commands.py` (раздел 4).

## 3. Пайплайн сверки (sync) — дедупликация скачивания

Новая верхнеуровневая функция в `sync.py`:

```python
def sync_institute_link(conn, institute_link, pdf_cache_dir, notify) -> list[SyncResult]:
```

1. `groups = storage.list_groups_for_link(conn, institute_link.id)`;
   `subscribed = [g for g in groups if storage.list_group_members(conn, g.id)]`.
   Если `subscribed` пуст — выход без единого сетевого запроса.
2. PDF скачивается **один раз**: `_load_pdf_bytes(institute_link.pdf_url)`
   с retry (как сейчас, `MAX_SYNC_ATTEMPTS`), и при устойчивой ошибке —
   `refresh_institute_catalog(conn, institute)` целиком, затем повтор с
   обновлённым `institute_link.pdf_url` (перечитанным из БД).
3. Байты пишутся во временный файл один раз.
4. Для каждой `group` из `subscribed` выполняется прежняя, почти не
   изменённая логика: `pdf_parser.parse_schedule(tmp_path, group.name)`
   (поиск колонки всё ещё индивидуален для каждой группы — колонки
   разные даже в одном файле), `compute_diff` против активной версии
   этой группы, при изменении — `_write_cache(pdf_bytes, pdf_cache_dir, f"{institute_link.id}/{group.name}")`
   (отдельная копия на диск на группу, как и сейчас), `insert_new_version`,
   рассылка подписчикам (каждый `send`/`send_photo` в try/except, без
   изменений в паттерне изоляции ошибок).
5. Решение сознательное: PDF скачивается и парсится один раз за цикл
   сверки (то, что требовалось устранить), но физически сохраняется по
   одной копии на диск на группу — это не вводит новый инвариант
   "рефкаунт shared-файла" и не ломает существующий
   удаление-устаревшего-кэша-при-смене-версии код и тесты
   (`test_sync_deletes_superseded_cache_file_on_change` и соседние).

## 4. Планировщик, admin-команды, меню, пользовательский выбор

### Планировщик (`scheduler.py`)

Job'ы переходят с уровня группы на уровень **института**:

- `institute_check_{institute_id}` — `IntervalTrigger(minutes=institute.check_interval_minutes)`,
  итерирует все `institute_links` института, вызывает
  `sync_institute_link` для каждой (сама пропускает ссылки без
  подписчиков).
- `institute_daily_send_{institute_id}` — `CronTrigger` из
  `institute.send_time`/`timezone`, аналогично итерирует ссылки с
  подписчиками и рассылает скриншот дня.
- `institute_catalog_refresh_{institute_id}` — `IntervalTrigger(minutes=institute.catalog_refresh_interval_minutes)`,
  вызывает `refresh_institute_catalog`.

Число job'ов падает с O(групп) до O(институтов); устраняется
параллельная сверка одного и того же файла несколькими job'ами
одновременно.

### Admin-команды (паттерн `_apply_x`/`x_command`/`ADMIN_COMMANDS` не меняется)

- `/add_institute <name> <catalog_url>` — создаёт институт с
  дефолтными `check_interval_minutes`/`send_time`/`catalog_refresh_interval_minutes`
  из конфига, сразу вызывает `refresh_institute_catalog`.
- `/set_institute_catalog_url <id> <url>`
- `/set_institute_check_interval <id> <minutes>`
- `/set_institute_send_time <id> <HH:MM>`
- `/set_institute_catalog_interval <id> <minutes>`
- `/refresh_institute <id>` — ручной пересчёт каталога.

Пункты меню — зеркально текущему паттерну в `menu.py`: институты как
group-scoped picker, но для институтов.

### Пользовательский выбор (замена `group_commands.py`)

Конверсейшн-флоу создания группы удаляется целиком вместе с
`resolve_group_pdf_source`/`resolve_pdf_link`/`_resolve_and_validate_pdf_source`.
`/start` показывает список институтов → после выбора — список готовых
групп этого института (уже наполненных сканированием каталога) →
выбор вызывает `upsert_member`, как и сейчас.

## 5. Скриншот — колонка времени + колонка группы (+ шапка)

### `pdf_parser.py`

`find_group_column_index` уже возвращает `header_idx` — индекс строки
заголовка, где была найдена ячейка с именем группы. Используем эту
строку для геометрии шапки.

Расширяем модель:

```python
@dataclass
class DaySchedule:
    y0: float
    y1: float
    time_col_x0: float
    time_col_x1: float
    group_col_x0: float
    group_col_x1: float
    lessons: list[Lesson]

@dataclass
class Schedule:
    table_x0: float
    table_x1: float
    header_y0: float
    header_y1: float
    header_time_col_x0: float
    header_time_col_x1: float
    header_group_col_x0: float
    header_group_col_x1: float
    days: dict[str, DaySchedule]
```

- `time_col_x0/x1` и `group_col_x0/x1` на `DaySchedule` считаются через
  min/max-агрегацию (`touch_bounds`, тот же приём, что уже используется
  для `y0/y1`) по `x0/x1` **фактически использованной ячейки** в каждой
  строке дня — своей ячейки группы, либо (в кейсе "общей пары") той
  растянутой ячейки слева, что уже находится текущей merge-эвристикой
  (`other.x1 >= table_x1 - epsilon`). Никакого отдельного порога/фолбэка
  не нужно: в обычный день агрегация даёт узкую колонку только нашей
  группы; в день с мержем — агрегация естественно расширяется влево до
  начала общего блока, то есть скриншот автоматически покажет "время +
  все группы с общей парой в этот день".
- `header_*` на `Schedule` считаются один раз из `rows[header_idx]`:
  `header_y0/y1` — `y0/y1` этой строки; `header_group_col_x0/x1` —
  `x0/x1` найденной ячейки с именем группы в этой строке;
  `header_time_col_x0/x1` — `x0/x1` ячейки времени (`cells[1]`) в этой
  же строке.
- Все новые поля сериализуются в `schedule_to_dict`/`schedule_from_dict`
  как дополнительные ключи. Для обратной совместимости со старыми
  закэшированными JSON без этих полей — `schedule_from_dict` подставляет
  `None`, и `render_day_image` в этом случае деградирует на старое
  поведение (кроп по всей ширине таблицы) до первой пересверки.

### `screenshot.py`

`render_day_image`:

1. Если геометрия колонок есть (не `None`) — делает четыре
   `page.get_pixmap(clip=...)`: время-шапка (`header_time_col_x0/x1`,
   `header_y0/y1`), группа-шапка (`header_group_col_x0/x1`,
   `header_y0/y1`), время-тело и группа-тело (те же x-диапазоны дня,
   `day.y0/y1`). Шапочные пиксмапы склеиваются горизонтально через
   `PIL.Image`, тело — аналогично; затем шапка и тело склеиваются
   вертикально в одно финальное изображение. Возвращает PNG-байты.
2. Если геометрии нет (старый кэш) — старое поведение: один кроп по
   `table_x0..table_x1` на диапазон `y0..y1` дня, без шапки.

### Обработка ошибок при сборке скриншота

Любое исключение при построении изображения (кроп/склейка) не должно
блокировать отправку расписания: подписчику уходит **текстовое
сообщение без фото** (расширение уже существующей обработки случая
"уроков нет, `render_day_image` вернул `None`" — теперь try/except
покрывает и непредвиденные исключения), и дополнительно администратору
(`admin_chat_id`) уходит алерт с текстом ошибки, группой и днём — по
аналогии с существующим паттерном изоляции ошибок в per-subscriber
рассылках (`sync.py`, `scheduler.py`).

## Тесты, которые нужно обновить или добавить

- `tests/test_storage.py` — новые тесты на `institutes`/`institute_links`
  CRUD, `UNIQUE(institute_id, name)` на `groups`, `list_groups_for_link`.
- Новый `tests/test_group_links_catalog.py` (или расширение
  `tests/test_group_links.py`) — `list_pdf_links`, `expand_group_names`
  (включая кейсы "Б-А-301,302,303" и "Б-Эв-101, Б-Мв-101"),
  `refresh_institute_catalog` (upsert по `(institute_id, raw_label)`,
  обновление `pdf_url` при неизменном `raw_label`).
- `tests/test_sync.py` — новый `sync_institute_link`: "не скачивает,
  если нет подписчиков ни у одной группы ссылки", "скачивает и парсит
  PDF ровно один раз при нескольких подписанных группах на одной
  ссылке" (мокнуть `_load_pdf_bytes`, проверить `call_count == 1`),
  существующие тесты кэша/удаления адаптировать на новую сигнатуру.
- `tests/test_scheduler.py` — заменить
  `test_build_scheduler_registers_one_job_pair_per_group` на аналог
  "один набор из трёх job'ов на институт", обновить locks-тест.
- `tests/test_pdf_parser_build_schedule.py` — новые тесты на агрегацию
  `time_col_x0/x1`/`group_col_x0/x1` в обычном и merge-кейсе.
- `tests/test_pdf_parser_integration.py` — обновить round-trip тест под
  новые поля JSON, добавить тест на подстановку `None` для старого
  формата.
- `tests/test_screenshot.py` — новый тест на ширину итогового PNG
  (сумма ширин кропов шапка+тело, а не ширина всей таблицы), тест на
  try/except-деградацию при искусственно сломанной геометрии.
- Удалить/переписать тесты, завязанные на `group_commands.py`
  (`tests/test_group_commands.py` и связанные) — флоу создания группы
  пользователем убирается целиком.

## Итог

Институт — новая верхнеуровневая сущность с собственным каталогом
PDF-ссылок, периодичностью сверки и временем отправки. Каталог
сканируется фоново и разворачивает имена групп из текста ссылок.
Группы создаются автоматически и выбираются пользователем в два шага
(институт → группа), без ручного создания. Сверка расписания
скачивает и парсит общий PDF-файл один раз за цикл независимо от
числа подписанных на него групп, и выполняется только если хотя бы у
одной такой группы есть подписчик. Скриншот дня теперь показывает
только колонку времени и колонку нужной группы (расширяясь естественно
до всех групп с общей парой в день мержа) с шапкой заголовков сверху;
ошибки сборки изображения не блокируют отправку текста и уведомляют
администратора.
