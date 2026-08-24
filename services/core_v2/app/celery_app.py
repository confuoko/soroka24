"""Инстанс Celery для core_v2.

Две очереди:

    urgent   — свежие дела от пользователей, нужен быстрый отклик
    regular  — всё остальное, в том числе ручные переобходы

Разнесение воркеров по очередям (-Q urgent / -Q regular) задаётся в docker-compose, а не
здесь.

## Расписание

Одно задание: раз в сутки набрать дела с флагом `is_on_monitoring` и поставить их на
обычный повторный обход. Кто на что подписан, core не знает — флаг ему выставляет
клиентский сервис запросом `PUT /monitoring/cases`. Разделение такое:

    клиентский сервис            core
    какие дела интересны   →     как и когда их обновлять

Планировщику нужен ОДИН процесс beat (`celery -A app.celery_app beat`), и ровно один:
две реплики дадут двойной обход, то есть двойной расход прокси и капчи.
"""
from celery import Celery
from celery.schedules import crontab
from kombu import Queue

from app import config

celery_app = Celery(
    "soroka_core_v2",
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
    include=["app.tasks"],
)

celery_app.conf.task_queues = (
    Queue("urgent"),   # свежие дела от пользователей — быстрый отклик
    Queue("regular"),  # всё остальное
)

# Задачи без явной маршрутизации уходят в regular: ручной прогон не должен вытеснять
# срочные запросы пользователей.
celery_app.conf.task_default_queue = "regular"

# Повторять подключение к брокеру при старте (RabbitMQ может ещё поднимиться). Задаём
# явно, чтобы не было CPendingDeprecationWarning в Celery 6.
celery_app.conf.broker_connection_retry_on_startup = True

# Время внутри Celery — в UTC. Выставляем явно, чтобы час ночного прогона не зависел от
# переменной TZ контейнера: без этого MONITORING_HOUR=3 означал бы разное время на
# машине разработчика и на сервере.
celery_app.conf.enable_utc = True

# Ночной прогон дел на мониторинге. Задача только выбирает дела и ставит их в очередь;
# сам обход — обычный resync, тот же, что у ручного прогона и у запроса пользователя.
celery_app.conf.beat_schedule = {
    "sync-monitored-cases": {
        "task": "app.tasks.sync_monitored_cases",
        "schedule": crontab(hour=config.MONITORING_HOUR, minute=0),
        # В regular, а не в urgent: ночной прогон не должен вытеснять из очереди дела,
        # которые пользователь добавляет прямо сейчас и ждёт на экране.
        "options": {"queue": "regular"},
    },
}
