"""Операции над делами на мониторинге — общие для веба, API и будущего бота.

Как и в accounts/services.py, функции принимают доменные объекты (User,
MonitoredCase) и не знают ни про request, ни про DRF. Всё, что специфично для
веба, остаётся во views.py, всё, что для API, — в apps/api/.

Разделение обязанностей с core:
    core  — умеет ходить в суд, разбирать карточку и хранить дело;
    client — знает, КТО за каким делом следит, и показывает это пользователю.
"""
import datetime as dt
import logging

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core_client import client as core
from apps.monitoring.models import MonitoredCase

logger = logging.getLogger(__name__)

_validate_url = URLValidator(schemes=["http", "https"])


def add_case_to_monitoring(user, url: str) -> MonitoredCase:
    """Поставить дело на мониторинг по ссылке на его карточку.

    Порядок такой:
      1. проверяем, что это вообще адрес (в суд за этим ходить не надо);
      2. если пользователь уже добавлял эту ссылку — отдаём, что есть;
      3. просим core разобрать дело;
      4. раскладываем ответ core по состояниям MonitoredCase.

    Дело в core может уже существовать (его добавил другой пользователь) — тогда
    ответ придёт со status="exists" и готовым case_id, и ходить в суд не придётся
    вовсе. Поход занимает полминуты, требует прокси и платной капчи, поэтому
    экономить на нём стоит.

    Поднимает ValidationError с человеческим текстом — его показывает и форма, и
    API, и бот.
    """
    url = (url or "").strip()
    try:
        _validate_url(url)
    except ValidationError:
        raise ValidationError("Это не похоже на ссылку на карточку дела.")

    existing = MonitoredCase.objects.filter(user=user, source_url=url).first()
    if existing is not None:
        return existing

    try:
        answer = core.request_case_sync(url)
    except core.CoreApiError as exc:
        logger.warning("core-api не принял ссылку %s: %s", url, exc)
        raise ValidationError(
            "Сервис поиска дел сейчас недоступен, попробуйте позже."
        ) from exc

    status = answer.get("status")

    if status == core.STATUS_EXISTS:
        # Дело уже разобрано — сразу активное, в суд идти не нужно.
        case_id = answer.get("case_id")
        monitored = _create(user, url, state=MonitoredCase.State.ACTIVE, core_case_id=case_id)
        _enable_monitoring_in_core(monitored)
        refresh_from_core(monitored)
        return monitored

    if status == core.STATUS_PROCESSING:
        # Задача заведена, дело появится через полминуты-минуту. Дальше его
        # подхватит poll_pending_cases.
        return _create(
            user,
            url,
            state=MonitoredCase.State.PENDING,
            core_task_id=answer.get("task_id"),
            core_case_id=answer.get("case_id"),
        )

    # Остальное — отказы core: суд не в справочнике, портал не поддержан,
    # адрес не разобран. У core есть готовый человеческий текст — показываем его.
    raise ValidationError(answer.get("message") or _default_refusal(status))


def _default_refusal(status: str | None) -> str:
    """Текст отказа, когда core не прислал свой message."""
    if status == core.STATUS_UNSUPPORTED_COURT:
        return "Этот суд пока не поддерживается."
    if status == core.STATUS_LINK_REQUIRED:
        return "По этому делу нужна прямая ссылка на карточку."
    return "Не удалось принять это дело."


def _create(user, url: str, **fields) -> MonitoredCase:
    """Создать запись, не падая на уникальных индексах.

    Сработать могут оба: (user, source_url) — если добавление пришло дважды
    одновременно, и (user, core_case_id) — если пользователь уже следит за этим
    делом по ДРУГОЙ ссылке (на одну карточку ведут http и https, разный порядок
    параметров, сменившийся адрес участка). В обоих случаях нужное дело у него
    уже есть — его и возвращаем.
    """
    try:
        with transaction.atomic():
            return MonitoredCase.objects.create(user=user, source_url=url, **fields)
    except IntegrityError:
        existing = MonitoredCase.objects.filter(user=user, source_url=url).first()
        if existing is not None:
            return existing
        case_id = fields.get("core_case_id")
        if case_id is not None:
            existing = MonitoredCase.objects.filter(user=user, core_case_id=case_id).first()
            if existing is not None:
                return existing
        raise


def list_monitored_cases(user):
    """Все дела пользователя. Рендерится из кэша витрины, без походов в core."""
    return MonitoredCase.objects.filter(user=user)


def get_monitored_case(user, pk: int) -> MonitoredCase:
    """Одно дело пользователя. Фильтр по user — это и проверка прав доступа."""
    return MonitoredCase.objects.get(user=user, pk=pk)


