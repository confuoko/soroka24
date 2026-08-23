# Phase 1 — Audit старого `services/core`

Документ аудита перед созданием `services/core_v2`. Соответствует ТЗ §29 Phase 1.

**В этой фазе код не менялся.** `services/core` и `services/client` прочитаны только на чтение.

Даты, номера строк и имена соответствуют коммиту `2e6ef1a` (`timezone`).

## Как читать документ

- Раздел 1 — таблица по каждому файлу: что переносим, куда, что меняем, чем рискуем.
- Разделы 2-9 — инвентари по темам, которых требует ТЗ §29 (клиенты, парсеры,
  детект вёрстки, identity, monitoring, sync, outbox, celery, зависимости клиента).
- Раздел 10 — мёртвый код, который не переносим.
- Раздел 11 — **риски переноса R1-R17.** Самая важная часть: это то, что нельзя
  случайно изменить. Читать перед каждой следующей фазой.
- Раздел 12 — намеренные отличия core_v2 от core.

Условные обозначения в колонке «Move?»: **да** — переносим; **да\*** — переносим с
изменением структуры; **нет** — не переносим; **удалить** — существует только ради
monitoring или старого client.

---

## 1. Таблица по файлам

### 1.1 `app/` — точка входа и конфигурация

| Existing file | Responsibility | Move? | New location | Behaviour change | Risk |
|---|---|---|---|---|---|
| `app/__init__.py` (пустой) | маркер пакета | да | `app/__init__.py` | — | — |
| `app/main.py` (23) | FastAPI app, подключение роутеров + admin, `/ping` | да | `app/main.py` | роутер monitoring не подключается | R15: все роуты обязаны остаться обычным `def`, не `async def` |
| `app/config.py` (6.6K) | 20 переменных окружения, читаются на импорте | да\* | `app/config.py` | `MONITORING_*` (4 шт., `:81-93`) удаляются; `DATABASE_URL` указывает на `soroka_core_v2` | конфиг читается на импорте — тесты не могут его переопределить без `reload` |
| `app/celery_app.py` (2.8K) | инстанс Celery, очереди `urgent`/`regular`, beat-расписание | да\* | `app/celery_app.py` | `beat_schedule` (`:34-44`) и импорт `crontab` (`:9`) удаляются целиком; `timezone`/`enable_utc` сохраняются | `enable_utc=True` был выровнен с client'ом; после удаления client'а ограничение снимается, но менять не нужно |
| `app/validators.py` (13.8K) | `validate_uid/url`, `canonical_case_url`, `synthetic_uid`, `is_synthetic_uid`, `host_variants` | да | `app/identity.py` (кандидат) или `app/validators.py` | — | **R-critical**: `canonical_case_url` и `synthetic_uid` — часть identity, менять нельзя (см. R18) |
| `app/timezones.py` (11.8K) | `TZ_BY_REGION`, `TZ_BY_COURT_CODE`, `timezone_for`, `to_utc`, `to_court_local` | да | `app/timezones.py` | — | R3: `timezone_for` намеренно бросает `KeyError`, а не подставляет Москву |
| `app/domain/__init__.py` (3 строки комментария) | заготовка «ports & adapters», кода нет | **нет** | — | удаляется | это ровно тот «архитектурный слой на будущее», который запрещает ТЗ §32 |

### 1.2 `app/models/`

| Existing file | Responsibility | Move? | New location | Behaviour change | Risk |
|---|---|---|---|---|---|
| `app/models/__init__.py` | комментарий | да | — | — | — |
| `app/models/database.py` (59K, 857 строк) | `engine`, `SessionLocal`, `session_scope`, `Base`, 4 enum, 2 association-таблицы, 14 моделей | да\* | `app/database.py` (инфраструктура) + `app/models/*.py` (модели) | удаляются: `Case.monitoring_enabled`, `Instance`, `CaseLink`, `Case.case_link_id`, `Document.document_text`, `Event.document_id` | **R19**: `Case.card_key` (`:288`) — формат `f"{uid}\|{court.code}\|{code}"` обязан остаться байт-в-байт |

### 1.3 `app/repositories/` — переносим почти как есть

Слой уже плоский: `class XRepository`, `__init__(self, session)`, никогда не коммитит.
Generic repository и Unit of Work отсутствуют — и заводить их запрещает ТЗ §32.

| Existing file | Responsibility | Move? | New location | Behaviour change | Risk |
|---|---|---|---|---|---|
| `app/repositories/__init__.py` | фасад, реэкспорт 12 имён | да | тот же | — | — |
| `cases.py` (15.6K, 273) | `_UPDATABLE_FIELDS`, `CaseFieldChange`, 13 методов `CaseRepository` | да\* | тот же | удаляются `set_monitoring:161`, `list_monitored_ids:188` | R1, R18 |
| `courts.py` (13.7K, 225) | `participok_no`, `host_of`, `get_by_code/participok/host/url`, `sync_from_entries` | да | тот же | — | R14: импортирует константы `spb_mir_court` (`:47-52`) отложенным импортом из-за цикла |
| `events.py` (8K) | `EVENT_UID_NAMESPACE`, `event_uid`, `sync_events` | да | тот же | — | **R2/R3**: uid по локальной дате, дедуп дублей |
| `place_history.py` (6K) | namespace, `place_history_uid`, `sync_place_history` | да | тот же | — | R2 |
| `court_sessions.py` (7.8K) | namespace, `court_session_uid`, `sync_court_sessions` | да | тот же | — | **R3/R4**: время входит в identity |
| `documents.py` (6.6K) | namespace, `document_uid(…, occurrence)`, `sync_documents` | да | тот же | — | **R2 (главный)**: порядок строк значим |
| `judges.py` | `get_or_create`, `get_or_create_many` | да | тот же | — | ключ — только `full_name`, БД-констрейнта нет |
| `sides.py` | `_ROLE_TO_TYPE`, `get_or_create[_many]` | да | тот же | — | ключ `(full_name, role)`, **не** `(full_name, type)` |
| `outbox_events.py` (2.8K) | `emit`, `list_since` | да\* | `app/outbox.py` | `list_since` → курсор по `id` вместо `created_at` | см. раздел 8 |
| `search_tasks.py` (4K) | 8 методов жизненного цикла `SearchTask` | да | тот же | — | «залипание» в RUNNING при убитом воркере — известная слабость |
| `proxies.py` (5.7K) | `lease` (`FOR UPDATE SKIP LOCKED`), `list_enabled`, `set_enabled` | да | тот же | — | R16: тесты требуют настоящий PostgreSQL |
| `captcha_solves.py` (6K) | учёт стоимости капчи: `record`, `attach_case`, суммы | да | тот же | — | `cost=NULL` означает «неизвестно», а не ноль |

### 1.4 `app/parsers/` — переносим максимально близко к оригиналу

| Existing file | Responsibility | Move? | New location | Behaviour change | Risk |
|---|---|---|---|---|---|
| `app/parsers/__init__.py` | комментарий «Strategy + Registry» (устарел) | да\* | тот же | комментарий переписывается — Strategy/Registry как паттерны не заводим | — |
| `base.py` (6.6K, 90) | `CaseParser(ABC)` + 70 строк докстринга с контрактом вывода | да\* | `app/parsers/parsed_case.py` | `dict`-контракт из докстринга становится typed `ParsedCase` | **R1 (главный)** |
| `moscow_type_a.py` (31.8K, 455+) | тип A: mos-sud.ru | да | тот же | — | **R5, R6, R7**, `_documents_table` по тексту заголовка |
| `msudrf_shared.py` (16K) | общее для B/C: `clean`, даты, табы, `CARD_FIELDS`, `detect_page_type` | да\* | `app/parsers/msudrf_shared.py` | `detect_page_type` переезжает туда, откуда его вызывает `get_parser` | R4 |
| `msudrf_type_b.py` (23K, 337+) | тип B: `<h2>`-метки, есть `<thead>` | да\* | тот же | удаляется дубль `column_index` (`:242-247`), остаётся импорт из `msudrf_shared:195` | **R8** |
| `msudrf_type_c.py` (18K, 284+) | тип C: `<b>`-метки, нет `<thead>`, транспонированные стороны | да | тот же | — | **R9** |
| `spb_type_d.py` (16.7K, 249+) | тип D: mirsud.spb.ru, только печатная форма | да | тот же | — | **R10**, `sides[].role` может быть `None` |
| `registry.py` | `PARSER_BY_PAGE_TYPE`, `get_parser(page_type)` | да\* | `app/parsers/__init__.py::get_parser(portal, html)` | подпись меняется: выбор по `(portal, html)`, а не по `page_type` клиента; `if`-ветки вместо dict | ТЗ PRIORITY 13 |

### 1.5 `app/courts/` — fetch / navigation

| Existing file | Responsibility | Move? | New location | Behaviour change | Risk |
|---|---|---|---|---|---|
| `app/courts/__init__.py` (38) | фасад пакета | да | тот же | — | — |
| `base.py` (12.8K) | `CourtClient(ABC)`, `PageSnapshot`, `FetchedCard`, исключения, `find_uid`, `check_status` | да\* | `app/courts/base.py` | `parse` и `extract_case_code` уходят из базового класса (клиент больше не парсит); `FetchedCard` расширяется | **R12**: снимок страницы берётся только внутри живого `with` |
| `moscow_mir_court.py` (9.7K) | `MoscowMirCourtClient`, portal `mos-sud`, поиск по UID | да\* | `app/courts/moscow.py` | `parse` (`:156`) удаляется | R14: константы читает `site_probe` |
| `msudrf_court.py` (68K) | `MsudrfCourtClient` + `MsudrfTypeCCourtClient`, 63 домена, капча | да\* | `app/courts/msudrf.py` | **два класса схлопываются в один `MsudrfClient`**; `parse` (`:679-685`) удаляется | R14, R15; 450 строк доменных константов с комментариями переносятся как есть |
| `spb_mir_court.py` (9.4K) | `SpbMirCourtClient`, portal `spb`, Angular-рендер | да\* | `app/courts/spb.py` | `parse` (`:144`) удаляется | ждёт `b.table-title` до 90 с — таймаут не сокращать |
| `resolver.py` (22.2K) | `COURT_BY_PREFIX`, `COURT_BY_DOMAIN` (64 записи), `define_court_by_*`, `portal_for` | да\* | `app/courts/__init__.py` | из `COURT_BY_DOMAIN` исчезает `MsudrfTypeCCourtClient` — 5 домашних регионов (PERM, ADG, TUVA, RIZ, CHEL) указывают на общий `MsudrfClient` | порядок проверки хостов load-bearing (`:307-317`) |
| `site_probe.py` (10.9K) | проверка доступности порталов через прокси | да | `app/courts/site_probe.py` | — | R14: импортирует живые селекторы клиентов (`:27-35`) |
| `tasks.py` | `sync_courts_from_json` | да | `app/tasks.py` | — | без справочника `Court` не работает ничего |

