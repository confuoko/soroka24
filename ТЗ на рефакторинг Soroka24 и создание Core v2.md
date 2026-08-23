# 1. Цель

На основе существующего проекта `soroka24` создать новую, упрощённую и понятную версию backend в отдельной директории:

`services/core_v2`

Существующий:

`services/core`

использовать как **read-only reference implementation**.

Не изменять его код в процессе рефакторинга.

Существующую клиентскую часть:

`services/client`

не рефакторить и не переносить. Она больше не нужна и должна быть удалена вместе с инфраструктурой, существующей исключительно ради неё.

Позже я самостоятельно напишу новый client на Django.

Главная ответственность `core_v2`:

**получение судебных страниц → определение судебной карточки → parsing → сохранение/синхронизация судебных данных → фиксация обнаруженных изменений.**

`core_v2` НЕ должен заниматься:

- пользователями;
- подписками пользователей;
- постановкой пользователя на мониторинг;
- снятием с мониторинга;
- тарифами;
- UI;
- notification delivery.

---

# 2. Главный архитектурный принцип

Главная цель рефакторинга — НЕ получить максимально sophisticated архитектуру.

Главная цель:

**получить backend, который я как junior Python developer могу самостоятельно прочитать, проследить и объяснить без помощи AI.**

При выборе между:

1. более абстрактным/расширяемым решением;
2. простым решением, достаточным для существующего проекта;

выбирать второй вариант.

---

# 3. Минимизировать количество abstractions

Если задачу можно понятно решить:

- обычной функцией;
- `if / elif`;
- небольшим helper;
- dictionary mapping;

НЕ создавать для неё отдельный:

- Resolver class;
- Registry class;
- Factory;
- Strategy;
- Protocol;
- Interface;
- Abstract Base Class;
- Manager;
- Handler hierarchy;
- Unit of Work;
- generic repository.

Например вместо:

```python id="c2ofz3"
class ParserResolver:
    ...

parser = ParserResolver(...).resolve(...)
```

при текущем количестве parsers предпочтительно:

```python id="evltwj"
parser = get_parser(portal, html)
```

и внутри допустим обычный:

```python id="k4gqx9"
if portal == "msudrf":
    page_type = detect_msudrf_page_type(html)

    if page_type == "B":
        return MsudrfTypeBParser()

    if page_type == "C":
        return MsudrfTypeCParser()
```

Это НЕ считается плохой архитектурой.

Явность важнее потенциальной расширяемости.

Создавать отдельный класс только если он:

- действительно хранит состояние;
- имеет существенное собственное поведение;
- устраняет реальное дублирование;
- либо уже существуют несколько реализаций, которым действительно нужен общий контракт.

**Блок на архитектурной схеме не означает обязательный Python class.**

---

# 4. Общие правила миграции

1. `services/core` не изменять.
2. Новый backend писать только в `services/core_v2`.
3. Не импортировать runtime-код из `services/core` в `core_v2`.
4. Старый core использовать только как reference.
5. Parsing logic переносить максимально близко к существующей реализации.
6. Не улучшать business logic попутно.
7. Любое намеренное изменение поведения отдельно документировать.
8. Перед переносом сложного поведения добавить characterization/regression tests.
9. Не создавать архитектурные слои «на будущее».
10. Новый core должен запускаться независимо от старого client.
11. Сначала сохранить существующее важное поведение, затем упрощать структуру.
12. После каждого крупного этапа запускать тесты.

---

# PRIORITY 1 — удалить существующий client

Существующий:

`services/client`

не переносить в новую архитектуру.

После создания и проверки `core_v2` удалить старый client.

Также удалить из:

- `docker-compose`;
- environment/config;
- scripts;
- documentation;
- infrastructure;

всё, что существует исключительно для старого client.

Проверить, что `core_v2` не импортирует client и не зависит от его моделей или настроек.

НЕ создавать новый Django client в рамках этого рефакторинга.

Будущая граница:

```text id="h6avx8"
Django
│
├── users
├── auth
├── UI
├── subscriptions
├── billing
└── notification delivery

          HTTP API

FastAPI Core
│
├── courts
├── cases
├── fetching
├── parsing
├── synchronization
└── case change events
```

Django не должен знать, как устроены сайты судов.

FastAPI Core не должен знать, как устроены пользователи.

---

# PRIORITY 2 — удалить существующую business logic monitoring

В `core_v2` НЕ переносить текущую бизнес-логику постановки на мониторинг.

Не переносить:

- `monitoring_enabled`;
- endpoints постановки на мониторинг;
- endpoints снятия с мониторинга;
- `sync_monitored_cases`;
- periodic monitoring scheduler;
- monitoring interval;
- monitoring batch;
- выбор дел для периодического обхода;
- правила определения, находится ли Case на мониторинге;
- пользовательские monitoring semantics;
- код, существующий только ради этих сценариев.

Позже я самостоятельно спроектирую monitoring вместе с Django.

## Важно

Не путать monitoring с обычной повторной синхронизацией дела.

Core должен по-прежнему уметь:

```text id="d2jtfj"
case_id
 ↓
получить Case
 ↓
найти известный источник
 ↓
получить страницу
 ↓
parse
 ↓
sync
```

Это обычная backend operation, а не monitoring.

---

# PRIORITY 3 — сохранить единый CaseSync

Не создавать отдельные независимые механизмы `sync` и `refresh`.

Должна существовать одна фундаментальная операция:

```text id="e56g25"
ParsedCase
    ↓
CaseSync
    ↓
состояние PostgreSQL приводится
к состоянию страницы суда
    ↓
CaseChanges
```

Перенести существующую reconciliation logic из текущего `update_case()` с сохранением поведения.

CaseSync отвечает за синхронизацию:

- Case fields;
- Events;
- CourtSessions;
- Documents;
- Judges;
- Sides;
- PlaceHistory;
- CaseUrl там, где это относится к synchronization;
- других существующих связанных сущностей.

CaseSync возвращает:

```text id="1ts48g"
CaseChanges
```

Сохранить существующие identity rules.

Особенно:

- Case identity;
- CaseUrl canonicalization;
- event_uid;
- new / updated / removed semantics;
- CourtSession identity;
- Document identity;
- Judge reconciliation;
- Side reconciliation.

Не менять эти правила в рамках архитектурного рефакторинга без отдельного обоснования.

---

# PRIORITY 4 — Discovery и Re-sync являются входами в один CaseSync

Не дублировать synchronization logic.

## Discovery

Когда Case ещё неизвестен:

```text id="s6tfbh"
UID / URL
 ↓
найти судебную карточку
 ↓
получить HTML
 ↓
определить identity
 ↓
parse
 ↓
ParsedCase
 ↓
CaseSync
```

## Re-sync

Когда Case уже существует:

```text id="8jzc0a"
case_id
 ↓
получить известный source
 ↓
получить HTML
 ↓
определить/проверить identity
 ↓
parse
 ↓
ParsedCase
 ↓
тот же CaseSync
```

Допустимы небольшие функции/services:

```text id="l21ij4"
discover_case(...)
resync_case(...)
```

или классы, только если они действительно имеют достаточную собственную логику.

Не создавать отдельный `CaseRefreshService` только ради названия.

Discovery и re-sync различаются способом получения исходных данных, но используют один механизм сохранения.

---

# PRIORITY 5 — разделить Fetch, Identity и Parsing

В `core_v2` концептуально разделить три вопроса.

## Fetch / navigation

**Как получить страницу дела?**

Ответственность CourtClient.

## Card identity

**Какую именно судебную карточку мы получили?**

Минимально сюда относятся:

- Court;
- UID;
- case code;
- source URL;

и другие значения только если они реально участвуют в существующей identity.

## Parsing content

**Что написано внутри судебной карточки?**

Например:

- judge;
- sides;
- events;
- sessions;
- documents;
- dates;
- category;
- state;
- остальные содержательные данные.

Ответственность CaseParser.

Это концептуальное разделение.

НЕ создавать автоматически три новых слоя классов.

---

# PRIORITY 6 — CourtClient

CourtClient отвечает прежде всего на вопрос:

**как добраться до страницы дела на конкретном судебном портале?**

CourtClient может отвечать за:

- HTTP/browser navigation;
- открытие URL;
- формы поиска;
- поиск по UID;
- search results;
- переход к карточке дела;
- CAPTCHA;
- proxy;
- cookies/session;
- portal-specific retry;
- transport errors;
- получение HTML.

CourtClient НЕ должен:

- выбирать CaseParser;
- вызывать CaseParser;
- быть привязан к конкретному HTML layout;
- существовать отдельно только потому, что layout страницы отличается;
- разбирать judge/events/sides/documents и другие содержательные поля.

Главное правило:

**CourtClient соответствует способу доступа к судебному порталу, а не типу HTML страницы.**

---

# PRIORITY 7 — CourtClient может возвращать navigation metadata

Не применять механическое правило:

`CourtClient возвращает только HTML`.

Часть identity может становиться известна непосредственно во время navigation.

Например, при поиске московского дела из search results до открытия карточки могут быть известны:

- `case_code`;
- `participok_no`;
- другие metadata результата поиска.

Такие данные нельзя выбрасывать и затем повторно извлекать из HTML.

CourtClient может возвращать существующий или новый минимальный:

```text id="jvg4t6"
FetchedCard
```

с:

- HTML;
- source URL;
- metadata, полученной непосредственно во время navigation/search.

Концептуально:

```python id="km6n4h"
@dataclass
class FetchedCard:
    html: str
    source_url: str
    case_code: str | None = None
    participok_no: int | None = None
```

Не копировать эту структуру буквально без анализа существующих flows.

Сначала определить, какие поля реально необходимы.

---

# PRIORITY 8 — сохранить существующую identity resolution

Перед изменением CourtClient проследить существующий полный flow:

```text id="ayglpb"
UID / URL
  ↓
CourtClient
  ↓
FetchedCard
  ↓
case_code / uid / court resolution
  ↓
Parser
  ↓
CaseSync
```

Особенно проверить:

- `extract_case_code`;
- `extract_uid`;
- `find_uid`;
- `_resolve_card_uid`;
- synthetic UID;
- Case lookup по URL;
- сохранение старого UID;
- card key;
- Case identity;
- identity дочерних сущностей.

Эту логику нельзя случайно изменить.

---

# PRIORITY 9 — UID

Сохранить существующую семантику UID.

Если текущая логика:

1. если Case по URL уже существует — использовать сохранённый UID;
2. иначе если настоящий UID найден на странице — использовать его;
3. иначе создать synthetic UID;

сохранить это поведение.

Если портал позднее начинает показывать настоящий UID для Case, ранее сохранённого с synthetic UID, не менять автоматически identity существующего Case, если это ломает card keys или identity дочерних сущностей.

## UID extraction

Если существующий:

```python id="2mqhqt"
find_uid(html)
```

универсален и не зависит от page layout, оставить его обычным helper.

Не создавать:

```text id="f81z22"
UidExtractor
UidResolverFactory
UidStrategy
```

если обычных функций достаточно.

---

# PRIORITY 10 — Case code

Не переносить `extract_case_code()` механически в CaseParser.

Сначала определить источник case code для каждого discovery flow.

## Moscow

Если case code известен из search result:

```text id="8x3pcv"
search results
    ↓
case_code
    ↓
open card
```

использовать уже известное значение.

Не парсить его повторно без необходимости.

## msudrf

Если case code становится известен только после получения HTML, проверить, одинаков ли extraction для B/C layouts.

Если B/C используют общий заголовок, создать простой общий helper:

```python id="hw21jz"
extract_msudrf_case_code(html)
```

Не дублировать одинаковый extraction в BParser и CParser.

Если в будущем layout использует другой способ extraction, сначала попробовать добавить понятный fallback/`if`.

---

# PRIORITY 11 — не создавать IdentityExtractor class без необходимости

Отдельный:

```text id="gngclp"
CardIdentityExtractor
```

НЕ является обязательным.

Предпочтительно использовать простые helpers:

```python id="nvzzp4"
find_uid(html)
extract_msudrf_case_code(html)
resolve_case_uid(...)
```

и понятный orchestration code.

Допустимо:

```python id="pg8a44"
fetched = client.fetch(...)

page_uid = find_uid(fetched.html)

if fetched.case_code:
    case_code = fetched.case_code
elif portal == "msudrf":
    case_code = extract_msudrf_case_code(fetched.html)
else:
    ...
```

Если это занимает несколько десятков понятных строк, не превращать это в hierarchy классов.

---

# PRIORITY 12 — один Client для одного способа доступа

Один портал и даже один суд могут отдавать несколько HTML layouts.

Поэтому НЕ создавать отдельный CourtClient только из-за layout.

Проверить существующие:

```text id="p8a7bk"
MsudrfCourtClient
MsudrfTypeCCourtClient
```

Если они отличаются только:

- `page_type`;
- parser selection;
- layout;

в `core_v2` должен существовать один:

```text id="9qzj8s"
MsudrfClient
```

Он отвечает за:

- доступ к msudrf;
- browser;
- CAPTCHA;
- proxy;
- получение страницы.

Он не знает, B это, C или будущий E layout.

---

# PRIORITY 13 — выбор Parser

После получения HTML выбрать CaseParser отдельно от CourtClient.

Не создавать `ParserResolver` class без необходимости.

Предпочтительно:

```python id="qyp4g3"
def get_parser(portal: str, html: str) -> CaseParser:
    ...
```

Для msudrf допустимо:

```python id="g4h54q"
if portal == "msudrf":
    page_type = detect_msudrf_page_type(html)

    if page_type == "B":
        return MsudrfTypeBParser()

    if page_type == "C":
        return MsudrfTypeCParser()

    raise UnsupportedPage(...)
```

Если существующий `detect_page_type()` корректен — сохранить его поведение.

---

# PRIORITY 14 — новый layout не создаёт новый Client

Architectural invariant:

Сегодня:

```text id="n0t2ol"
MsudrfClient
    ↓
HTML
    ↓
B / C
```

Если завтра появляется E:

```text id="qskhh5"
MsudrfClient
    ↓
тот же fetch
    ↓
HTML
    ↓
detect E
    ↓
MsudrfTypeEParser
```

не должен появляться:

```text id="n7f9bq"
MsudrfTypeEClient
```

если механизм получения страницы не изменился.

---

# PRIORITY 15 — если identity действительно зависит от layout

Не предполагать заранее, что identity никогда не зависит от layout.

Для каждого parser проверить это.

Если обнаружится layout, где case code/UID можно корректно извлечь только после определения page type, допустима схема:

```text id="wsgonm"
CourtClient
    ↓
HTML
    ↓
detect page type
    ↓
Parser
    ↓
parser.extract_identity(...)
    +
parser.parse(...)
```

Но даже тогда CourtClient не выбирает Parser.

Перед созданием `extract_identity()` у каждого parser сначала проверить, нельзя ли решить задачу общим helper с понятными fallback/if branches.

---

# PRIORITY 16 — CaseParser

CaseParser отвечает:

**как превратить конкретный HTML layout в нормализованные судебные данные?**

Граница:

```text id="rb1j64"
HTML → ParsedCase
```

Parser может извлекать:

- case fields;
- judges;
- sides;
- events;
- sessions;
- documents;
- dates;
- другие domain data.

Parser НЕ должен:

- ходить в сеть;
- выбирать proxy;
- решать CAPTCHA;
- писать в БД;
- открывать DB session;
- выбирать CourtClient.

---

# PRIORITY 17 — не переписывать parsing logic

Парсеры содержат накопленное знание о реальных судебных сайтах.

При переносе нельзя без необходимости:

- сокращать selectors;
- объединять parsing methods;
- удалять fallback logic;
- менять обработку пустых значений;
- менять parsing таблиц;
- менять даты;
- менять deduplication;
- менять judge/sides/events/documents extraction.

Перед переносом каждого parser зафиксировать:

```text id="6sdfe8"
Parser
├── какие страницы обрабатывает
├── какие поля извлекает
├── selectors
├── fallback rules
└── layout-specific особенности
```

После переноса сравнить старую и новую реализацию.

Любое изменение:

```text id="c2ohbz"
OLD → NEW → reason
```

---

# PRIORITY 18 — typed ParsedCase

Если сейчас Parser возвращает большой `dict`, заменить публичный контракт на typed structure.

Минимально рассмотреть:

```text id="5tkmlt"
ParsedCase
ParsedEvent
ParsedCourtSession
ParsedDocument
ParsedSide
```

Использовать dataclasses или Pydantic — выбрать более простой вариант.

Не создавать дополнительный DTO/domain mapping layer поверх них.

Желаемая граница:

```text id="8z74zy"
HTML
 ↓
Parser
 ↓
ParsedCase
 ↓
CaseSync
```

Типизация не должна менять содержимое существующего parser output.

---

# PRIORITY 19 — сохранить OutboxEvent

`OutboxEvent` НЕ удалять вместе с monitoring.

Он не означает пользовательскую подписку.

Он означает:

**core обнаружил изменение судебной карточки.**

Flow:

```text id="vhlg8b"
ParsedCase
 ↓
CaseSync
 ↓
CaseChanges
 ↓
changes_to_events
 ↓
OutboxEvent
```

Сохранить главное свойство Transactional Outbox:

**изменение судебных данных и соответствующие OutboxEvent записываются в одной DB transaction.**

Если transaction откатывается, OutboxEvent тоже не сохраняется.

---

# PRIORITY 20 — baseline

Первичный импорт Case является baseline.

Если при первом discovery страница уже содержит:

- 10 events;
- 3 documents;
- 2 sessions;

не создавать 15 notification-like OutboxEvents только потому, что эти записи впервые появились в нашей БД.

Outbox должен описывать изменения, обнаруженные после первоначального состояния.

Если текущая реализация уже делает это корректно — сохранить поведение.

---

# 
# PRIORITY 22 — Outbox не занимается delivery

Не добавлять в OutboxEvent:

- user_id;
- sent;
- delivered;
- notification_status;
- email;
- Telegram ID.

Core не знает получателей.

Будущий Django самостоятельно будет знать:

```text id="62dtzn"
User
 ↓
CaseSubscription
 ↓
core_case_id
 ↓
last_event_id
```

и решать, какие уведомления кому отправлять.

---

# PRIORITY 23 — пока не добавлять RabbitMQ relay

Не реализовывать сейчас:

```text id="7wtimk"
OutboxEvent
 ↓
OutboxPublisher
 ↓
RabbitMQ
 ↓
Django consumer
```

На первом этапе достаточно будущего:

```text id="6k1jkg"
PostgreSQL
 ↓
OutboxEvent
 ↓
FastAPI HTTP
 ↓
Django
```

Архитектура Outbox должна позволять позже добавить relay, но сам relay сейчас не нужен.

---

# PRIORITY 24 — вынести orchestration из Celery tasks

Celery task должен быть тонким entry point.

Celery отвечает за:

- background execution;
- retry;
- Celery-specific handling.

Celery task не должен содержать основную бизнес-логику:

- определения портала;
- выбора CourtClient;
- получения страницы;
- identity resolution;
- parser selection;
- parsing;
- persistence.

Желаемый flow:

```text id="y9i6zb"
Celery task
    ↓
discover_case / resync_case
    ↓
CourtClient
    ↓
FetchedCard
    ↓
identity resolution
    ↓
get_parser(...)
    ↓
Parser
    ↓
ParsedCase
    ↓
CaseSync
    ↓
Repositories
```

Не переносить старый `sync_monitored_cases`.

---

# PRIORITY 25 — timezone

Сохранить существующее правило.

## Calendar date

Если источник сообщает только:

```text id="9a4p9z"
21.08.2026
```

хранить как:

```python id="gzhf40"
date
```

Не придумывать `00:00`.

## Datetime

Если суд сообщает:

```text id="sod6b3"
21.08.2026 15:30
```

интерпретировать значение как local time соответствующего Court.

Court хранит IANA timezone:

```text id="5z5vua"
Europe/Moscow
Asia/Yekaterinburg
...
```

Перед persistence timestamp переводится в UTC.

PostgreSQL timestamps должны оставаться timezone-aware.

Если identity сущности использует local court date, сохранить это поведение.

Особенно проверить:

- Event;
- CourtSession;
- event_uid.

Не менять timezone logic без обнаруженного дефекта.

---

# PRIORITY 26 — tests

До изменения критической логики добавить characterization tests.

Обязательные сценарии:

## Parsers

Каждый существующий parser на сохранённых HTML fixtures.

Проверить весь output.

## Layout detection

Минимально:

```text id="20gypj"
msudrf B → BParser
msudrf C → CParser
```

Один MsudrfClient должен работать с обоими.

## Moscow discovery

Проверить:

```text id="e8o4kj"
UID
→ search
→ search result metadata
→ card
→ identity
→ parser
→ CaseSync
```

## msudrf B

```text id="az1kyc"
URL
→ Court
→ MsudrfClient
→ HTML
→ identity
→ BParser
→ CaseSync
```

## msudrf C

```text id="u5pzwq"
URL
→ Court
→ тот же MsudrfClient
→ HTML
→ identity
→ CParser
→ CaseSync
```

## Identity

Проверить:

- настоящий UID;
- synthetic UID;
- существующий Case с synthetic UID;
- появление настоящего UID позже;
- case code;
- Case identity;
- CaseUrl canonicalization.

## Sync

Проверить:

- первый import;
- повторный sync без изменений;
- new event;
- updated event;
- removed event;
- аналогичные критические reconciliation cases.

## Timezone

Проверить:

- local court datetime → UTC;
- date остаётся date;
- event_uid использует правильную дату;
- CourtSession timezone.

## Outbox

Проверить:

- initial baseline не создаёт change events;
- subsequent change создаёт OutboxEvent;
- rollback CaseSync не оставляет OutboxEvent;
- события читаются последовательно по `id`.

---

# PRIORITY 27 — желаемая структура core_v2

Не обязательно механически следовать этому дереву.

Это ориентир, а не требование создать каждую папку.

```text id="j6ctcb"
services/core_v2/
│
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── timezones.py
│   │
│   ├── api/
│   │
│   ├── models/
│   │
│   ├── repositories/
│   │
│   ├── parsers/
│   │
│   ├── courts/
│   │
│   ├── services/
│   │   ├── discovery.py
│   │   ├── resync.py
│   │   └── case_sync.py
│   │
│   ├── browser/
│   ├── captcha/
│   └── outbox/
│
├── tests/
├── alembic/
└── ARCHITECTURE.md
```

Не создавать пустые директории.

Если `outbox` состоит из одной небольшой функции, например, не создавать отдельный package только ради архитектурной симметрии.

---

# 28. Основной mental model core_v2

Архитектура должна сводиться примерно к следующему:

```text id="gb9fbu"
                   UID / URL / Case
                          │
                          ▼
                 Court / portal
                          │
                          ▼
                    CourtClient
                 "как получить?"
                          │
                          ▼
                    FetchedCard
                    /         \
                   /           \
        navigation metadata    HTML
                   \           /
                    \         /
                     ▼       ▼
                  identity resolution
                          │
                          ▼
                get_parser(portal, html)
                          │
                          ▼
                      CaseParser
                    "что внутри?"
                          │
                          ▼
                     ParsedCase
                          │
                          ▼
                       CaseSync
                          │
                   ┌──────┴───────┐
                   ▼              ▼
               PostgreSQL     CaseChanges
                                  │
                                  ▼
                             OutboxEvent
```

Не создавать отдельный Python class для каждого прямоугольника.

---

# 29. Порядок выполнения

Не выполнять всё одним огромным refactoring commit.

## Phase 1 — Audit

Сначала ничего не менять.

Изучить текущий `services/core`.

Составить таблицу:

| Existing file | Responsibility | Move to core_v2? | New location | Behaviour change | Risk |

Отдельно перечислить:

- CourtClients;
- Parsers;
- layout detection;
- identity extraction;
- monitoring logic;
- synchronization logic;
- Outbox logic;
- Celery orchestration;
- client dependencies.

После Audit остановиться и показать результат.

---

## Phase 2 — Characterization tests

До рефакторинга зафиксировать существующее критическое поведение.

После выполнения остановиться.

---

## Phase 3 — создать skeleton `core_v2`

Создать самостоятельный:

`services/core_v2`

Не импортировать `services/core`.

После выполнения показать дерево проекта.

---

## Phase 4 — Models / Repositories / CaseSync

Перенести стабильное ядро данных.

Перенести synchronization/reconciliation logic.

Не переносить monitoring scheduling.

Запустить tests.

Остановиться.

---

## Phase 5 — Parsers

Перенести parsing logic.

Ввести typed ParsedCase там, где это можно сделать без изменения поведения.

Запустить characterization tests.

Сравнить старый и новый parser output.

Остановиться.

---

## Phase 6 — CourtClients

Перенести fetching/navigation.

Объединить clients, существующие только из-за разных layouts.

Сохранить navigation metadata.

Убрать зависимость CourtClient → Parser.

Запустить discovery tests.

Остановиться.

---

## Phase 7 — Identity + Parser selection

Реализовать минимально необходимую identity resolution.

Предпочитать helpers и `if`.

Реализовать простой `get_parser(...)`.

Проверить UID/case code/card identity.

Остановиться.

---

## Phase 8 — Discovery / Re-sync

Реализовать оба entry flow поверх одного CaseSync.

Запустить integration tests.

Остановиться.

---

## Phase 9 — Outbox

Перенести Transactional Outbox.

Сохранить atomicity.

Использовать `id` как cursor.

Не реализовывать notification delivery/RabbitMQ relay.

Остановиться.

---

## Phase 10 — FastAPI / Celery

Добавить тонкие API/background entry points поверх уже работающего Python core.

Не переносить monitoring scheduler.

Запустить integration tests.

Остановиться.

---

## Phase 11 — удалить client

После проверки `core_v2` удалить старый `services/client` и связанную исключительно с ним infrastructure.

Не создавать новый Django client.

---

# 30. После каждого Phase

После каждого этапа:

1. перечислить созданные/изменённые файлы;
2. объяснить каждое архитектурное решение;
3. перечислить намеренные differences со старым core;
4. запустить соответствующие tests;
5. показать результат tests;
6. остановиться перед следующим крупным этапом.

Не выполнять автоматически все phases подряд.

Я хочу иметь возможность посмотреть результат каждого этапа.

---

# 31. ARCHITECTURE.md

После завершения создать:

`services/core_v2/ARCHITECTURE.md`

Документ должен быть написан для junior Python developer.

