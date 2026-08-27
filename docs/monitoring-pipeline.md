# Мониторинг: как данные идут между core_v2 и клиентским сервисом

Документ описывает состояние после Phase 1–7: что именно едет через RabbitMQ, что происходит
при постановке дела на мониторинг, снятии и обновлении карточки, в каком порядке читать код и
какие хвосты остались.

Обоснования решений на стороне core — `services/core_v2/ARCHITECTURE.md` §21–27.
Детали Django-стороны — `services/client/README.md`. Карта эндпоинтов — `docs/api-overview.md`.

---

## 1. Граница сервисов

**core_v2** (FastAPI + Celery + SQLAlchemy, своя БД) знает про суды: `Case`, `Event`,
`CourtSession`, `Document`, парсер, прокси, капчу. **client** (Django, своя БД) знает про людей:
`User`, `CaseSubscription`, `UserCaseChange`. Судебных данных в Django нет ни строки — карточка
берётся у core по HTTP в момент показа.

Между сервисами ровно три канала:

| Канал | Направление | Что везёт |
|---|---|---|
| `PUT /monitoring/cases` | client → core | полный список дел, за которыми надо следить |
| `POST /search_case`, `GET /cases?ids=`, `GET /cases/{id}` | client → core | данные для показа |
| RabbitMQ, очередь `case_changes` | core → client | **только факт**: «по делу 481 появилась сущность 712» |

Через очередь не едут данные — едет указатель. Сообщение остаётся крошечным, а формат —
стабильным. За подробностями клиент идёт по HTTP и находит нужную строку по `id == entity_id`.

Core не знает, КТО следит за делом. Флаг `is_on_monitoring` — один на карточку, а не на подписку.

---

## 2. Очередь `case_changes`

### Топология

Обе стороны объявляют одно и то же, каждая у себя, и обе `durable`:

```
exchange  soroka.case_changes   type=direct  durable=true
queue     case_changes          durable=true
binding   routing_key = "case_changes"
```

* core — `services/core_v2/app/integration_publisher.py:65-73` (kombu `Exchange`/`Queue`)
* client — `services/client/cases/management/commands/consume_case_events.py:99-109` (pika)

Библиотеки разные намеренно: core уже тянет kombu как зависимость celery, Django-сервис celery
не заводит вовсе и берёт pika.

Имена берутся из env `CASE_CHANGES_EXCHANGE` / `CASE_CHANGES_QUEUE`, и `docker-compose.yml`
раздаёт их **всем** контейнерам обеих сторон: расхождение имён не упало бы, а молча увело бы
сообщения в никуда.

Очередь объявляется до первой публикации — иначе изменения, случившиеся раньше первого запуска
подписчика, ушли бы в exchange без привязанной очереди.

**Ловушка с vhost.** `INTEGRATION_BROKER_URL = amqp://soroka:soroka@rabbitmq:5672//` — форма
kombu, где хвост `//` означает vhost `/`. Для pika тот же URL означает **пустой** vhost.
Нормализация — `broker_parameters()`, `consume_case_events.py:41-59`.

### Формат сообщения — это и есть контракт

`message_of()`, `integration_publisher.py:95-102`, ровно шесть полей:

```json
{"id": 1502, "type": "event_new", "version": 1, "case_id": 481, "entity_id": 712,
 "occurred_at": "2026-08-23T13:20:00+00:00"}
```

| Поле | Смысл |
|---|---|
| `id` | PK строки `integration_outbox_event`. На стороне клиента зовётся `integration_event_id` и держит идемпотентность |
| `type` | одно из 16 публичных имён (`event_new`, `session_updated`, `case_field_changed`, …). В таблице лежит строкой, не enum: переименование внутреннего enum не должно требовать миграции публичного контракта |
| `version` | версия контракта, сейчас 1. Consumer сверяет с `settings.INTEGRATION_EVENT_VERSION` и чужую версию считает мусором |
| `case_id` | дело |
| `entity_id` | `id` изменившейся строки; **NULL** у `case_field_changed` — менялось скалярное поле самого дела |
| `occurred_at` | ISO 8601 **со смещением**. Consumer отвергает naive datetime (`consumer.py:116-120`): без смещения не отличить момент по Москве от момента по UTC |

Payload-колонки в таблице нет намеренно: схема таблицы и есть контракт, добавить поле незаметно
нельзя. Набор полей закреплён тестом `test_message_shape_is_the_contract`
(`services/core_v2/tests/test_integration_publisher.py:317`).