### 1.6 `app/browser/`, `app/captcha/`, `app/storage/`

| Existing file | Responsibility | Move? | New location | Behaviour change | Risk |
|---|---|---|---|---|---|
| `browser/__init__.py` | фасад | да\* | тот же | `lease_proxy` уходит из фасада | R13 |
| `browser/chromium.py` (10.2K) | `ChromiumSession` (sync Playwright), `goto`, `submit_and_wait`, `open_in_new_tab` | да | тот же | — | **R15**; `open_in_new_tab` содержит предупреждение (`:122-127`), что обращение к `response.frame` убивает сессию |
| `browser/proxy.py` (5.3K) | `ProxySettings`, `parse_proxy_url`, `lease_proxy` | да\* | `ProxySettings`/`parse_proxy_url` → `app/browser/proxy.py`; `lease_proxy` → `app/services/` | leasing уходит из `app/browser` | **R13**: сейчас `app/browser` зависит от `app/repositories` — это и есть цикл |
| `browser/relay.py` (14K) | `ProxyRelay`: локальный HTTP-прокси перед SOCKS5-с-авторизацией | да | тот же | — | демон-поток `ThreadingTCPServer` в одном процессе с sync Playwright |
| `captcha/__init__.py`, `captcha/rucaptcha.py` (12.9K) | `solve_image`, `CaptchaAttempt`, `AttemptSink` | да\* | тот же | `report_incorrect` (`:227`) не переносим — не вызывается | ходит напрямую, не через прокси суда (намеренно) |
| `storage/s3.py` | `get_client` (`lru_cache`), `put_object` | да | тот же | — | модульный синглтон |
| `storage/html_snapshots.py` (5.3K) | ключи и запись снапшотов HTML в S3 | да | тот же | — | — |
| `storage/captcha_images.py` (3K) | ключи и запись картинок капчи | да | тот же | — | ключ по URL, не по UID |

### 1.7 `app/api/`, `app/admin.py`

| Existing file | Responsibility | Move? | New location | Behaviour change | Risk |
|---|---|---|---|---|---|
| `api/routes.py` (13.8K) | `POST /search_case`, `GET /search_case/tasks/{id}`, `_sync_by_uid`, `_sync_by_url`, `_enqueue` | да\* | `app/api/search.py` | оркестрация не меняется, но `_sync_by_*` становятся тонкими: решение «уже есть / уже в работе» остаётся, а разбор портала уходит в `discover_case` | UID-ветка всегда пере-обходит, `force` игнорируется (`:47-57`) — сохранить |
| `api/cases.py` (3.5K) | `GET /cases/{id}`, `GET /cases/{id}/summary`, `POST /cases/{id}/monitoring` | да\* | `app/api/cases.py` | `POST …/monitoring` (`:48-61`) **удаляется**; `GET …/summary` (`:33-45`) создавался только для Django-клиента — решение отложено до Phase 10 | — |
| `api/schemas.py` (12.7K) | 17 pydantic-схем | да\* | тот же | удаляются `MonitoringRequest/Response` (`:243-253`), `InstanceOut` (`:142`), поля `monitoring_enabled` (`:198`, `:239`) | — |
| `admin.py` (17.6K) | SQLAdmin: `AdminAuth` + 13 `ModelView`, `ADMIN_TIMEZONE` | да\* | `app/admin.py` | удаляются `InstanceAdmin` (`:243`), `CaseLinkAdmin` (`:275`), bulk-действие `resync_cases` остаётся (это обычный re-sync, не monitoring) | `test_admin_forms.py` фиксирует регрессию sqladmin 0.28 + WTForms 3.2 |

### 1.8 `app/monitoring/` — пакет разбирается целиком

Имя пакета вводит в заблуждение: 2 из 3 модулей — это не monitoring, а ядро синхронизации.

| Existing file | Responsibility | Move? | New location | Behaviour change | Risk |
|---|---|---|---|---|---|
| `monitoring/case_update.py` (9.4K) | `update_case`, `_reconcile`, `CaseChanges` | да\* | `app/services/case_sync.py` | переименование функции в `sync_case`; 7 шагов, их порядок и `CaseChanges` — без изменений | **R19** |
| `monitoring/outbox.py` (7.2K) | `changes_to_events` + 7 сериализаторов payload | да\* | `app/outbox.py` | — | **R20**: baseline-подавление `if changes.is_new: return []` |
| `monitoring/tasks.py` (45.8K) | `sync_case`, `_sync_case` (190 строк оркестрации), `sync_monitored_cases`, `enqueue_case_resync`, 16 приватных хелперов | да\* | разбирается: оркестрация → `app/services/discovery.py` + `app/services/resync.py`; тонкие задачи → `app/tasks.py`; хелперы → по месту | `sync_monitored_cases` (`:614-664`) **удаляется**; `_resolve_card_uid` (`:309-352`) становится публичным хелпером identity | **самый большой риск фазы 8** |
| `monitoring/__init__.py` | комментарий | нет | — | пакет исчезает | — |

### 1.9 `alembic/`, `tests/`, `scripts/`, прочее

| Что | Состояние сейчас | Move? | Решение |
|---|---|---|---|
| `alembic.ini` | `sqlalchemy.url` — заглушка, подменяется в `env.py:16` из `app.config` | да | тот же приём |
| `alembic/env.py` | `target_metadata = Base.metadata` | да | тот же |
| `alembic/versions/*` — **27 миграций**, одна линейная цепочка, head `d2f6a91c74be` | 4 из них — data-миграции (`b3f7c21a9e04`, `c4e8b7a21f60`, `d2f6a91c74be`, `c2d95f31e7a8`) | **нет** | core_v2 идёт на **отдельную БД** `soroka_core_v2` → одна squashed initial-миграция под целевые модели. Историю старого core не копируем |
| `tests/` — 28 файлов, ~340 тестов | требуют настоящий PostgreSQL | да\* | переносятся с адаптацией под новые пути; `conftest.py` — фикстуры `session`, `court`, `no_proxy` |
| `html_examples/` — **81 `.html`**, плоский каталог, без манифеста | 35 имён используются тестами; загрузка через per-module константу `HTML_DIR`, не через conftest | да | переносим целиком (это накопленное знание о реальных сайтах); загрузку выносим в общий conftest-хелпер |
| `data/courts.json` (2.3 МБ) | справочник судов, заливается `sync_courts_from_json` | да | тот же |
| `scripts/build_courts_json.py`, `sync_courts.py`, `save_html.py`, `check_proxy.py`, `search_case.py`, `search_case_browser.py` | вспомогательные CLI | да | переносим |
| `scripts/fetch_case_by_url.py` | **дублирует identity-логику** (`:122-135`: свой `find_uid` + `synthetic_uid` без шага «уже известная карточка по URL») | да\* | переписывается через общий `resolve_case_uid` |
| `_probe_spb.py` в корне core | временный разведочный скрипт с `sys.path.insert(0, "/app")` | **нет** | не переносим |
| `Dockerfile`, `requirements.txt` | pytest 8.3.4 закреплён | да | переносим; **добавляем `pytest.ini`** (см. R17) |
| `pytest.ini` / `pyproject.toml` | **отсутствуют в репозитории** | — | завести в core_v2 явно |

### 1.10 Вне `services/core`

| Что | Решение |
|---|---|
| `services/client/` (Django, 33 файла) | **удалить целиком в Phase 11** |
| `docker-compose.yml` — `client-web`, `client-worker`, `client-beat` | удалить (Phase 11) |
| `docker-compose.yml` — `core-beat` + `MONITORING_*` env (`:204-220`) | удалить (monitoring) |
| `docker-compose.yml` — `CLIENT_DB_*`, `DJANGO_*`, `CORE_API_URL` | удалить (Phase 11) |
| `deploy/postgres/init.sql` — база `soroka_client` | заменить на `soroka_core_v2` |
| `README.md`, `docs/api-overview.md` | обновить (Phase 11) |

---

## 2. Инвентарь CourtClients

| Класс | Файл:строка | portal | page_type | Как достаёт страницу |
|---|---|---|---|---|
| `CourtClient(ABC)` | `app/courts/base.py:136` | — | — | контракт: `fetch_cases_by_uid`, `fetch_case_html_by_url`, `extract_uid`, `extract_case_code`, `parse` |
| `MoscowMirCourtClient` | `app/courts/moscow_mir_court.py:57` | `mos-sud` | `A` | **единственный клиент с поиском по UID.** `goto(https://mos-sud.ru/search)` → `fill('input[name="uid"]')` → `submit_and_wait("#case-index-search-form-btn")` → перебор `div.wrapper-search-tables … a.detailsLink[href*="/details/"]` → `open_in_new_tab(link)` на каждую строку. Без капчи |
| `MsudrfCourtClient` | `app/courts/msudrf_court.py:538` | `msudrf` | `B` | `ChromiumSession(ignore_https_errors=True)` (сертификаты субдоменов не совпадают) → `goto(url)` → `_pass_captcha` (`:590`), до `CAPTCHA_ATTEMPTS` попыток. Капча детектируется **по тексту**, не по селектору: `CAPTCHA_MARK` (`:521`). Картинка снимается **скриншотом** элемента (`:629`), а не повторным запросом `/captcha.php` — иначе картинка перегенерируется (`:622-624`) |
| `MsudrfTypeCCourtClient` | `app/courts/msudrf_court.py:688` | `msudrf` (наследует) | `C` | **Тело класса — одна строка `page_type = "C"` (`:707`).** Ни одного переопределённого метода |
| `SpbMirCourtClient` | `app/courts/spb_mir_court.py:85` | `spb` | `D` | `goto` → `check_status` **до** ожидания (иначе 403 выглядел бы как таймаут 90 с) → `wait_for_selector("b.table-title", timeout=90000)`. Карточка рендерится Angular'ом через `/cases/api/detail/` + опрос `/cases/api/results/` каждые 5 с; `networkidle` срабатывает слишком рано. Прямой вызов API отдаёт 403 — нужны XHR-заголовки страницы |