Без сложной архитектурной терминологии там, где она не нужна.

Он должен отвечать:

1. Где начинается FastAPI application?
2. Что делает FastAPI?
3. Где начинается Celery task?
4. Что такое CourtClient?
5. Как выбирается CourtClient?
6. Почему CourtClient соответствует порталу/способу доступа, а не layout?
7. Какие metadata CourtClient может вернуть?
8. Как определяется Court?
9. Как определяется UID?
10. Что происходит с synthetic UID?
11. Как определяется case code?
12. Как определяется page layout?
13. Как выбирается Parser?
14. Что возвращает Parser?
15. Что такое ParsedCase?
16. Где начинается CaseSync?
17. Как работает new / updated / removed?
18. Где находятся identity rules?
19. Где происходит timezone conversion?
20. Что такое CaseChanges?
21. Зачем существует OutboxEvent?
22. Почему OutboxEvent не является notification?
23. Почему Outbox читается по `id`?
24. Что было удалено из старого monitoring?
25. Как будущий Django будет взаимодействовать с Core?

Для основных flows привести ссылки на конкретные функции/файлы.

---

# 32. Что НЕ делать

В рамках этого refactoring запрещено без отдельной необходимости:

- добавлять новую продуктовую функциональность;
- создавать новый Django client;
- проектировать subscriptions;
- проектировать пользователей;
- проектировать billing;
- проектировать notification delivery;
- создавать RabbitMQ Outbox relay;
- переписывать parsers ради красоты;
- менять identity rules;
- менять event_uid;
- менять timezone semantics;
- создавать generic repositories;
- создавать Unit of Work;
- внедрять Clean Architecture;
- вводить ports/adapters;
- создавать Protocol для каждого класса;
- создавать Factory для нескольких `if`;
- создавать Resolver classes там, где достаточно функции;
- создавать пустые packages «на будущее».

---

# 33. Acceptance criteria

Рефакторинг считается успешным, если:

1. существует самостоятельный `services/core_v2`;
2. старый `services/core` остался reference и не изменён;
3. старый client удалён;
4. новый Django client не создан;
5. пользовательская monitoring logic отсутствует;
6. `monitoring_enabled` отсутствует;
7. periodic monitoring отсутствует;
8. ручной re-sync существующего Case возможен;
9. существует единый CaseSync;
10. discovery и re-sync используют один CaseSync;
11. CourtClient отвечает за получение страницы;
12. CourtClient не выбирает Parser;
13. CourtClient может сохранять navigation metadata;
14. identity не потеряна при разделении Client/Parser;
15. один MsudrfClient работает с B и C;
16. добавление нового layout не требует нового Client, если fetch не изменился;
17. parser выбирается после получения HTML;
18. для выбора parser предпочтительно используется простая функция/if;
19. Parser возвращает typed ParsedCase;
20. существующие parser edge cases сохранены;
21. UID/synthetic UID semantics сохранены;
22. Case identity сохранена;
23. CaseUrl semantics сохранена;
24. event_uid сохранён;
25. timezone semantics сохранена;
26. CaseSync возвращает CaseChanges;
27. OutboxEvent создаётся атомарно с изменениями;
28. initial baseline не создаёт ложные change events;
29. Outbox читается по `id`;
30. Core не занимается notification delivery;
31. RabbitMQ Outbox relay отсутствует;
32. Celery tasks являются тонкими entry points;
33. критическое поведение покрыто tests;
34. `ARCHITECTURE.md` позволяет самостоятельно проследить основные flows;
35. в проекте нет abstractions, которые существуют только ради потенциального будущего расширения.

## Главный acceptance criterion

**Я должна иметь возможность открыть `core_v2`, выбрать конкретный сценарий — например поиск дела по UID или повторную синхронизацию дела — и последовательно проследить путь от входных данных до PostgreSQL и OutboxEvent, понимая назначение каждой вызываемой функции без помощи AI.**