`entity_id` — это `id` из `EventOut` / `PlaceHistoryOut` / `DocumentOut` / `CourtSessionOut`
(`app/api/schemas.py`). Не путать с `uid`: `uid` детерминированно считается от `card_key` и служит
**сверке строк между обходами**; `id` — номер строки в базе, и именно он уезжает в очередь. Одно
другое не заменяет.

### Гарантии доставки

**at-least-once с обеих сторон.** Потери не допускаются, дубли допускаются, читающий обязан быть
идемпотентным по `id`.

Publisher (`publish_batch`, `integration_publisher.py:125-159`):

* сначала `producer.publish(...)`, потом `mark_published(sent)`. Обратный порядок терял бы
  сообщения; этот даёт дубли. Выбрана обратимая неприятность;
* исключение внутри порции **не пробрасывается**, а кладётся в `Batch.error`: иначе `session_scope`
  откатил бы отметки уже успешно отправленных, и они уехали бы повторно. Порция, упавшая на
  пятидесятом сообщении, оставляет помеченными первые сорок девять;
* ретраи трёхуровневые: kombu `retry_policy` (5 попыток) внутри `publish`; недоотправленный хвост —
  следующим кругом; падение вне publish (БД недоступна) → лог + sleep + continue, процесс не умирает;
* `published_at` берётся из Python, а не `func.now()`: `now()` вернул бы время начала транзакции,
  то есть до отправки, и задержка доставки считалась бы неверно;
* несколько реплик безопасны — `take_unpublished` делает `ORDER BY id ... FOR UPDATE SKIP LOCKED`.

Consumer (`services/client/cases/consumer.py:58-63`) — три исхода:

| Исход | Действие | Почему |
|---|---|---|
| `PROCESSED` | ack | штатно |
| `MALFORMED` | **ack** | битое сообщение выбрасываем: requeue заблокировал бы очередь навсегда |
| `RETRY` | nack(requeue) | `DatabaseError` — наша авария, сообщение не виновато |

Идемпотентность на приёме держится не проверкой в коде, а `UNIQUE (user, integration_event_id)` +
`bulk_create(ignore_conflicts=True)`: SELECT перед INSERT оставляет окно, в которое пролезет
второй consumer. Побочный эффект приятный — повторная доставка не сбрасывает уже проставленный
`read_at`, конфликтующая строка пропускается, а не обновляется.

---

## 3. Пайплайн А: клиент ставит дело на мониторинг

```
POST /cases/add/  {query}
  │ AddCaseView.form_valid                                views.py:102
  ├─ core.search_case(query)  ──HTTP──▶  POST /search_case      expected 200/202/422
  │    CoreUnavailable → ошибка формы; 500 пользователь не видит
  │
  ├─ ветка «готово» (есть case_ids):
  │    monitoring.subscribe(user, case_ids)                monitoring.py:134
  │      ├─ transaction.atomic: update_or_create(user, core_case_id, is_active=True)
  │      │    повторная подписка не дублируется (uq_subscription_user_case)
  │      │    прежняя отписка «оживает» тем же update_or_create
  │      └─ ПОСЛЕ коммита: sync_monitoring()
  │           ▲ внутри atomic core получил бы список БЕЗ новой подписки
  │           monitored_case_ids() = distinct core_case_id активных подписок ВСЕХ юзеров
  │           core.replace_monitored_cases(ids, force=False)
  │             ──HTTP──▶ PUT /monitoring/cases {case_ids}
  │                 CaseRepository.set_monitoring_list     repositories/cases.py:218
  │                   SELECT known                                        → unknown_ids
  │                   UPDATE SET true  WHERE id IN ids AND NOT flag       → added
  │                   UPDATE SET false WHERE flag AND id NOT IN ids       → removed
  │                   COUNT WHERE flag                                    → monitored
  │             ◀── {monitored, added, removed, unknown_ids}
  │           ошибка core проглатывается: подписка уже у нас,
  │           расхождение снимет следующий успешный синк
  │
  └─ ветка «processing» (task_id):
       PendingCaseSearch.get_or_create(user, core_task_id)
         ▲ uq_pending_user_task — защита от двойного submit формы
       redirect на страницу ожидания, <meta refresh 3s> → PendingSearchView.get
         → monitoring.resolve_pending → GET /search_case/tasks/{id}
              success + case_id → тот же subscribe(...) → тот же sync_monitoring()
              failed            → last_error[:500], resolved_at
       закрытую вкладку добирает cron-команда resolve_pending_searches
```

**Замещающая семантика.** Клиент никогда не говорит «добавь дело X» — он всегда присылает полный
список. Отсюда:

* повторный вызов бесплатен: условия обоих UPDATE отбирают только строки, которым флаг реально
  надо поменять, так что второй запрос даёт `added=0, removed=0`;
* `unknown_ids` считаются отдельным SELECT, потому что `rowcount` у UPDATE не отличает «дела нет
  в базе» от «флаг уже стоял»;
* отдельная сверка не нужна: каждый вызов и есть сверка.

---

## 4. Пайплайн Б: дело обновилось → пользователь видит «новое»

```
core-v2-beat, crontab(hour=MONITORING_HOUR)          celery_app.py:56-64
  └─ app.tasks.sync_monitored_cases → очередь regular            tasks.py:170
       list_monitored_ids(limit)                     repositories/cases.py:283
         WHERE is_on_monitoring ORDER BY last_checked_at ASC NULLS FIRST, id LIMIT n
         ▲ NULLS FIRST: NULL = «не проверяли ни разу», ждёт дольше всех.
           Сортировка по id оставила бы хвост списка необойдённым навсегда
         ▲ частичный индекс ix_case_on_monitoring закрывает и фильтр, и сортировку
       enqueue_case_resync(countdown = position * MONITORING_SPACING_SECONDS)
         ▲ разнос по времени, чтобы не выжечь пул прокси и капчу залпом
       сама задача никуда не ходит и last_checked_at не трогает
  ▼
worker regular: resync_case_task → discovery.resync_case → discover_case → _save_cards
  ▼ ОДНА транзакция                                  discovery.py:302-343
    sync_case(...)                        → CaseChanges
    mark_checked(case, fetched_at, changed=changes.has_changes())
        last_checked_at — всегда, даже на холостом обходе
        last_changed_at — только при непустом diff
    changes_to_events(changes)            → list[DomainEvent]
        ▲ у новой карточки пусто: первый обход — baseline, а не изменения
    OutboxEventRepository.emit(...)       → outbox_event + flush()
        ▲ именно flush даёт id новым Event/Session/Document
    to_integration_events(case.id, ...)   → entity_id = entity.id
        ▲ порядок критичен: вызови до flush — и все entity_id молча стали бы None
    IntegrationOutboxRepository.emit(...) → integration_outbox_event, published_at = NULL
    COMMIT  ← карточка, домен-лог и очередь на публикацию едут вместе или не едут вовсе
  ▼
core-v2-outbox-publisher, цикл ~1/сек            python -m app.integration_publisher
    take_unpublished(100)  WHERE published_at IS NULL ORDER BY id FOR UPDATE SKIP LOCKED
    publish(message_of(row)) → soroka.case_changes / rk=case_changes / delivery_mode=2
    mark_published(sent)
    порция была полной → сразу следующая, иначе sleep(1)
  ▼  RabbitMQ  case_changes
client-consumer                                  manage.py consume_case_events
    свой while + process_data_events(time_limit=1)
      ▲ вместо start_consuming(): иначе выйти по SIGTERM можно только тронув соединение извне
    on_message:
      close_old_connections()
        ▲ именно здесь, а не в consumer.py: у команды нет границы HTTP-запроса,
          на которой работает CONN_MAX_AGE, а внутри handle() вызов рвал бы
          соединение, которое тесты держат в транзакции
      handle(body):
        parse()   обязательные поля, version == 1, occurred_at со смещением,
                  id-шники приводимы к int → иначе Malformed → ack
        fan_out(change):
          подписки WHERE core_case_id = ? AND is_active
          bulk_create([UserCaseChange...], ignore_conflicts=True)
          ▲ одна строка на подписчика; ноль подписчиков — не ошибка
        DatabaseError → RETRY → nack(requeue)
  ▼
«Мои дела»                                       views.py:44-55
    annotate(unread=Count("changes", filter=Q(changes__read_at__isnull=True)))
    .order_by("-unread", "-created_at")     ← дела с новостями наверх
    попадает в частичный индекс ix_change_unread
    витрины одним GET /cases?ids=1,2,3      ← против N+1 по сети
    core недоступен → страница всё равно рисуется, с флагом core_unavailable
  ▼
Карточка дела                                    views.py:244-268
    СНАЧАЛА читаем unread → new_entity_ids, new_field_changes
    шаблон подсвечивает конкретные строки по id == entity_id
    ПОТОМ один UPDATE read_at = now()
    ▲ обратный порядок оставил бы человека без ответа на вопрос
      «а что именно изменилось»
```

