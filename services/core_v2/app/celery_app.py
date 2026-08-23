"""Инстанс Celery для core_v2.

Две очереди:

    urgent   — свежие дела от пользователей, нужен быстрый отклик
    regular  — всё остальное, в том числе ручные переобходы

Разнесение воркеров по очередям (-Q urgent / -Q regular) задаётся в docker-compose, а не
здесь.

**Расписания здесь нет вовсе, и это главное отличие от старого core.** Там жил
`beat_schedule` с ночным заданием `sync-monitored-cases`: раз в сутки оно набирало дела с
флагом `monitoring_enabled` и ставило их в очередь. Ни флага, ни планировщика в core_v2
нет — решать, какие дела и когда переобходить, будет тот сервис, который знает
пользователей и их подписки. Core умеет только «обойди это дело сейчас».

Поэтому и контейнера beat у core_v2 быть не должно.
"""
from celery import Celery
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

# Время внутри Celery — в UTC. Расписаний у нас нет, так что локальный пояс здесь ни на
# что не влияет; выставляем явно, чтобы это не зависело от переменной TZ контейнера.
celery_app.conf.enable_utc = True