def remove_from_monitoring(user, pk: int) -> None:
    """Снять дело с мониторинга.

    В core мониторинг выключаем только если за делом больше никто не следит:
    таблица дел там общая, и выключив флаг, мы перестали бы обновлять дело всем.
    """
    monitored = get_monitored_case(user, pk)
    case_id = monitored.core_case_id
    monitored.delete()

    if case_id is None:
        return
    others = MonitoredCase.objects.filter(core_case_id=case_id).exists()
    if others:
        return
    try:
        core.set_monitoring(case_id, enabled=False)
    except core.CoreApiError as exc:
        # Не критично: дело просто продолжит обходиться вхолостую.
        logger.warning("Не удалось выключить мониторинг дела %s в core: %s", case_id, exc)


def refresh_from_core(monitored: MonitoredCase) -> MonitoredCase:
    """Подтянуть из core актуальное состояние дела.

    Для PENDING — узнать, чем кончилась задача разбора. Для ACTIVE — обновить
    витрину (статус и даты).
    """
    if monitored.state == MonitoredCase.State.PENDING:
        return _refresh_pending(monitored)
    if monitored.core_case_id is not None:
        return _refresh_summary(monitored)
    return monitored


def _refresh_pending(monitored: MonitoredCase) -> MonitoredCase:
    """Опросить задачу разбора и перевести дело в active/failed."""
    if monitored.core_task_id is None:
        return monitored

    try:
        task = core.get_search_task(monitored.core_task_id)
    except core.CoreApiError as exc:
        # Временная недоступность core — дело остаётся pending, попробуем в
        # следующий раз. В last_error не пишем: это не отказ по делу.
        logger.warning("Не удалось опросить задачу %s: %s", monitored.core_task_id, exc)
        return monitored

    status = task.get("status")

    if status == core.TASK_SUCCESS and task.get("case_id"):
        case_id = task["case_id"]

        # Ссылка привела к делу, за которым пользователь уже следит по другому
        # адресу. Дубль не заводим и не падаем на уникальном индексе — просто
        # убираем лишнюю запись, дело у него останется.
        duplicate = (
            MonitoredCase.objects.filter(user_id=monitored.user_id, core_case_id=case_id)
            .exclude(pk=monitored.pk)
            .first()
        )
        if duplicate is not None:
            monitored.delete()
            return duplicate

        monitored.core_case_id = case_id
        monitored.state = MonitoredCase.State.ACTIVE
        monitored.last_error = ""
        monitored.save(update_fields=["core_case_id", "state", "last_error", "updated_at"])
        _enable_monitoring_in_core(monitored)
        return _refresh_summary(monitored)

    if status == core.TASK_FAILED:
        monitored.state = MonitoredCase.State.FAILED
        monitored.last_error = task.get("last_error") or "core не смог получить дело"
        monitored.save(update_fields=["state", "last_error", "updated_at"])
        return monitored

    # pending/running — просто ждём дальше.
    return monitored


def _refresh_summary(monitored: MonitoredCase) -> MonitoredCase:
    """Обновить кэш витрины из лёгкой сводки по делу."""
    try:
        summary = core.get_case_summary(monitored.core_case_id)
    except core.CoreApiError as exc:
        logger.warning("Не удалось получить сводку дела %s: %s", monitored.core_case_id, exc)
        return monitored

    monitored.core_status = summary.get("status") or ""
    monitored.core_changed_at = _parse_dt(summary.get("last_changed_at"))
    monitored.core_last_checked_at = _parse_dt(summary.get("last_checked_at"))
    monitored.save(
        update_fields=[
            "core_status",
            "core_changed_at",
            "core_last_checked_at",
            "updated_at",
        ]
    )
    return monitored


def _enable_monitoring_in_core(monitored: MonitoredCase) -> None:
    """Сказать core, что это дело надо переобходить по расписанию."""
    if monitored.core_case_id is None:
        return
    try:
        core.set_monitoring(monitored.core_case_id, enabled=True)
    except core.CoreApiError as exc:
        # Дело у пользователя останется, но обновляться не будет — это видно по
        # пустой дате последней проверки, и следующий poll попробует снова.
        logger.warning(
            "Не удалось включить мониторинг дела %s в core: %s", monitored.core_case_id, exc
        )


def _parse_dt(value: str | None) -> dt.datetime | None:
    """Разобрать дату из ответа core.

    core пишет времена через datetime.utcnow() — без таймзоны. Django с USE_TZ=True
    такие значения принимать откажется, поэтому наивные считаем UTC (чем они и
    являются) и делаем aware.
    """
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("core прислал неразбираемую дату: %r", value)
        return None
    if timezone.is_naive(parsed):
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed
