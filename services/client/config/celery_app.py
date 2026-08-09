"""Инстанс Celery для client.

Отдельное приложение и ОТДЕЛЬНАЯ очередь `client`: воркеры core слушают urgent и
regular, и если бы задачи клиента шли туда же, их подхватил бы воркер, в котором
нет ни Django, ни его настроек.

Задачи лежат в apps/*/tasks.py — их подхватывает autodiscover_tasks по
INSTALLED_APPS.
"""
import os

from celery import Celery
from celery.schedules import crontab
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

celery_app = Celery("soroka_client")

# Настройки берём из settings.py (все с префиксом CELERY_).
celery_app.config_from_object("django.conf:settings", namespace="CELERY")
celery_app.autodiscover_tasks()

celery_app.conf.task_queues = (Queue("client"),)
celery_app.conf.task_default_queue = "client"
celery_app.conf.broker_connection_retry_on_startup = True

# Те же значения, что у core: приложения ходят через один брокер, и разойдись они
# по этим настройкам — воркеры начнут ругаться на расхождение часов друг с другом.
celery_app.conf.timezone = "Europe/Moscow"
celery_app.conf.enable_utc = True

celery_app.conf.beat_schedule = {
    # Дело добавляют — core заводит задачу и сразу отдаёт её id, а поход в суд идёт
    # ещё полминуты. Опрашиваем часто, чтобы пользователь быстро увидел результат.
    "poll-pending-cases": {
        "task": "apps.monitoring.tasks.poll_pending_cases",
        "schedule": crontab(minute="*"),
    },
    # Витрину (статус, даты) подтягиваем редко: дела переобходятся раз в сутки.
    "refresh-case-summaries": {
        "task": "apps.monitoring.tasks.refresh_case_summaries",
        "schedule": crontab(minute="*/15"),
    },
}
