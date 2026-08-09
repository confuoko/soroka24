"""Фоновые задачи клиента.

Клиент сам в суд не ходит — этим занимается core. Здесь только опрос core:
узнать, чем кончился разбор, и подтянуть витрину.

Расписание — в config/celery_app.py (beat_schedule).
"""
from celery import shared_task
from celery.utils.log import get_task_logger

from apps.monitoring.models import MonitoredCase
from apps.monitoring.services import refresh_from_core

logger = get_task_logger(__name__)


@shared_task
def poll_pending_cases() -> int:
    """Опросить задачи разбора по делам, которые ещё ищутся.

    Раз в минуту: поход core в суд занимает 25-35 секунд, и пользователь должен
    увидеть результат вскоре после того, как он появился.

    Возвращает число дел, вышедших из состояния pending.
    """
    pending = MonitoredCase.objects.filter(
        state=MonitoredCase.State.PENDING, core_task_id__isnull=False
    )
    settled = 0
    for monitored in pending:
        # Одно упавшее дело не должно уносить весь проход: у остальных свои задачи
        # в core, и к этой ошибке они отношения не имеют.
        try:
            refreshed = refresh_from_core(monitored)
        except Exception as exc:
            logger.warning("Дело id=%s: не удалось обновить: %s", monitored.pk, exc)
            continue
        if refreshed.state != MonitoredCase.State.PENDING:
            settled += 1

    if settled:
        logger.info("Определилось дел: %d", settled)
    return settled


@shared_task
def refresh_case_summaries() -> int:
    """Подтянуть статус и даты по делам на мониторинге.

    Реже, чем опрос pending: дела переобходятся раз в сутки, и чаще спрашивать
    core не о чем.

    Возвращает число обновлённых дел.
    """
    active = MonitoredCase.objects.filter(
        state=MonitoredCase.State.ACTIVE, core_case_id__isnull=False
    )
    updated = 0
    for monitored in active:
        try:
            refresh_from_core(monitored)
        except Exception as exc:
            logger.warning("Дело id=%s: не удалось обновить витрину: %s", monitored.pk, exc)
            continue
        updated += 1
    return updated