### 2.1 Отличаются ли B- и C-клиенты механикой похода?

**Нет.** Проверено чтением `msudrf_court.py:688-707`. Подкласс не переопределяет ни
`fetch_case_html_by_url`, ни `_pass_captcha`, ни `_solve_visible_captcha`, ни
`extract_case_code`, ни `parse`, ни `portal`, ни `__init__`. Собственный докстринг класса
(`:691-694`) прямо это утверждает: *«Ходить на портал здесь нечем отличаться… Отличается
только разметка самой карточки… Отсюда и вся разница между клиентами: одна
переопределённая константа.»*

Более того, `page_type` здесь — только **ожидание** вёрстки: фактический тип `parse()`
берёт со страницы через `detect_page_type`, расхождение лишь пишется в лог
(`msudrf_court.py:679-685`, докстринг `:704-706`).

→ В core_v2 остаётся **один `MsudrfClient`**. Это прямое требование ТЗ PRIORITY 12 и 14.

### 2.2 Клиент сегодня сам выбирает и вызывает парсер

Три места:

- `app/courts/moscow_mir_court.py:156` — `return get_parser(self.page_type).parse(html)`
- `app/courts/spb_mir_court.py:144` — то же
- `app/courts/msudrf_court.py:679-685` — `detected = detect_page_type(html)`, предупреждение
  при расхождении, затем `get_parser(detected or self.page_type).parse(html)`

Вызывается из `app/monitoring/tasks.py:554` как `client.parse(card.html)`.
**В core_v2 эта связь разрывается** (ТЗ PRIORITY 6, 13).

### 2.3 Что клиент возвращает сегодня

| Метод | Возвращает | Метаданные навигации |
|---|---|---|
| `fetch_cases_by_uid` | `list[FetchedCard]` (`base.py:41`: `code`, `html`, `participok_no`) | `code` и `participok_no` — из таблицы результатов, **до** открытия карточки |
| `fetch_case_html_by_url` | **простую строку `str`** | никаких |

Из-за второго задача вынуждена сама пересобирать карточку:
`app/monitoring/tasks.py:495` — `cards = [FetchedCard(code=code, html=html)]`, а
`source_url`, `url_court`, `fetched_at` тащит отдельными переменными. Метаданные
разбросаны: `participok_no` на `FetchedCard`; `source_url` в задаче (`tasks.py:434`);
`url`/`status` — только на исключениях в `PageSnapshot`; `captchas_solved` — изменяемое
состояние клиента (`msudrf_court.py:565`).

→ Целевая форма `FetchedCard` для core_v2 (ТЗ PRIORITY 7): `html`, `source_url`,
`case_code`, `participok_no`, `status`, `captchas_solved`.

---

## 3. Детект вёрстки и выбор парсера

`detect_page_type(html) -> str | None` — **`app/parsers/msudrf_shared.py:216`**, не в
registry. Смотрит два CSS-селектора:

- `_TYPE_C_LABELS = "div#contentt td > b"` (`:212`)
- `_TYPE_B_LABELS = "div#contentt td:not([colspan]) > h2"` (`:213`)

**C проверяется первым**, потому что на C-страницах тоже есть `<h2>` — секционные
заголовки; охранник `:not([colspan])` не даёт им прочитаться как B (`:208-210`).
`None` = «это не карточка».

Типы A и D **никогда не детектируются** — берутся из константы клиента.

Реестр: `PARSER_BY_PAGE_TYPE` (`app/parsers/registry.py:13`) — рукописный dict
`{"A": MoscowTypeAParser, "B": MsudrfTypeBParser, "C": MsudrfTypeCParser, "D": SpbTypeDParser}`.
`get_parser(page_type)` (`:27`) создаёт парсер заново на каждый вызов и бросает
`ValueError` (не `CourtError`) на неизвестный тип — поэтому широкий `except Exception`
в `tasks.py:555` ловит это как обычную ошибку разбора, минуя канал `CourtError`/снапшота.

Охранник пустого разбора: `_parse_is_empty(data)` (`tasks.py:286-306`) — см. R11.

---

## 4. Инвентарь identity

### 4.1 Порядок операций (URL-ветка), `app/monitoring/tasks.py:464-496`

1. **Суд определяется до любого сетевого вызова** — `_court_by_url(source_url)`
   (`tasks.py:447`, функция `:273`). Не нашли — терминальный `_mark_failed`.
2. `client.fetch_case_html_by_url(source_url)` (`:473`).
3. `code = client.extract_case_code(html)` (`:478`) — **намеренно первым**, как
   доказательство, что это карточка (комментарий `:474-477`).
4. `uid = _resolve_card_uid(html, source_url, url_court)` (`:479`, функция `:309-352`).
5. Сверка `uid[:8] != url_court.code` — только предупреждение (`:485-493`).
6. `_record_uid(task_id, uid)` (`:494`).

### 4.2 `_resolve_card_uid` — три шага (ТЗ PRIORITY 9)

| Шаг | Строка | Правило |
|---|---|---|
| 1 | `tasks.py:329` | `CaseRepository.get_by_url(source_url)` (сверка по `canonical_case_url`). Карточка нашлась → **её uid побеждает навсегда**; отличающийся uid со страницы только пишется в лог (`:334-342`) |
| 2 | `tasks.py:332, 344` | `find_uid(html)` — настоящий uid со страницы |
| 3 | `tasks.py:347` | `synthetic_uid(url_court.code, source_url)` = `"nouid-" + код_суда + "-" + sha256(canonical_case_url)[:12]` (`validators.py:93,98,101,122`) |

`is_synthetic_uid` — `validators.py:126`. Синтетические uid скрываются из API через
`Case.public_uid` (`database.py:273`) и `routes.py:232`.

### 4.3 UID-ветка (московские дела) — identity другая

`tasks.py:497-503`: uid приходит от пользователя, суд определяется **по участку из ссылки
результата поиска**: `_court_by_participok(uid[:4], card.participok_no)` (`:539`, функция
`:260`). Комментарий `:534-536` фиксирует правило: суд **никогда** не выводится из UID
(участок № 463 → код суда 77MS0466). `_resolve_card_uid` и `find_uid` на этой ветке
не используются вовсе.

### 4.4 Layout-зависимость identity (ТЗ PRIORITY 15)

| Что | Layout-зависимо? | Где |
|---|---|---|
| `find_uid` / `extract_uid` | **нет** — одна общенациональная регулярка | `base.py:14,17,179` |
| `extract_case_code` | **да** — своя регулярка на портал | `msudrf_court.py:535` (`(?:ДЕЛО\|МАТЕРИАЛ)\s*№\s*([^<]+)`), `spb_mir_court.py:73` (`Судебное дело\s*№\s*([^<\|]+)`) |
| `participok_no` | зависит от портала, не от вёрстки | Москва — из `href` (`moscow_mir_court.py:51`); СПб — из пути URL (`spb_mir_court.py:76`); msudrf — из субдомена, т.е. из справочника |

Для msudrf B и C заголовок с номером дела **общий** (об этом прямо сказано в докстринге
`msudrf_court.py:691-694`) → в core_v2 достаточно одного общего хелпера
`extract_msudrf_case_code(html)` без дублирования в B- и C-парсерах (ТЗ PRIORITY 10).

### 4.5 Card key

`Case.card_key` (`app/models/database.py:288`) = `f"{uid}|{court.code}|{code}"`.
Из него выводятся **все** uid дочерних сущностей (`:291-302`) — потому что по одному UID
карточек бывает несколько, а uid дочерних строк уникален глобально.

### 4.6 Определение суда — два независимых механизма

**Клиент** по префиксу/домену — `app/courts/resolver.py`: `COURT_BY_PREFIX` (`:110`,
только `77MS`), `COURT_BY_DOMAIN` (`:116-219`, 64 записи). `client_class_by_url` (`:230`)
перебирает `host_variants(host)` (`validators.py:150`) и сопоставляет по границе имени
(`:294`). **Порядок load-bearing**: точное совпадение первым, варианты вторыми — иначе
`ralt.msudrf.ru` схлопнется в `alt.msudrf.ru`, а `perm` в `prm` (`:307-317`).

**Строку `Court`** из справочника — `CourtRepository.get_by_url` (`repositories/courts.py:162`):
если у хоста есть правило «участок в пути» (`_participok_in_path_rule`, `:34`, сейчас
только `mirsud.spb.ru`) → `get_by_participok`, иначе `get_by_host` (`:116`).

---

## 5. Monitoring — что удаляем (точные адреса)

| Что | Где |
|---|---|
| колонка `Case.monitoring_enabled` + индекс | `app/models/database.py:243-248` |
| `CaseRepository.set_monitoring` | `app/repositories/cases.py:161-167` |
| `CaseRepository.list_monitored_ids` (фильтр на `:197`) | `app/repositories/cases.py:188-207` |
| задача `sync_monitored_cases` | `app/monitoring/tasks.py:614-664` |
| импорты `MONITORING_*` в задачах | `app/monitoring/tasks.py:34-40`, использование `:634-635, 639, 653, 661-662` |
| beat-расписание `sync-monitored-cases` + импорт `crontab` | `app/celery_app.py:9, 34-44` |
| `MONITORING_INTERVAL_HOURS/HOUR/SPACING_SECONDS/BATCH_LIMIT` | `app/config.py:81, 85, 89, 93` |
| эндпоинт `POST /cases/{case_id}/monitoring` | `app/api/cases.py:48-61` (+ импорты `:11-12`) |
| схемы `MonitoringRequest` / `MonitoringResponse` | `app/api/schemas.py:243-253` |
| поле `monitoring_enabled` в схемах ответа | `app/api/schemas.py:198, 239` |
| сервис `core-beat` + `MONITORING_*` env | `docker-compose.yml:204-220` |
| потребитель на стороне клиента | `services/client/apps/core_client/client.py:108-114`, `apps/monitoring/services.py:147, 235-246` |
| миграция, добавившая флаги | `alembic/versions/d8e3a15b7c46_case_monitoring_flags.py` |

