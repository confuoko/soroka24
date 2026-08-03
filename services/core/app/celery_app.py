"""Инстанс Celery для core.

Здесь описаны очереди `urgent` и `regular`. Разнесение воркеров по контейнерам
(-Q urgent / -Q regular) задаётся в docker-compose, а не здесь.

Таски лежат в app/monitoring/tasks.py и app/courts/tasks.py — их подхватит `include`.
"""
from celery import Celery
from kombu import Queue

from app import config

celery_app = Celery(
    "soroka_core",
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
    include=["app.monitoring.tasks", "app.courts.tasks"],
)

# Объявляем очереди, которые будут слушать воркеры.
celery_app.conf.task_queues = (
    Queue("urgent"),    # свежие дела от новых пользователей — быстрый отклик
    Queue("regular"),   # фоновый ежедневный обход существующих дел
)

# По умолчанию задачи без явной маршрутизации уходят в regular.
celery_app.conf.task_default_queue = "regular"

# Повторять подключение к брокеру при старте (на случай, если RabbitMQ ещё
# поднимается). Явно выставляем, чтобы не было CPendingDeprecationWarning в Celery 6.
celery_app.conf.broker_connection_retry_on_startup = True
