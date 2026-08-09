"""Инстанс Celery для core.

Здесь описаны очереди `urgent` и `regular`. Разнесение воркеров по контейнерам
(-Q urgent / -Q regular) задаётся в docker-compose, а не здесь.

Таски лежат в app/monitoring/tasks.py и app/courts/tasks.py — их подхватит `include`.
"""
from celery import Celery
from celery.schedules import crontab
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

# Расписание фоновых обходов. Работает только при запущенном контейнере core-beat
# (celery -A app.celery_app beat): сам воркер расписание не читает.
celery_app.conf.beat_schedule = {
    # Ежедневный обход дел на мониторинге. Ночью, потому что поход в суд медленный
    # (25-35 секунд), а днём та же очередь нужна свободной.
    "sync-monitored-cases": {
        "task": "app.monitoring.tasks.sync_monitored_cases",
        "schedule": crontab(hour=config.MONITORING_HOUR, minute=0),
        "options": {"queue": "regular"},
    },
}
# Расписание считаем по московскому времени, а сами сообщения — в UTC.
# enable_utc задаём явно: core и client ходят через один брокер, и если их
# приложения разойдутся по этой настройке, воркеры начнут ругаться на расхождение
# часов друг с другом (mingle: "Substantial drift").
celery_app.conf.timezone = "Europe/Moscow"
celery_app.conf.enable_utc = True