**Не путать с обычным re-sync.** `enqueue_case_resync` (`tasks.py:667-707`) — это обычная
backend-операция, она остаётся; удаляется только её планировщик. Bulk-действие админки
`resync_cases` (`app/admin.py:119-139`) тоже остаётся.

**Отложенное решение (Phase 4):** `Case.last_checked_at` (`database.py:252`) существует
именно как курсор планировщика; `Case.last_changed_at` (`:255`) — пользовательское
«последнее обновление» и заполняется при создании (`cases.py:257`). Оба пишутся
`CaseRepository.mark_checked` (`cases.py:169-186`). Судьбу `last_checked_at` решаем
в Phase 4, а не здесь.

---

## 6. Инвентарь synchronization

`update_case(session, uid, data, court, code) -> CaseChanges` —
**`app/monitoring/case_update.py:101-175`**. `court` и `code` передаются аргументами, а не
внутри `data`, потому что оба входят в card key и определяются до вызова
(докстринг `:104-111`). Коммит — забота вызывающего.

### 6.1 Семь шагов, порядок значим

| # | Строка | Что |
|---|---|---|
| 1 | `:113-120` | `CaseRepository.upsert_by_uid_court_code` → `(case, field_changes, is_new)`; затем `add_url` + `mark_url_success` |
| 2 | `:123-126` | Judges: `get_or_create_many(data["judge_names"])` → `_reconcile` |
| 3 | `:129-132` | Sides: `get_or_create_many(data["sides"])` → `_reconcile` |
| 4 | `:135-137` | `EventRepository.sync_events` → (new, updated, removed) |
| 5 | `:141-143` | `PlaceHistoryRepository.sync_place_history` → (new, updated, removed) |
| 6 | `:147-149` | `CourtSessionRepository.sync_court_sessions` → (new, updated, removed) |
| 7 | `:152-154` | `DocumentRepository.sync_documents` → (new, removed) — **ветки `updated` нет**, изменяемых полей не осталось |

Тот же порядок повторяет `changes_to_events`.

`_reconcile(current, desired)` (`:85-98`) — диф по identity, мутирует `current` на месте
(append/remove), чтобы сработала M2M-связь ORM; опирается на то, что
`get_or_create_many` возвращает **те же экземпляры**, что уже лежат в `current`.

### 6.2 Что считается изменением

| Сущность | Где | Изменяемые поля |
|---|---|---|
| поля Case | `repositories/cases.py:229-273` | whitelist `_UPDATABLE_FIELDS` (`:19-31`), 11 полей. **`url` и `code` исключены намеренно** (`:12-18`) |
| Events | `repositories/events.py:49-121` | `event_date` (время может дозаполниться позже, `:106-108`), `document_str`, `published_at` |
| PlaceHistory | `place_history.py:41-97` | только `comment` (`:88-90`) |
| CourtSession | `court_sessions.py:51-118` | `place`, `result`, `basis` (`:101-109`) |
| Document | `documents.py:54-104` | нет |

Во всех четырёх синкерах: проход 1 — создание/обновление по `desired_uids`, проход 2 —
удаление всего, чего не было на странице (`events.py:117-119`, `delete-orphan`).
Дубли со страницы гасятся охранником `if uid in desired_uids: continue`
(`events.py:80`, `place_history.py:71`, `court_sessions.py:82`, `documents.py:85`) —
повторная вставка того же uid уронила бы UNIQUE-индекс вместе со всей транзакцией дела.

### 6.3 Четыре uuid5-namespace — переносить байт-в-байт

| Сущность | Namespace | Ключ | Где |
|---|---|---|---|
| Event | `af75dcd7-7083-4294-8e05-d5f643e533c3` | `card_key \| local_date.isoformat() \| state_description` — **только дата**, из локального времени | `events.py:12, 15-40` |
| PlaceHistory | `6b1f3c02-9a4d-5e77-b8c1-2f0a7d43e915` | `card_key \| place_date \| place_description` | `place_history.py:13, 16-32` |
| CourtSession | `9c4e7a10-2f83-5b6d-a1c7-4e0d9f5b3a26` | `card_key \| local_datetime.isoformat() \| stage` — **со временем** | `court_sessions.py:14, 17-42` |
| Document | `2f7b91c4-6d3e-5a08-9c1f-7b45e0a2d836` | `card_key \| document_date \| document_type \| occurrence` | `documents.py:18, 21-45` |

### 6.4 Ключи реконсиляции

- **Case:** `UniqueConstraint("uid","court_id","code")` (`database.py:175-177`).
- **CaseUrl:** `url` уникален **глобально**, канонизация `canonical_case_url`
  (`database.py:337-357`, `cases.py:130-151`; `ValueError`, если URL уже принадлежит
  другой карточке — `cases.py:143-145`).
- **Judge:** глобальный дедуп по `full_name`, БД-констрейнта нет (`judges.py:14-27`).
- **Side:** `(full_name, role)` — **не** `(full_name, type)`; `type` выводится из
  `_ROLE_TO_TYPE` (`sides.py:9-12, 21-49`).

---

## 7. Timezone

`app/timezones.py`: `TZ_BY_REGION` (`:24-134`, ключ — `Court.region`, **не** префикс кода,
обоснование `:8-11`), `TZ_BY_COURT_CODE` (`:148`, сейчас пустой словарь исключений для
Якутии и Северо-Курильска), `timezone_for(region, code)` (`:151-167`, исключение →
регион; **бросает `KeyError`**, а не подставляет Москву), `to_utc` (`:170`),
`to_court_local` (`:179`).

- `Court.timezone` заполняется при заливке справочника — `repositories/courts.py:209`.
- **Локальное → UTC происходит ровно в двух местах**, на границе БД: `events.py:75`
  и `court_sessions.py:77`. Парсеры отдают наивное локальное время как есть
  (`parsers/base.py:56`, `msudrf_shared.py:59`, `spb_type_d.py:40`).
- **Identity считается по локальному времени, хранение — в UTC** (`events.py:76`,
  `court_sessions.py:78`). Асимметрия намеренная: хеширование UTC заставило бы одинаковое
  локальное время в разных зонах хешироваться по-разному и переписало бы все сохранённые
  uid при переходе на timestamptz (обоснование `court_sessions.py:31-35`).
- **Календарные даты остаются `Date`, фиктивная полночь не выдумывается**: `Case.*_date`,
  `Event.published_at` (`database.py:489`), `PlaceHistory.place_date` (`:522`),
  `Document.document_date` (`:577`). Политика зафиксирована в `database.py:67-78`: все
  *моменты* — `DateTime(timezone=True)`, все *календарные даты* — `Date`.
- Миграция перехода: `alembic/versions/d2f6a91c74be_timezones.py:58, 98-116`.

---

## 8. Outbox

`changes_to_events(changes) -> list[tuple[OutboxEventType, dict]]` —
`app/monitoring/outbox.py:106-149`. Плоский список, порядок ветвлений совпадает
с `update_case`. Сериализаторы payload: `_event_to_dict` (`:36`), `_place_to_dict` (`:47`),
`_session_to_dict` (`:57`), `_document_to_dict` (`:74`), `_field_change_to_dict` (`:86`),
`_judge_to_dict` (`:98`), `_side_to_dict` (`:102`), `_iso` (`:27`).

**Baseline-подавление есть** (ТЗ PRIORITY 20 выполнен уже сегодня):
`outbox.py:117-118` — `if changes.is_new: return []`. Обоснование в докстринге `:110-116`:
на первом обходе вся карточка формально «новая» (десятки строк истории, заседания,
документы), а пользователь ставит дело на мониторинг ровно в этот момент — значит всё это
попало бы ему в уведомления.

**Атомарность есть.** `OutboxEventRepository.emit` (`repositories/outbox_events.py:17-37`)
вызывается внутри **того же** `session_scope`, что и `update_case`:
`app/monitoring/tasks.py:571-596` (`update_case` на `:579`, `mark_checked` на `:587`,
`emit` на `:594-596`). Вызывать обязательно до коммита, пока у удалённых дочерних строк
атрибуты ещё загружены (`outbox.py:114-115`, `tasks.py:591-593`).

Модель `OutboxEvent` (`database.py:820-857`): append-only, колонок доставки нет намеренно
(`:826-830`), `Index("ix_outbox_event_case_created","case_id","created_at")`.
16 значений `OutboxEventType` (`:113-129`).

### 8.1 Две находки, требующие решения в Phase 9

1. **Курсор сегодня — `created_at`, а не `id`.** `list_since(case_id, since, limit)`
   (`outbox_events.py:39-56`) фильтрует `created_at > since`, сортирует по
   `(created_at, id)`. ТЗ §29 Phase 9 требует читать по `id`. Это **намеренное
   изменение** — заносим в раздел 12.
2. **`list_since` не имеет ни одного вызывающего.** HTTP-эндпоинта чтения outbox
   не существует; Django-клиент его никогда не читал (`grep` по `services/client`: только
   `set_monitoring`, `request_case_sync`, `get_search_task`, `get_case`,
   `get_case_summary`). Outbox сегодня — write-only плюс просмотр в админке
   (`admin.py:196-210`). → В Phase 9 эндпоинт **нужно добавить**, иначе будущий Django
   не сможет читать изменения.

Историческая справка: outbox заменил JSONB-колонку `Case.diff_history`
(`outbox.py:1-10`; миграции `b4d21e7c9f30` → `e5b93d7a1c48:63`).

---

## 9. Celery-оркестрация и зависимости клиента

### 9.1 Задачи — всего четыре

| Задача | Файл:строка | Логика внутри задачи |
|---|---|---|
| `sync_case(self, task_id)` | `monitoring/tasks.py:402-421` | **тонкая обёртка.** `Retry` пробрасывается как есть (`:415-418`), любое другое исключение → задача помечается FAILED и пробрасывается, чтобы traceback остался в логах. Охранник нужен потому, что залипшая в RUNNING задача навсегда блокирует этот UID (`:404-412`) |
| `_sync_case(celery_task, task_id)` | `monitoring/tasks.py:424-611` | **толстая: ~190 строк оркестрации.** Разбирается в Phase 8 |
| `sync_monitored_cases(interval_hours, limit)` | `monitoring/tasks.py:614-664` | **удаляется** |
| `sync_courts_from_json(src)` | `courts/tasks.py:20-37` | тонкая, делегирует в `CourtRepository.sync_from_entries` |

Очереди: `urgent` / `regular`, по умолчанию `regular` (`celery_app.py:22-28`);
разнесение по контейнерам — в `docker-compose.yml:137-203`.

### 9.2 Что делает `_sync_case` — карта для разбора в Phase 8

| Строки | Что |
|---|---|
| `:427-433` | пометить RUNNING (своя короткая транзакция), прочитать `uid`/`source_url` |
| `:438-440` | собрать callback учёта стоимости капчи |
| `:447-451` | определить суд по хосту URL, упасть сразу, если его нет в справочнике |
| `:454-504` | браузерная секция: `lease_proxy(portal=portal_for(...))`, затем ветка URL (`fetch_case_html_by_url` → `extract_case_code` → `_resolve_card_uid` → сверка кода суда → `_record_uid` → одна `FetchedCard`) или ветка UID (`fetch_cases_by_uid` → N карточек) |
| `:505-525` | таксономия ошибок: `UnsupportedCourt`/`CaseNotFound` — терминальные → `_mark_failed`; остальное — временные → ручная проверка счётчика попыток и `celery_task.retry(countdown=30)` (комментарий `:517-520` объясняет, почему `MaxRetriesExceededError` поймать нельзя) |
| `:533-596` | цикл по карточкам: суд по участку (`:537-545`), снапшот (`:549`), `client.parse` (`:554`), охранник `_parse_is_empty` (`:561-568`), затем один `session_scope` с `update_case` + `mark_checked` + `emit` |
| `:598-611` | агрегация отказов, `_attach_captcha_costs_to_case`, `_mark_success` |

16 приватных хелперов модуля: `CourtRef` (`:67`), `_log_changes` (`:80`),
`_take_snapshot` (`:124`), `_find_single_card` (`:148`), `_page_status` (`:175`),
`_captcha_recorder` (`:186`), `_attach_captcha_costs_to_case` (`:221`),
`_attach_captcha_costs` (`:234`), `_court_by_participok` (`:260`), `_court_by_url` (`:273`),
`_parse_is_empty` (`:286`), `_resolve_card_uid` (`:309`), `_record_uid` (`:355`),
`_record_error` (`:369`), `_mark_failed` (`:379`), `_mark_success` (`:388`).

### 9.3 Discovery и re-sync сегодня

**Discovery** (новое дело): `POST /search_case` → `request_for_case_sync`
(`api/routes.py:25-44`) → ветка по `looks_like_url`:
- UID: `_sync_by_uid` (`:47-100`) — `validate_uid` → `define_court_by_uid` (проверка
  поддержки) → `list_by_uid` → `get_active_by_uid` (дедуп) → `tasks.create(uid=…)` →
  `_enqueue` (`:201-216`) → `sync_case.apply_async(queue="urgent")`. **UID-ветка всегда
  пере-обходит**, `force` не используется (`:47-57`).
- URL: `_sync_by_url` (`:135-198`) — `validate_url` → `CourtRepository.get_by_url` →
  `is_supported_url` → `cases.get_by_url` (возвращает `exists`, если не `force`) →
  `get_active_by_url` → `tasks.create(source_url=…)` → `_enqueue`.

**Re-sync** (существующее дело): `enqueue_case_resync(case_id, queue, countdown)`
(`monitoring/tasks.py:667-707`) — создаёт **новый** `SearchTask` (по
`CaseRepository.primary_url(case)`, иначе по `case.uid`), затем `apply_async`
**после коммита** (`:704-706`). Дедупа по активным задачам нет намеренно (`:682-685`).
Вызывается из `sync_monitored_cases` (удаляем) и из bulk-действия админки
(`admin.py:119-139`).

`primary_url`: предпочесть последний успешный URL, иначе самый свежий (`cases.py:209-227`).

### 9.4 `SearchTask` — job-record, переносим

Модель `database.py:687-738`, таблица `search_task`,
`CheckConstraint("uid IS NOT NULL OR source_url IS NOT NULL")` (`:704-708`).

Существует потому, что эндпоинт обязан ответить за миллисекунды, а поход в суд занимает
25-35 с с прокси и платной капчей (докстринг `:688-699`). `sync_case` принимает **id
задачи, а не id дела** — отсюда и необходимость `enqueue_case_resync` сначала создавать
задачу.

Жизненный цикл: PENDING (`routes.py:98`/`:196` или `tasks.py:700`/`:702`) →
`apply_async` после коммита → `mark_running` (`tasks.py:432`) → для URL-задач `uid`
дописывается, как только страница его отдала (`_record_uid` → `set_uid`, `tasks.py:355-366`)
→ `mark_success(task, case_id)` по первой сохранённой карточке (`:388-399`, `:611`) или
`mark_failed` (`:379-385`); нетерминальные ошибки идут в `_record_error` (`:369-377`),
оставляя статус RUNNING для повтора. Читается через
`GET /search_case/tasks/{task_id}` (`routes.py:219-238`), синтетические uid маскируются
в `None` (`:232`).

Известная слабость (задокументирована в `tasks.py:404-412` и `:682-685`): жёстко убитый
воркер оставляет задачу в RUNNING навсегда, навсегда блокируя этот UID через API.

Также `SearchTask` — точка привязки учёта капчи: `CaptchaSolve.search_task_id`
пишется своей короткой транзакцией на каждое решение (`tasks.py:186-218`), потом
связывается с делом через `attach_case` (`:221-257`, `:608`).

### 9.5 Зависимости от `services/client`

- **Общего кода нет, общей ORM нет, общих моделей нет.** Ни одного Python-импорта через
  границу ни в одну сторону (проверено `grep`). Намеренно —
  `client/apps/core_client/client.py:3-5`: «две ORM на одних таблицах ломаются на каждой
  alembic-миграции».
- **Событий через RabbitMQ между сервисами нет.** Единственная связь через брокер — оба
  приложения используют один инстанс и потому обязаны согласовать `enable_utc`
  (`core/app/celery_app.py:45-48`); у клиента своё приложение Celery и своя очередь
  `client`.
- **Связь только по HTTP, пять эндпоинтов**, все вызываются из
  `client/apps/core_client/client.py`: `POST /search_case` (`:75-83`),
  `GET /search_case/tasks/{id}` (`:86-91`), `GET /cases/{id}` (`:94-96`),
  `GET /cases/{id}/summary` (`:99-105`), `POST /cases/{id}/monitoring` (`:108-114`).
- **Только для клиента существуют:** `GET /cases/{case_id}/summary`
  (`core/app/api/cases.py:33-45`, докстринг `:36-40` прямо это говорит; схема
  `CaseSummaryResponse`, `schemas.py:217-240`) и `POST /cases/{case_id}/monitoring`.
- **Дублированные константы** (контракт по строкам, не по импорту): `STATUS_*` и `TASK_*`
  в `client/apps/core_client/client.py:17-29` повторяют статусы `core/app/api/routes.py`
  и `SearchStatus`.
- Push'а нет — клиент опрашивает: `poll_pending_cases`, `refresh_case_summaries`
  (`client/config/celery_app.py:33-43`) → `refresh_from_core`
  (`client/apps/monitoring/services.py:153-232`).
- Инфраструктура только под клиента: сервисы `client-web`/`client-worker`/`client-beat`
  в compose, env `CLIENT_DB_*`, `DJANGO_*`, `CORE_API_URL`, база `soroka_client`
  в `deploy/postgres/init.sql`.

---

## 10. Мёртвый код — не переносим

| Что | Где | Почему мёртвое |
|---|---|---|
| `app/domain/__init__.py` | 3 строки комментария про ports & adapters | кода нет; ТЗ §32 запрещает заводить такие слои |
| модель `Instance` + таблица `instance` | `database.py:537-558` | ни парсер, ни задача её не пишут; трогается только из админки (`admin.py:243`) и схемы (`schemas.py:142`) |
| модель `CaseLink` + `Case.case_link_id` | `database.py:373`, `:265` | то же: только админка (`admin.py:275`) |
| `Document.document_text` | `database.py:580-582` | «оставлено для совместимости», никогда не заполняется |
| `Event.document_id` | `database.py:497` | FK есть, никто не выставляет — события и документы код синхронизации не связывает |
| `report_incorrect` | `app/captcha/rucaptcha.py:227` | намеренно никогда не вызывается |
| `_probe_spb.py` | корень `services/core` | временный разведочный скрипт с хардкодом `/app` |
| `Court.region_code` | упоминается в миграции `71aa569d4757`, в модели отсутствует | остаток; в модели есть `region` (свободный текст) и `code`, первые 4 символа которого кодируют регион+уровень |

Стухшие комментарии, которые надо переписать при переносе, а не копировать:
`app/parsers/__init__.py` («Strategy + Registry»), докстринг
`tests/test_parser_registry.py:3-5` («тип C — заглушка», хотя тест `:61` уже разбирает
реальный C), комментарий `app/repositories/cases.py:259-261` (см. R1).

---

## 11. Риски переноса

### R1 — **главный.** Отсутствующий ключ ≠ значение `None`

`upsert_by_uid_court_code` делает `if field not in data: continue`
(`repositories/cases.py:263-268`). Отсюда:

- **ключ есть, значение `None`** = «портал убрал метку со страницы, обнули колонку»;
- **ключа нет** = «этот портал такого поля не имеет, колонку не трогай».

Каждый парсер пре-сеивает **только свой** набор меток: тип A — 11 полей
(`moscow_type_a.py:154-200`, засев `:469`), B/C — ровно 5 из `CARD_FIELDS.values()`
(`msudrf_shared.py:143-161`, засев `msudrf_type_b.py:92` и `msudrf_type_c.py:89`),
D — 4 (`spb_type_d.py:106-111`).

Различия по ключам:

Таблица ниже **перепроверена по golden-файлам** в Phase 2 (не по чтению кода):

| Ключ | A | B | C | D |
|---|---|---|---|---|
| `application_number`, `incoming_number`, `superior_case_number`, `code`, `registration_date` | есть | **нет** | **нет** | **нет** |
| `first_instance_date`, `first_instance_decision`, `decision_effective_date` | есть | есть | есть | **нет** ← уточнение Phase 2 |
| `accepted_date` | **нет** | нет | нет | есть (единственный источник) |
| `events[].published_at` | **ключа нет вовсе** (`moscow_type_a.py:236-242`) | есть | всегда `None` (`msudrf_type_c.py:199`) | есть |
| `place_history`, `court_sessions`, `documents` | заполняются | `[]` | `[]` | `[]` |

Закреплено тестом `test_fields_the_parser_never_reports_stay_absent`
(`services/characterization/test_parsers_golden.py`).

**Комментарий в коде на `cases.py:259-261` утверждает обратное** — «парсер отдаёт ВСЕ ключи
карточки». Это неверно; защищает колонки именно охранник `if field not in data`.
При переносе комментарий исправить.

→ **Наивный `ParsedCase` с `None`-дефолтами молча затрёт колонки.** Typed `ParsedCase`
обязан нести сентинел `UNSET` либо `provided_fields: frozenset[str]`.

### R2 — Порядок строк документов семантически значим

`document_uid(card_key, date, type, occurrence)`, где `occurrence` — сколько таких же
`(date, type)` встретилось **выше на той же странице** (`documents.py:21-45`, счётчик
`:75-84`). Портал отдаёт до 21 одинаковой строки «Приложение» на одну дату. Любая
пересортировка или повторный дедуп в парсере меняет uid и даёт волну фейковых
delete/create в `outbox_event`.

### R3 — Identity по локальному времени, хранение в UTC

`events.py:76`, `court_sessions.py:78`. Асимметрия намеренная (обоснование
`court_sessions.py:31-35`). Event uid берёт только `.date()` — значит изменение времени
даёт UPDATE; CourtSession включает время — значит изменение времени даёт новую строку.

### R4 — Отсутствующее время → локальная полночь, детерминированно

`msudrf_shared.py:75-78`, `spb_type_d.py:95-96`. Не `None`: время участвует в identity
заседания, и недетерминированный фолбэк переписывал бы uid на каждом разборе. Мусор
в ячейке времени не должен убивать строку — `try/except ValueError: pass`, затем полночь.

### R5 — Порядок `CARD_FIELDS` в типе A load-bearing

`Номер дела вышестоящей инстанции` (`moscow_type_a.py:163-167`) обязан идти **до**
`Номер дела` (`:171-175`), и у второго обязан сохраниться якорь `\s*$`. Иначе номер
чужого дела попадёт в `code`.

### R6 — Гомоглифы в типе A

Портал отдаёт `Cудья` с **латинской** `C` (U+0043). Регулярки `[СсCc]удья` (`:71`),
`[СсCc]тороны` (`:73`), и каждая метка в `CARD_FIELDS` (`:154-200`) терпит
Н/H, Д/D, К/K, Т/T, Р/P.

### R7 — Тип A: таблицы только по тексту `<h3>` и по `id` контейнера, никогда по классу

Рядом живут скрытые мобильные клоны тех же таблиц в `div#modalTable` — у них нет ни
`<h3>`, ни `id`; порядок токенов класса тоже плавает (`:26-33`, `_parse_state_history:213-218`,
`_parse_place_history:253-258`, `SESSIONS_CONTAINER:90`, `DOCUMENTS_CONTAINER:93`).
`_documents_table` (`:327-339`) выбирается по тексту заголовка `"Дата документа"`, потому
что в том же контейнере `#act-documents` лежит вторая, неродственная таблица
«Исполнительные документы» с 6 другими колонками.

### R8 — Тип B: колонки движения только по тексту заголовка

Порядок колонок реально различается по регионам (`msudrf_type_b.py:43-48`,
`_event_columns:142-168`). Индексы-фолбэки `0/1/2/3/5` (`:128-132`) применяются **только**
при полном отсутствии `<thead>`; колонка времени по индексу не угадывается никогда
(`:164-166`). Заголовки — `<td>` внутри `<thead>`, **не** `<th>` (`:148`, `:264`, `:302`).
Строка заголовка, протёкшая в `<tbody>`, должна пропускаться (`:197-202`) — иначе
C-страница, разобранная как B, положит текст заголовка в `status` и обойдёт охранник
`_parse_is_empty`.

### R9 — Тип C: транспонированная таблица сторон и colspan-заголовки

`_label` (`:70-80`) возвращает `None` для ячеек с `colspan` — секционные заголовки
`<td colspan><h2>ОСНОВНЫЕ СВЕДЕНИЯ</h2>` не являются метками. Строка заголовка находится
сканированием строки, чьи ячейки — `<b>` и содержат «наименование события»
(`_event_columns:110-141`). Стороны транспонированы (`_parse_sides:219-251`): строки — это
поля, колонки — участники; строка ролей и строка имён сшиваются поэлементно с отбросом
пар, где одна из половин пуста. Число колонок 4 или 5 в зависимости от региона; колонка
времени может быть `None` — это норма, не поломка (`:133-137`).

### R10 — Тип D: только печатная форма

Парсится **только** `div.case-print table.case-print__table`, никогда `section.case-info`
(докстринг `:18-24`): экранное представление повторяет подписи колонок внутри каждой
записи. Карточная таблица опознаётся как «единственная печатная таблица без `<th>`»
(`_card_table:145-150`). События требуют **≥ 4 ячеек** (`:226`) — строже остальных
парсеров. Описание события портал уже склеил через `" / "` — **не склеивать повторно**
(`:239`). `sides[].role` здесь может быть `None` (`:209`) — в отличие от A/B/C; `None`
доживает до колонки `Side.role` (`sides.py:38-49`). `registration_date` для СПб не
существует, и благодаря R1 колонка не обнуляется, а остаётся нетронутой.

### R11 — Пустой документ даёт пустой результат, а не исключение

Браузер иногда возвращает `<html><head></head><body></body></html>`. Каждый парсер обязан
это выдержать (тест `test_empty_document_parses_to_empty_result` в B/C/D). Плюс есть
охранник `_parse_is_empty` (`tasks.py:286-306`): сохранение пустого разбора затёрло бы
события, судей и стороны существующей карточки.

Смежное: строки без `event_date` пропускаются, **но `status` всё равно выставляется**
(B — `msudrf_type_b.py:204-213`, C — `msudrf_type_c.py:179-187`). У только что поданных
дел дат нет ни в одной строке; без этого у них не было бы вообще никаких признаков жизни.

### R12 — Снимок страницы берётся только пока браузер жив

`PageSnapshot` обязан прикрепляться к исключению **внутри** `with`-блока клиента
(`base.py:26-33`, `capture_page` `:100`); вызывающий читает его через
`getattr(exc, "page", None)` (`tasks.py:182`). Любой рефакторинг, выносящий обработку
ошибок из блока сессии, навсегда теряет `status` и `url`.

### R13 — `lease_proxy` открывает DB-сессию из пакета `app/browser`

`app/browser/proxy.py:68` вызывает `session_scope` и `ProxyRepository.lease`, из-за чего
`app/browser` зависит от `app/models` и `app/repositories`. Это и есть цикл, вынуждающий
отложенный импорт внутри функции в `repositories/courts.py:45` (задокументировано
`:39-44`). **В core_v2 leasing выносится из `app/browser`.**

### R14 — Константы клиентов — публичный API

`site_probe.py:27-35` намеренно импортирует живые селекторы и маркеры из клиентов, а не
копирует их. `repositories/courts.py:47-52` импортирует `DOMAIN`, `REGION_CODE`,
`participok_from_url` из `spb_mir_court`. Переименование или перенос этих констант молча
ломает probe-слой и поиск судов СПб.

Строка `portal` связана **тройственно**: `CourtClient.portal` ↔ ключи `SITE_PROBES`
(`site_probe.py:138`) ↔ колонка `Proxy.portals`. Enum'а нет, только литералы
`"mos-sud"`, `"msudrf"`, `"spb"`.

### R15 — Playwright здесь sync API

`sync_playwright()` падает внутри работающего event loop. Сегодня это безопасно **только
потому**, что все роуты в `api/routes.py` объявлены обычным `def`, а походы выполняются
исключительно в Celery-воркерах. В core_v2 правило сохранить явно и записать
в `ARCHITECTURE.md`. Один `sync_playwright().start()` на каждый `ChromiumSession.__enter__`
— это тяжело, но менять не нужно. `ProxyRelay` вертит `ThreadingTCPServer.serve_forever`
на демон-потоке в том же процессе (`relay.py:270`).

### R16 — Тесты требуют настоящий PostgreSQL

`tests/conftest.py:1-9` объясняет: `OutboxEvent.payload` — JSONB, а `ProxyRepository.lease`
использует `FOR UPDATE … SKIP LOCKED`; ни того, ни другого SQLite не умеет. Ни SQLite,
ни testcontainers. Фикстура `session` (`:56-68`) работает через внешнюю транзакцию с
откатом; `test_sync_case_task.py` этим приёмом воспользоваться не может (`_sync_case`
открывает свой `session_scope` и коммитит) и чистит свои строки сам.

### R17 — Конфига pytest в репозитории нет