`UserCaseChange` — не копия судебного события, а факт «этому человеку есть что показать по делу».
Внутри только указатели (`event_type`, `core_entity_id`), подробности берутся у core при показе.
`event_type` — `CharField` без `choices`: core публикует 16 типов и добавит новые.

---

## 5. Пайплайн В: снятие с мониторинга

```
POST /cases/<core_case_id>/unsubscribe/   → UnsubscribeView (только POST)
  monitoring.unsubscribe(subscription)             monitoring.py:157
    is_active = False
      ▲ строка НЕ удаляется: на ней висят UserCaseChange
    sync_monitoring(force_empty=True)
      monitored_case_ids() → возможно []
      core.replace_monitored_cases(ids, force = force_empty and not ids)
        ▲ форсим ТОЛЬКО когда пустота правдива
        ▲ без force core вернёт 409:
          "empty case_ids would unmonitor N cases; repeat with ?force=true"
      на стороне core: UPDATE SET is_on_monitoring = false WHERE is_on_monitoring
                       (условие NOT IN не добавляется при пустом списке)
```

Снятие у **одного** пользователя не снимает дело с обхода, если на него подписан кто-то ещё:
`monitored_case_ids()` считает distinct по всем активным подпискам всех пользователей.
`distinct` тут не оптимизация — лишний поход на портал стоит прокси и оплаченной капчи.

409 на пустой список — защита ровно от одного сценария: клиент поднялся с пустой базой и случайно
снял с мониторинга всё. Без `force_empty=True` в `unsubscribe` последнее дело осталось бы на
обходе навсегда.

### Обновление списка

Отдельного «обновить» нет. Любое изменение подписок приводит к одному и тому же
`sync_monitoring()` с полным списком — это и постановка, и снятие, и сверка.

Ручной прогон — `manage.py sync_monitoring [--force-empty]`. Нужен, если core был недоступен в
момент подписки, подписки правили через админку или core подняли с чистой базы.

---

## 6. Уведомления: состояние Phase 7

Обвязка боевая и покрыта тестами, **канала доставки нет** — по решению заказчика.

```
manage.py notify_case_changes [--limit N]
  └── notifications.notify_pending(limit)         выборка, группировка, notified_at
        └── deliver(user, core_case_id, changes)  ЗАГЛУШКА: строка в лог
```

`deliver` — единственное место, которое изменится, когда появится почта или Telegram.
Всё вокруг работает.

* Выборка — `UserCaseChange.objects.filter(notified_at__isnull=True)`, попадает в частичный индекс
  `ix_change_unnotified`. Строки берутся выборкой, а не из результата `fan_out`:
  `bulk_create(ignore_conflicts=True)` не заполняет первичные ключи.
* Группировка по «пользователь + дело»: обход находит по 5–8 изменений разом, и уведомление на
  каждое дало бы восемь писем подряд про одно дело. `--limit` режет **группы**, не строки.
* **Сначала доставка, потом отметка.** Наоборот — отметили и упали — и человек не узнает об
  изменении никогда: второй раз строка в выборку не попадёт. Повтор дешевле пропажи.
* Отказ доставки на одной группе не мешает остальным и оставляет группу неотмеченной — попадёт
  в следующий прогон.
* `notified_at` отдельно от `read_at`: «не видел на сайте» и «не получал уведомления» — разные
  факты. Подавлять ли уведомление о прочитанном, решится вместе с каналом: без известной задержки
  доставки сравнивать не с чем.
* Расписания нет намеренно — гонять заглушку по cron незачем. Consumer не тронут: заглушке нечего
  делать в горячем пути очереди.

Реестра каналов и модели `Notification` нет и не будет (ТЗ §13).

---

## 7. С чего начать читать код

Начинать с контракта, а не с потока данных.

| # | Файл | Зачем |
|---|---|---|
| 1 | `services/core_v2/app/models/integration_outbox.py` | схема таблицы **и есть** публичный контракт |
| 2 | `services/core_v2/app/integration_publisher.py:95-102` | `message_of` — те самые шесть полей |
| 3 | `services/client/cases/consumer.py` | другой конец того же контракта: `parse` + `fan_out` |
| 4 | `services/client/cases/models.py:118-206` | `UserCaseChange`: почему UNIQUE, а не проверка; два частичных индекса |
| 5 | `services/core_v2/app/services/discovery.py:302-343` | где рождаются оба outbox'а; порядок `emit` → `to_integration_events` |
| 6 | `services/core_v2/app/repositories/cases.py:218-304` | `set_monitoring_list` (два UPDATE) и `list_monitored_ids` (NULLS FIRST) |
| 7 | `services/client/cases/monitoring.py` | `subscribe` / `unsubscribe` / `sync_monitoring` — синк ПОСЛЕ коммита |
| 8 | `services/client/cases/views.py:215-270` | «сначала читаем, потом помечаем» |
| 9 | `services/client/cases/notifications.py` | обвязка Phase 7 и граница заглушки |
| 10 | `services/core_v2/ARCHITECTURE.md` §21–27 | обоснования решений, которые в коде выглядят странно |

