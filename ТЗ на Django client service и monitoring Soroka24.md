# 1. Цель и границы сервисов

Создать новый клиентский сервис Soroka24 на обычном Django, без DRF.

`core_v2` остаётся отдельным backend-сервисом и отвечает за судебные данные и их обновление.

Граница ответственности:

```text
Django
- User
- auth
- UI
- подписки пользователей
- unread state
- пользовательские уведомления

        HTTP + RabbitMQ

core_v2
- Case
- CaseEvent
- CourtSession
- Document
- CourtClient
- Parser
- CaseSync
- регулярный monitoring
- domain events
- integration events
```

Главный принцип:

**Django знает, какие дела интересуют пользователей.  
Core знает, как и когда обновлять эти дела.**

Не переносить судебную domain-модель в Django.

---

# 2. Django: минимальная клиентская часть

Использовать обычный Django:

- Django ORM;
- Django Forms;
- Django Templates;
- Django authentication;
- обычные Django views.

Можно и желательно использовать стандартные готовые Django class-based views/generic views там, где они упрощают код:

- `ListView`;
- `DetailView`;
- `FormView`;
- `CreateView`;
- `DeleteView`;
- `LoginView`;
- другие стандартные Django CBV.

Не писать function-based view вручную, если готовый Django class решает задачу понятнее.

При этом не создавать собственную сложную hierarchy CBV.

## Минимальные модели Django

Использовать стандартный `User`.

### CaseSubscription

Означает:

> пользователь подписан на Case из core.

Минимально:

```python
class CaseSubscription(models.Model):
    user = models.ForeignKey(...)
    core_case_id = models.BigIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

Constraint:

```text
(user, core_case_id) UNIQUE
```

Пока не добавлять:

- тариф;
- monitoring interval;
- папки;
- custom title;
- Telegram;
- billing;
- notification preferences.

### UserCaseChange

Означает:

> для конкретного пользователя существует новое изменение по подписанному делу.

Это НЕ копия backend `CaseEvent`.

Минимально:

```python
class UserCaseChange(models.Model):
    user = models.ForeignKey(...)
    subscription = models.ForeignKey(...)
    integration_event_id = models.BigIntegerField()
    event_type = models.CharField(...)
    core_entity_id = models.BigIntegerField(null=True)
    occurred_at = models.DateTimeField()
    read_at = models.DateTimeField(null=True)
```

Constraint:

```text
(user, integration_event_id) UNIQUE
```

Это обеспечивает idempotency при повторной доставке RabbitMQ message.

## Не дублировать судебные данные

Не создавать Django-модели:

```text
Case
CaseEvent
CourtSession
Document
Judge
Side
```

как копии backend.

Для отображения Django получает их через HTTP API `core_v2`.

Например:

```text
Django DetailView
      ↓
core_api.get_case(case_id)
      ↓
HTTP
      ↓
core_v2
      ↓
JSON
      ↓
template context
```

Для взаимодействия с core создать один небольшой integration module, например:

```text
integrations/core.py
```

С простыми функциями вроде:

```python
search_case(...)
get_search_task(...)
get_case(...)
get_cases(...)
sync_monitored_cases(...)
```

Не размазывать `httpx/requests` по Django views.

---

# 3. Monitoring

Django НЕ определяет, когда конкретно надо повторно обходить Case.

Django отвечает только на вопрос:

> какие Case сейчас имеют хотя бы одного активного подписчика?

Из подписок строится distinct список:

```python
CaseSubscription.objects.filter(
    is_active=True,
).values_list(
    "core_case_id",
    flat=True,
).distinct()
```

Django передаёт этот список backend через endpoint, например:

```http
PUT /monitoring/cases
```

```json
{
    "case_ids": [10, 17, 481, 922]
}
```

Semantics запроса:

> после выполнения именно эти дела должны находиться на monitoring.

В `core_v2` добавить:

```python
Case.is_on_monitoring: bool
```

Backend приводит состояние к:

```text
id присутствует → is_on_monitoring=True
id отсутствует  → is_on_monitoring=False
```

Операция должна быть idempotent.

Если:

```text
Natasha → Case 100
Anna    → Case 100
Peter   → Case 200
```

в Django существуют три subscriptions, но backend мониторит:

```text
Case 100
Case 200
```

то есть каждый Case обходится один раз.

## Scheduler

В `core_v2` создать периодическую задачу, которая для MVP запускается раз в сутки.

Она:

```text
SELECT Case
WHERE is_on_monitoring=True
```

и ставит каждому Case обычный существующий re-sync в очередь `regular`.

Не создавать отдельную synchronization logic для monitoring.

Использовать существующий flow:

```text
Case
 ↓
resync
 ↓
CourtClient
 ↓
Parser
 ↓
CaseSync
```

Monitoring только выбирает, какие Case нужно отправить на этот flow.

---

# 4. Domain events, Integration events и Outbox

Чётко разделять три понятия.

## Domain entity

Например:

```text
Case
CaseEvent
CourtSession
Document
```

`CaseEvent` — это реальное судебное событие из карточки дела.

Например:

```text
23.08.2026 — Назначено судебное заседание
```

## Domain event

Это факт внутри backend:

```text
CaseEventAdded
CourtSessionAdded
DocumentAdded
```

Например:

```python
CaseEventAdded(
    case_id=481,
    case_event_id=712,
)
```

Domain event не обязан быть ORM-моделью и не обязан сохраняться отдельно в БД.

Это может быть обычный Python object/dataclass.

После domain event могут запускаться handlers.

Например:

```text
CaseEventAdded
      │
      ├── audit handler
      ├── metrics handler
      └── integration handler
```

Не создавать сложный внутренний event bus, если достаточно нескольких обычных функций.

## Integration event

Domain event НЕ использовать напрямую как публичный контракт между сервисами.

Даже если сейчас поля совпадают, создать отдельное representation.

Причина:

**изменение внутреннего domain event не должно ломать Django.**

Например integration event:

```json
{
    "id": 1502,
    "type": "case_event_added",
    "version": 1,
    "case_id": 481,
    "entity_id": 712,
    "occurred_at": "2026-08-23T13:20:00Z"
}
```

Передавать минимально необходимые данные.

Не передавать:

- SQLAlchemy models;
- Parser;
- внутренние Python objects;
- всю карточку Case целиком.

Полные судебные данные Django при необходимости получает через HTTP API.

## Domain → Integration mapping

Сделать явное преобразование:

```text
DomainEvent
    ↓
IntegrationEvent
```

Например простой функцией:

```python
def to_integration_event(event):
    ...
```

Не создавать mapper framework/factory без необходимости.

---

# 5. Transactional Outbox и RabbitMQ

Для надёжной публикации integration events использовать Transactional Outbox.

Не добавлять:

```text
is_published_to_rabbitmq
```

в судебную сущность `CaseEvent`.

Infrastructure delivery state не должен смешиваться с судебными данными.

Использовать отдельную модель, например существующий `OutboxEvent`, либо переименовать её в:

```text
IntegrationOutboxEvent
```

Минимально:

```text
id
event_type
case_id
entity_id
payload
created_at
published_at nullable
```

## Atomicity

Судебное изменение и Outbox должны записываться в одной DB transaction:

```text
BEGIN

INSERT/UPDATE Case data
INSERT CaseEvent

INSERT IntegrationOutboxEvent

COMMIT
```

Если transaction откатилась — не должно остаться ни изменения Case, ни OutboxEvent.

Добавить test на это.

## Outbox publisher

Создать отдельный backend process/job, который регулярно ищет:

```text
published_at IS NULL
```

и публикует integration event в RabbitMQ.

Для MVP допустим polling примерно раз в секунду.

После успешной публикации:

```text
published_at = now()
```

Не пытаться обеспечить exactly-once delivery.

Нужно рассчитывать на возможную повторную публикацию.

---

# 6. RabbitMQ: отдельная очередь для integration events

Существующие:

```text
urgent
regular
```

оставить для внутренних Celery commands core.

Они означают:

```text
"выполни работу"
```

Создать отдельную очередь, например:

```text
case_changes
```

Она означает:

```text
"работа уже произошла, вот факт изменения"
```

То есть:

```text
regular
   ↓
core Celery worker
   ↓
"переобойди Case"


case_changes
   ↓
Django consumer
   ↓
"Case уже изменился"
```

Не смешивать эти два типа сообщений.

---

# 7. Django RabbitMQ consumer

Создать отдельный постоянно работающий Django process.

Например management command:

```bash
python manage.py consume_case_events
```

Он подключается к RabbitMQ и постоянно слушает:

```text
case_changes
```

Не делать polling RabbitMQ раз в минуту.

Consumer держит соединение открытым, и RabbitMQ отправляет сообщение сразу после его появления.

Для первой реализации можно использовать простой RabbitMQ client вроде `pika`.

Не обязательно использовать Celery protocol для integration messages.

Integration message желательно передавать как обычный JSON.

## Consumer flow

Приходит:

```json
{
    "id": 1502,
    "type": "case_event_added",
    "case_id": 481,
    "entity_id": 712,
    "occurred_at": "...",
    "version": 1
}
```

Django:

```text
получил event
    ↓
нашёл CaseSubscription(core_case_id=481)
    ↓
нашёл всех пользователей
    ↓
создал UserCaseChange каждому
    ↓
успешно сохранил
    ↓
ACK RabbitMQ
```

Если обработка упала до ACK, RabbitMQ может доставить сообщение снова.

Поэтому consumer должен быть idempotent.

Unique:

```text
(user, integration_event_id)
```

обязателен.

---

# 8. Unread и уведомления

`UserCaseChange.read_at=NULL` означает:

> пользователь ещё не видел это изменение.

Главная страница должна уметь показать:

```text
Дело A — 3 новых
Дело B — 1 новое
```

На странице Case можно визуально выделить изменения.

Для MVP допустимо:

> открытие страницы Case помечает все UserCaseChange пользователя по этому Case как прочитанные.

## Судебные данные для отображения

Сами данные нового `CaseEvent` Django не обязан хранить.

Например Django знает:

```text
core_entity_id = 712
event_type = case_event_added
```

Когда пользователь открывает Case:

```text
Django
 ↓
GET core_v2 /cases/481
 ↓
получает актуальный Case + events
 ↓
строит страницу
```

## Notification

На первом этапе НЕ делать сложную модель `Notification`, если она ещё не нужна.

Сначала реализовать рабочую цепочку:

```text
IntegrationEvent
    ↓
Django consumer
    ↓
UserCaseChange
    ↓
unread в UI
```

После этого можно добавить простой email handler.

Например:

```text
UserCaseChange создан
    ↓
send_email(...)
```

и при необходимости добавить в `UserCaseChange`:

```text
notified_at
```

Отдельную модель `Notification` вводить позже, когда реально появятся:

- Telegram;
- email + Telegram одновременно;
- retry;
- delivery status;
- digest;
- несколько notification channels.

Не проектировать notification framework заранее.

---

# 9. Минимальный UI Django

Сделать только первый vertical slice.

## Login

Использовать стандартный Django auth и готовые auth views.

## Мои дела

Использовать `ListView` или другой подходящий стандартный Django CBV.

Показать:

- подписанные дела;
- базовые данные Case из core API;
- unread count.

## Добавить дело

Обычная Django Form + подходящий `FormView`.

Flow:

```text
пользователь вводит URL/UID
    ↓
Django вызывает core discovery API
    ↓
core создаёт/находит Case
    ↓
Django получает core_case_id
    ↓
CaseSubscription
    ↓
синхронизация monitoring list
```

## Страница дела

Использовать `DetailView` либо простой подходящий CBV.

Django получает актуальные судебные данные из core API.

После успешного открытия помечает связанные `UserCaseChange` read.

Красивый frontend пока не является целью.

---

# 10. Порядок реализации

Не делать весь проект одним большим изменением.

## Phase 1 — backend monitoring

Добавить:

```text
Case.is_on_monitoring
PUT /monitoring/cases
daily scheduler
```

Проверить:

```text
subscription list
→ monitoring list
→ regular re-sync
```

Остановиться.

## Phase 2 — DomainEvent → IntegrationEvent → Outbox

Зафиксировать:

```text
DomainEvent
IntegrationEvent
mapping
IntegrationOutboxEvent
published_at
```

Проверить atomicity.

Остановиться.

## Phase 3 — RabbitMQ publisher

Создать:

```text
OutboxPublisher
→ case_changes queue
```

Вручную проверить через RabbitMQ Management UI, что после изменения Case появляется JSON message.

Остановиться.

## Phase 4 — Django skeleton

Создать новый Django service с:

```text
User
CaseSubscription
integrations/core.py
login
Мои дела
Добавить дело
Case page
```

Использовать стандартные Django CBV там, где это делает код проще.

Остановиться.

## Phase 5 — Django consumer

Создать:

```text
consume_case_events
```

Сначала consumer может только логировать полученное сообщение.

Проверить:

```text
core
→ Outbox
→ RabbitMQ
→ Django
```

Остановиться.

## Phase 6 — UserCaseChange

Добавить:

```text
integration event
→ subscribers
→ UserCaseChange
→ unread UI
```

Проверить повторную доставку RabbitMQ.

Остановиться.

## Phase 7 — уведомление

Только после работающего unread flow добавить первый простой notification channel, например email.

Не создавать отдельную сложную `Notification` model, пока она действительно не нужна.

---

# 11. Основные tests

Backend:

- monitoring list sync idempotent;
- Case получает правильный `is_on_monitoring`;
- scheduler выбирает только monitored Case;
- каждый Case ставится на re-sync один раз;
- DomainEvent формируется при реальном diff;
- DomainEvent преобразуется в IntegrationEvent;
- IntegrationEvent сохраняется в Outbox;
- изменение Case и Outbox атомарны;
- unpublished Outbox публикуется;
- опубликованные сообщения корректно помечаются.

Django:

- `(user, core_case_id)` unique;
- monitoring list строится с `distinct`;
- IntegrationEvent на Case с двумя подписчиками создаёт два `UserCaseChange`;
- duplicate RabbitMQ message не создаёт дублей;
- unread count корректен;
- открытие Case выставляет `read_at`;
- судебные domain entities не дублируются в Django DB.

---

# 12. Архитектурная схема

Итоговая схема должна быть такой:

```text
                   DJANGO
                      │
              CaseSubscription
                      │
           distinct core_case_ids
                      │
                      ▼
          PUT /monitoring/cases
                      │
                      ▼

                   CORE
                      │
            Case.is_on_monitoring
                      │
                  daily job
                      │
                      ▼
                  regular
                      │
                      ▼
                core worker
                      │
                      ▼
                 CaseSync
                      │
                 нашли diff
                      │
                      ▼
                 DomainEvent
                      │
                      ▼
             integration handler
                      │
                      ▼
              IntegrationEvent
                      │
               same DB tx
                      │
                      ▼
          IntegrationOutboxEvent
                      │
                      ▼
              OutboxPublisher
                      │
                      ▼
                  RabbitMQ
                case_changes
                      │
                      ▼
               Django consumer
                      │
             найти подписчиков
                      │
             UserCaseChange
                  /       \
                 /         \
                ▼           ▼
             unread       email
```

Судебные данные при отображении:

```text
Django page
    ↓ HTTP
core_v2 API
    ↓
Case / CaseEvent / Session / Document
    ↓
Django template
```

Не копировать их в Django DB без необходимости.

---

# 13. Что не делать сейчас

Без отдельной задачи не делать:

- DRF;
- React/Vue;
- SPA;
- собственный framework CBV;
- копии backend Case/CaseEvent в Django;
- billing;
- тарифы;
- Telegram;
- notification framework;
- digest;
- Kafka;
- CQRS;
- Event Sourcing;
- сложный DDD/event-bus framework;
- generic repositories;
- SDK generator для core API.

Если готовый Django class/function или обычный `if` решает задачу — предпочитать его новой abstraction.

---

# Acceptance criteria

Первый полноценный сценарий должен работать так:

```text
1. Пользователь добавляет Case в Django.

2. Создаётся CaseSubscription.

3. Django отправляет backend distinct список monitored Case.

4. Core выставляет Case.is_on_monitoring=True.

5. Раз в сутки scheduler отправляет Case на обычный re-sync.

6. CaseSync обнаруживает новый судебный CaseEvent.

7. Core создаёт DomainEvent CaseEventAdded.

8. Handler преобразует его в отдельный IntegrationEvent.

9. IntegrationEvent сохраняется в Outbox в одной transaction
   с судебным изменением.

10. OutboxPublisher публикует его в RabbitMQ case_changes.

11. Django consumer получает integration message.

12. Django находит подписчиков Case.

13. Для каждого создаётся UserCaseChange.

14. На главной отображается unread indicator.

15. При открытии Case Django получает судебные данные через core API.

16. UserCaseChange становится read.

17. Судебные сущности не дублируются в Django.

18. Только после этого добавляется простая отправка email.
```

Главный критерий:

**Django зависит только от публичного HTTP API core и стабильного IntegrationEvent contract. Изменения внутренних DomainEvent и моделей core_v2 не должны автоматически ломать клиентский сервис.**