Ни `pytest.ini`, ни `pyproject.toml`, ни `setup.cfg`, ни `tox.ini`. Pytest работает на
дефолтах с rootdir `services/core`; импорты `app.*` разрешаются только потому, что CWD =
`services/core`. Для core_v2 конфиг завести явно.

### R18 — `canonical_case_url` и `synthetic_uid` — часть identity

`canonical_case_url` (`validators.py:60`, с `MEANINGFUL_QUERY_PARAMS` на `:57`)
используется и как ключ уникальности `CaseUrl.url`, и внутри `synthetic_uid` (`:101`).
Изменение канонизации переписало бы синтетические uid, а значит и `card_key`, а значит и
все uid дочерних строк. Менять нельзя.

### R19 — `Case.card_key` и четыре uuid5-namespace

Формат `f"{uid}|{court.code}|{code}"` (`database.py:288`) и четыре namespace-константы
(раздел 6.3) переносятся байт-в-байт. Разделитель — вертикальная черта без пробелов.

### R20 — Baseline-подавление в outbox

`outbox.py:117-118`. Убрать эту строку значит на первом же discovery сгенерировать
десятки событий на карточку.

### R25 — Флакующий тест в старом core

`tests/test_proxy_relay.py::test_relay_answers_502_when_upstream_is_dead` даёт разный
результат на одинаковых прогонах: три подряд запуска на неизменённом коде — fail, pass,
fail. Тест поднимает локальный сокет и ждёт ответа от заведомо мёртвого upstream, то есть
зависит от таймингов ОС.

Практический вывод: **baseline старого core — это 373-374 passed при 1-2 failed**, и
падение именно этих двух тестов (плюс R23) не является признаком регрессии.

**Закрыто в Phase 6.** Причина найдена: релей на отказе отвечает 502 и сразу закрывает
соединение, а хелпер теста после чтения статуса ещё писал в туннель. Запись в закрытый
сокет на Windows даёт `ConnectionAbortedError` — и не всегда, а в зависимости от того,
дошёл ли RST. В core_v2 у теста на отказ свой хелпер `_connect_status`, который в туннель
не пишет: писать туда после отказа и незачем, туннеля нет. 10 прогонов из 10 зелёные.

---

## 12. Намеренные отличия core_v2 от core

Стартовый список; пополняется по мере выполнения фаз.

| # | Отличие | Обоснование |
|---|---|---|
| 1 | Отдельная БД `soroka_core_v2`, своя история alembic с одной initial-миграцией | решение пользователя; `alembic_version` — однострочная таблица, две независимые истории на одной базе несовместимы |
| 2 | Один `MsudrfClient` вместо `MsudrfCourtClient` + `MsudrfTypeCCourtClient` | ТЗ PRIORITY 12, 14; механика похода идентична (раздел 2.1) |
| 3 | Парсер выбирается **вне** клиента: `get_parser(portal, html)` с обычными `if` | ТЗ PRIORITY 6, 13 |
| 4 | Клиент возвращает расширенный `FetchedCard` вместо `str` | ТЗ PRIORITY 7; сегодня задача пересобирает карточку вручную (`tasks.py:495`) |
| 5 | Typed `ParsedCase` вместо `dict` — с сентинелом `UNSET` | ТЗ PRIORITY 18 + R1 |
| 6 | Outbox читается по `id`, а не по `created_at` | ТЗ §29 Phase 9 |
| 7 | Добавляется HTTP-эндпоинт чтения outbox по курсору | сегодня его нет вовсе (раздел 8.1), без него Django не сможет читать изменения |
| 8 | Оркестрация вынесена из Celery-задачи в `discover_case` / `resync_case` | ТЗ PRIORITY 24 |
| 9 | `update_case` → `sync_case` в `app/services/case_sync.py` | ТЗ PRIORITY 3; поведение и порядок 7 шагов не меняются |
| 10 | Пользовательский monitoring удалён целиком | ТЗ PRIORITY 2 |
| 11 | `lease_proxy` вынесен из `app/browser` | R13 — разрыв цикла импортов |
| 12 | Не переносятся `Instance`, `CaseLink`, `Document.document_text`, `Event.document_id`, `app/domain/`, `report_incorrect`, `_probe_spb.py` | мёртвый код (раздел 10) |
| 13 | Устраняется дублирование: `column_index` (`msudrf_type_b.py:242-247` поверх `msudrf_shared.py:195`), четыре приватные копии `clean`/`parse_date`/`parse_local_datetime`, identity-логика в `scripts/fetch_case_by_url.py:122-135` | реальное дублирование, а не «на будущее» |
| 14 | Заводится `pytest.ini` | R17 |
| 15 | Единый conftest-хелпер загрузки HTML-фикстур вместо per-module константы `HTML_DIR` | 5 копий одного и того же кода |
| 16 | Модели разложены по `app/models/` (8 модулей) вместо одного файла на 857 строк; подключение к БД отдельно в `app/database.py` | было не видно, где кончается «как мы говорим с базой» и начинается «что мы храним» |
| 17 | `app/monitoring/` как пакет исчез: `case_update.py` → `app/services/case_sync.py` | имя пакета вводило в заблуждение — 2 из 3 модулей были ядром, а не мониторингом |
| 18 | `update_case()` → `sync_case()` | подпись и все 7 шагов те же; имя отражает, что это единственная операция синхронизации |
| 19 | Комментарий в `cases.py` про «парсер отдаёт ВСЕ ключи карточки» переписан | утверждение неверно (R1): у типа A 11 скаляров, у B/C 5, у D 4. Код не менялся, только комментарий |
| 20 | Комментарии про планировщик у `Case.last_checked_at` и `mark_checked` переписаны | планировщика в core_v2 нет; колонки остались как факты о деле |
| 21 | `httpx2` добавлен в requirements как тестовая зависимость | нужен `TestClient`; в старом core тесты вызывали функции роутов напрямую, минуя HTTP |
| 22 | Парсеры возвращают typed `ParsedCase` вместо `dict`; отсутствие поля выражено сентинелом `UNSET` | ТЗ PRIORITY 18 + риск R1. Проверено golden-файлами: вывод совпал побайтово на всех 320 сочетаниях парсер×страница |
| 23 | **У типа A в событиях появился ключ `published_at` со значением `None`** | OLD: ключа не было вовсе (на карточке Москвы нет колонки «Дата размещения»). NEW: `ParsedEvent.published_at` есть всегда. REASON: типизированная строка не может иметь разный набор полей в зависимости от портала. На запись не влияет — поле не входит ни в `event_uid`, ни в `_UPDATABLE_FIELDS`, а репозиторий читал его через `.get()` |
| 24 | `clean`, `clean_or_none`, `parse_date`, `parse_local_datetime` вынесены в `app/parsers/text.py` | в старом core существовали в 4 идентичных копиях. Разбор даты-времени у Москвы (одна ячейка) НЕ слит с msudrf/СПб (две колонки) — это разные функции с разным входом |
| 25 | Удалён дубль `column_index` в `msudrf_type_b.py:242-247`, затенявший импорт из `msudrf_shared.py:195` | байт-в-байт одинаковые копии |
| 26 | Адрес страницы уходит в `sync_case` отдельным аргументом `source_url`, а не ключом `"url"` внутри разбора | адрес — не содержимое карточки, а знание того, кто ходил на портал. В старом core его дописывала в словарь Celery-задача уже после парсинга |
| 27 | Репозитории дочерних строк принимают `list[ParsedEvent]` и т.п. вместо `list[dict]` | иначе типизация обрывалась бы на полпути, и понадобился бы слой преобразования, который ТЗ §32 запрещает |
| 28 | Контракт вывода парсера описан типами, а не докстрингом на 70 строк в `base.py` | опечатка в имени ключа больше не означает молча пропавшее поле |
| 29 | **Один `MsudrfClient` вместо двух классов** | ТЗ PRIORITY 12 и 14. `MsudrfTypeCCourtClient` состоял целиком из строки `page_type = "C"`. 63 региона в `COURT_BY_DOMAIN` теперь на одном клиенте |
| 30 | У клиентов больше нет атрибута `page_type` | вёрстка — свойство страницы, а не способа до неё добраться |
| 31 | Клиенты не выбирают и не вызывают парсер; метода `parse` у них нет | ТЗ PRIORITY 6 |
| 32 | `fetch_case_html_by_url` (отдавал `str`) → `fetch_card_by_url` (отдаёт `FetchedCard`) | ТЗ PRIORITY 7. Раньше вызывающий код пересобирал карточку вручную, таская адрес и статус отдельными переменными |
| 33 | `FetchedCard` несёт `html`, `case_code`, `participok_no`, `source_url`, `status`, `captchas_solved` | метаданные навигации не выбрасываются и не выковыриваются повторно из HTML |
| 34 | `extract_case_code` из метода клиента → функции `extract_msudrf_case_code`, `extract_spb_case_code` | ТЗ PRIORITY 10, 11: номер дела это содержимое страницы, а не способ до неё добраться. Одна функция на обе вёрстки движка — заголовок у них общий |
| 35 | Метод `extract_uid` удалён, остался хелпер `find_uid` | его ветка с `CaseNotFound` в бою не работала: у карточек без УИД ключ считается от адреса |
| 36 | `lease_proxy` перенесён из `app/browser/proxy.py` в `app/services/proxy_pool.py` | R13. **Цикл импортов исчез**: отложенный импорт внутри функции в `repositories/courts.py:45` стал обычным импортом на уровне модуля |
| 37 | `report_incorrect` не перенесён | намеренно никогда не вызывался (раздел 10) |
| 38 | **Флакующий тест релея (R25) сделан детерминированным** | 10 прогонов из 10 зелёные вместо 4 падений из 8 |
| 39 | `get_parser(page_type)` → `get_parser(portal, html)` | ТЗ PRIORITY 13. Обычные `if` вместо словаря-реестра: парсеров четыре, и выбор по порталу плюс вёрстке читается сверху вниз. Реестра `PARSER_BY_PAGE_TYPE` больше нет |
| 40 | **Неопознанная вёрстка msudrf → `UnsupportedPage`** | OLD: клиент нёс ожидаемую вёрстку константой, страница разбиралась ожидаемым парсером, разбор выходил пустым, и его отсекала проверка «пустой разбор» в задаче. NEW: явная ошибка. REASON: ожидаемой вёрстки больше не существует — её носил клиент. Итог в обоих случаях окончательный отказ, но теперь по нему видно, что портал сменил разметку |
| 41 | `_resolve_card_uid` → `app/services/identity.py::resolve_case_uid`; сессия приходит аргументом, а не открывается внутри | читать «нет ли уже такой карточки» и потом писать её осмысленно в одной транзакции, и решать это должен вызывающий |
| 42 | Появилась `resolve_case_code(portal, fetched)` | ТЗ PRIORITY 10: номер, известный из навигации, берётся как есть и со страницы не перечитывается |
| 43 | `scripts/fetch_case_by_url.py` больше не содержит своей копии правила опознания карточки | пункт 13 раздела 12. В старом core (`:122-135`) у него была упрощённая версия без шага «карточка по этому адресу уже есть», то есть скрипт и обход могли назвать одну карточку по-разному |
| 44 | Тестам, которым нужен PostgreSQL, проставлен маркер `db` | без него `pytest -m "not db"` всё равно лез в базу и висел на таймаутах подключения |
| 45 | `_sync_case` (190 строк внутри Celery-задачи) разобран на `discover_case` и `resync_case` в `app/services/discovery.py` | ТЗ PRIORITY 24. Обе функции ничего не знают ни про Celery, ни про `SearchTask` |
| 46 | Оба входа лежат в ОДНОМ модуле, а не в `discovery.py` + `resync.py` | так видно, что путь один, а не два похожих. `resync_case` — 15 строк: найти сохранённый источник и позвать `discover_case`. Отдельный файл под это был бы файлом ради названия (ТЗ §32) |
| 47 | Появился `CrawlResult` (`uid`, `court`, `saved_case_ids`, `failures`, `fetched_at`, `captchas_solved`) | вызывающему нужно знать, что вышло из захода, а раньше это знание оставалось внутри задачи |
| 48 | Таксономия ошибок вынесена в `is_terminal(exc)` и `page_status_of(exc)` | раньше `except (UnsupportedCourt, CaseNotFound)` жил прямо в задаче; теперь знание в одном месте, и Celery-обёртка его не дублирует |
| 49 | `retry`, `countdown` и проверка счётчика попыток остались за пределами обхода | обход просто падает исключением; повторять или нет — решает тот, кто его вызвал |
| 50 | Учёт капчи не внутри обхода: `on_captcha` приходит колбэком | у обхода нет ни БД-сессии для этого, ни номера задачи. Привязка расходов к делу (`attach_case`) переезжает во входные точки |
| 51 | `app/monitoring/outbox.py` → `app/outbox.py`, `emit` встроен в транзакцию сверки | ТЗ PRIORITY 19. Baseline-подавление (`if changes.is_new: return []`) перенесено дословно |
| 52 | **Добавлен эндпоинт `GET /cases/{case_id}/events`** | в старом core `list_since` не имел ни одного вызывающего: события писались, а прочитать их можно было только глазами в админке (раздел 8.1). Без него читающий сервис не увидит изменений вовсе |
| 53 | Роут положен прямо в `app/main.py`, пакета `app/api/` нет | под один роут заводить пакет незачем; смысл раскладывать появится, когда роутов станет несколько |
| 54 | **Курсор outbox остаётся `created_at`** — расхождение с ТЗ | ТЗ §29 Phase 9 и критерий приёмки №29 требуют читать по `id`. Переход был сделан и по решению заказчика откачен. Практическое следствие: `created_at` берётся из `func.now()`, то есть из момента начала транзакции, поэтому у всех событий одного обхода метка одинаковая — их взаимный порядок держится только вторичной сортировкой по `id` в `ORDER BY` |
| 55 | `_sync_case` (Celery) → `run_search_task`, `enqueue_case_resync` → `resync_case_task` | задача и бизнес-функция больше не носят одно имя: `sync_case` теперь только операция сверки |
| 56 | Жизненный цикл `SearchTask` и учёт капчи живут в `app/tasks.py`, а не в обходе | обход про задачи не знает: он зовёт колбэки `on_captcha` и `on_uid`, а записывает их вызывающий |
| 57 | Появился колбэк `on_uid` | в старом core УИД дописывался в задачу посреди `_sync_case`. Без колбэка он терялся бы при непредвиденном отказе после похода |
| 58 | `POST /cases/{id}/monitoring` не перенесён, `GET /cases/{id}/summary` перенесён | первый — это мониторинг (ТЗ PRIORITY 2). Второй существовал только ради списка дел в клиенте, но под него уже есть `get_with_court`, и решение — переносить |
| 59 | Роуты разложены по смыслу запроса: `search_case.py`, `cases.py`, `events.py` | в старом core `routes.py` смешивал запуск обхода и чтение |
| 60 | Из админки убраны `InstanceAdmin` и `CaseLinkAdmin` | вместе с мёртвыми моделями |
| 61 | **Починен R23** — предсуществующий красный тест | `test_finished_task_does_not_block_new_one` хардкодил `case_id=1` и падал на внешнем ключе. Теперь дело создаётся фикстурой |
| 62 | Пять тестов `test_sync_case_task.py` снято, их покрытие переехало | они проверяли полный путь через заглушки старого контракта клиента; тот же путь теперь покрыт на настоящих страницах в `test_discovery_and_resync.py` и `test_identity_resolution.py`. Список — в докстринге `tests/test_task_lifecycle.py` |
| 63 | Тесты про `page_type` у клиента переписаны | утверждали, что домен региона резолвится в клиент С ОЖИДАЕМОЙ вёрсткой. Ожидания вёрстки больше нет; осталась половина, которая может сломаться молча — «регион выпал из `COURT_BY_DOMAIN`» |
| 64 | Автофикстура `no_proxy` в `tests/conftest.py` | без неё почти любой тест обхода упирался в `ProxyUnavailable` и сообщал про прокси вместо проверяемого поведения |
| 65 | `services/client` удалён целиком (46 файлов) | ТЗ PRIORITY 1. Восстановим из git, если понадобится |
| 66 | Из compose убраны `client-web`, `client-worker`, `client-beat`, `core-beat` и **все сервисы старого core** | клиент удалён; beat гонял мониторинг, которого нет; старый core остаётся reference — его читают, а не запускают |
| 67 | В compose добавлены `core-v2-api`, `core-v2-worker-urgent`, `core-v2-worker-regular` | общие настройки собраны в якорь `x-core-v2` на верхнем уровне: у трёх контейнеров один образ и одни переменные, различается только команда |
| 68 | Из `.env` убраны `CLIENT_DB_NAME`, `DJANGO_*`, `CORE_API_URL`, все `MONITORING_*`; `DATABASE_URL` → `CORE_V2_DATABASE_URL` | переменные существовали только ради клиента и мониторинга |
| 69 | `deploy/postgres/init.sql`: вместо `soroka_client` заводится `soroka_core_v2` | базы клиента больше нет |
| 70 | `README.md` и `deploy/k8s/README.md` переписаны | описывали каркас из двух сервисов, которого больше нет |
| 71 | `docs/api-overview.md` приведён к текущему API | описывал `POST /cases/{id}/monitoring` и поле `monitoring_enabled`, которых нет. Вместо них — раздел про `GET /cases/{id}/events` с оговоркой, что события это не уведомления и что первый обход их не даёт |**Судьба `Case.last_checked_at`** — решение отложено до Phase 4 (раздел 5).
**Судьба `GET /cases/{id}/summary`** — решение отложено до Phase 10 (раздел 1.7).