Докстринги в проекте несут «почему», а не «что» — их стоит читать, а не проматывать.

---

## 8. Что не доделано

| Хвост | Где | Комментарий |
|---|---|---|
| **Cron на `resolve_pending_searches`** | нет нигде | Единственный хвост, ломающий пользовательский сценарий: закрыл вкладку на странице ожидания — подписка не создастся, пока команду не запустят руками. Нужен раз в минуту |
| **Канал доставки в `deliver()`** | `cases/notifications.py:75-96` | Phase 7 по ТЗ §8: простой email. `EMAIL_BACKEND` уже в settings; нужны `DEFAULT_FROM_EMAIL`, SMTP, шаблон письма. Меняется одна функция |
| **Расписание `notify_case_changes`** | нет | Заводится вместе с каналом, не раньше |
| Решение «подавлять ли уведомление о прочитанном» | — | Отложено осознанно, см. §6 |
| **k8s-манифесты** | `deploy/k8s/README.md` — только план | beat `replicas: 1` + `Recreate`, миграции в initContainer/Job, CronJob'ы |
| Аутентификации на API core нет | `ARCHITECTURE.md` §25 | Защита сейчас только сетевая |
| Лента изменений наружу, поиск, фильтры | `docs/api-overview.md` §7 | Вне текущего ТЗ |

Чего делать **не надо** (ТЗ §13, прямой запрет): модель `Notification`, реестр каналов, Telegram,
DRF, SPA, digest, Kafka, CQRS, Event Sourcing, generic repositories.

---

## 9. Известные риски

1. **Пользователь, подписавшийся после изменения, его не увидит.** `fan_out` раскладывает
   сообщение по подписчикам, активным в момент его получения. По смыслу верно («новое с прошлого
   раза»), но не очевидно.
2. **`resolve_pending_searches` без cron — потерянные подписки.** Самая дешёвая и самая важная
   из дыр.
3. **Дубли уведомлений** при падении между `deliver` и `update(notified_at=...)`. Заложено
   сознательно, но с настоящим каналом станет видно пользователю.
4. **Расхождение `is_on_monitoring` при недоступном core.** `sync_monitoring` глотает
   `CoreUnavailable` и возвращает `None`; автоматического ретрая нет, только ручной
   `manage.py sync_monitoring`. Периодической сверки по расписанию не хватает.
5. **`unknown_ids` только логируется.** Подписка на потерянное делo останется активной и будет
   уезжать в каждый `PUT /monitoring/cases` вечно. Никто не чистит.
6. **`MALFORMED → ack` теряет сообщение навсегда.** Верное решение против блокировки очереди, но
   dead-letter queue нет — восстановить нечем, останется WARNING в логе.
7. **Публикуются все 16 типов без фильтрации.** Для unread это нормально, для писем — нет:
   человек получит уведомление о смене состава суда. Фильтр понадобится вместе с каналом доставки.

Не риски, хотя выглядят так (проверено, работает как задумано): дубли из publisher'а гасятся
UNIQUE на приёме; `bulk_create(ignore_conflicts=True)` не сбрасывает `read_at`; несколько реплик
publisher'а безопасны благодаря `SKIP LOCKED`; несколько реплик consumer'а безопасны благодаря
UNIQUE; повторный `PUT /monitoring/cases` бесплатен.

---

## 10. Как проверить, что цепочка жива

```bash
# формат сообщения закреплён тестом
docker compose exec core-v2-api pytest tests/test_integration_publisher.py -q

# цепочка на стороне клиента
docker compose exec client-web pytest cases/tests/test_consumer.py \
    cases/tests/test_unread.py cases/tests/test_notifications.py -q
```

Метрики здоровья:

* длина очереди `case_changes` (http://localhost:15672 → Queues) — растёт, значит
  `client-consumer` не работает;
* число строк с пустым `published_at` в `integration_outbox_event` — растёт, значит не работает
  `core-v2-outbox-publisher`.