---

### R21 — Окружение прогона тестов (закрыто в Phase 2)

Найдено в Phase 1: в системном Python (`python` в PATH — это 3.6.0!) стояла SQLAlchemy
1.4.54 вместо требуемой 2.0.51, сбор тестов падал на
`ImportError: cannot import name 'Uuid'`. Docker Desktop не запущен.

**Решено в Phase 2:** создан venv на Python 3.10.11 в корне репозитория —
`.venv310` — с зависимостями из `services/core/requirements.txt`. PostgreSQL оказался
доступен. Запуск:

```
.venv310\Scripts\python.exe -m pytest -q          # из services/core
.venv310\Scripts\python.exe -m pytest services/characterization -q
```

Venv намеренно лежит **вне** `services/core`: правило миграции №1.

### R22 — В `html_examples/` есть файл не в UTF-8

`mir_court_list_full.html` — 4 МБ в windows-1251. Это не карточка дела, а выгрузка списка
судов с sudrf.ru для `scripts/build_courts_json.py`. Тесты старого core её и не читают
(они используют 35 имён из 81). Любой обход каталога целиком с `encoding="utf-8"` на ней
падает — исключать по имени, а не глушить ошибку декодирования.

### R23 — Предсуществующий падающий тест

`tests/test_case_entry_points.py:426` `test_finished_task_does_not_block_new_one`
хардкодит `repo.mark_success(task, case_id=1)` и падает с `ForeignKeyViolation`, если в
локальной БД нет строки `case` с id=1. Baseline на момент Phase 2: **374 passed,
1 failed** — этот. В core_v2 тест обязан создавать дело фикстурой, а не полагаться на
содержимое базы.

### R24 — `canonical_case_url` делает больше, чем «отбросить utm»

Замерено в Phase 2 (`test_canonical_case_url_is_pinned`): схема приводится к `https`,
хост к нижнему регистру, завершающий слэш пути снимается, а значимые query-параметры
**сортируются по имени**. Каждое из этих действий входит в ключ уникальности
`CaseUrl.url` и в `synthetic_uid`.

---

## 13. Что делать дальше

Phase 2 — characterization tests на старом core до начала переноса.
Основа уже есть: 81 HTML-фикстура, 28 тест-файлов, ~340 тестов.
Добрать по ТЗ §26: полное сравнение вывода всех четырёх парсеров (golden-файлы),
сценарии identity (настоящий/синтетический uid, появление настоящего позже), sync
(первый импорт, повторный без изменений, new/updated/removed), outbox (baseline не даёт
событий, откат транзакции не оставляет строк), timezone.
