"""Синхронизация списка дел на мониторинге и развязка ожиданий поиска.

Обе операции нужны из двух мест — из view (сразу после действия пользователя) и из
management-команды (сверка и подбор хвостов), — поэтому живут здесь, а не в views.py.
"""
import logging

from django.db import transaction
from django.utils import timezone

from cases.integrations import core
from cases.models import CaseSubscription, PendingCaseSearch

logger = logging.getLogger(__name__)


def monitored_case_ids() -> list[int]:
    """Дела, за которыми следит хотя бы один пользователь.

    distinct обязателен, и это не оптимизация запроса. На дело с тремя подписчиками core
    должен сходить ОДИН раз: поход стоит прокси и оплаченной капчи, и он не должен
    множиться на число заинтересованных. Без distinct мы бы прислали id трижды — core, к
    счастью, склеит их сам, но полагаться на это значит держать защиту в чужом сервисе.
    """
    return sorted(
        CaseSubscription.objects.filter(is_active=True)
        .values_list("core_case_id", flat=True)
        .distinct()
    )


def sync_monitoring(force_empty: bool = False) -> dict | None:
    """Отправить core полный список дел на мониторинге.

    Возвращает ответ core либо None, если до него не дозвонились.

    **Отказ core не роняет вызывающего.** Подписка пользователя уже записана в нашей базе,
    и терять её из-за недоступности соседнего сервиса нельзя: человек нажал кнопку, дело у
    него в списке, а расхождение снимет следующая синхронизация. Поэтому исключение здесь
    проглатывается — но не молча, а с WARNING: «дела не обновляются» начинают разбирать
    именно с этой строки в логе.

    force_empty пробрасывается в core только когда пустой список — это правда. Обычный
    вызов пустого списка не форсирует: core отклонит его с 409, и это ровно то поведение,
    которое нужно, если у нас поехал queryset.
    """
    case_ids = monitored_case_ids()
    try:
        result = core.replace_monitored_cases(case_ids, force=force_empty and not case_ids)
    except core.MonitoringRefused as exc:
        # core не поверил пустому списку — и правильно сделал. Это срабатывание защиты от
        # НАШЕЙ аварии: подписки есть, а queryset пришёл пустым. Логируем как ошибку, а не
        # предупреждение: тут действительно надо разбираться, и разбираться у нас.
        logger.error(
            "core отклонил список мониторинга: %s. Проверьте, почему список дел пуст, "
            "если подписки существуют", exc,
        )
        return None
    except core.CoreUnavailable as exc:
        logger.warning(
            "Список мониторинга (%s дел) не отправлен в core: %s. "
            "Расхождение снимет следующая синхронизация",
            len(case_ids), exc,
        )
        return None

    logger.info(
        "Мониторинг синхронизирован: на обходе %s, добавлено %s, снято %s",
        result.get("monitored"), result.get("added"), result.get("removed"),
    )
    unknown = result.get("unknown_ids") or []
    if unknown:
        # Подписка есть, дела в core нет. Молчать нельзя: пользователь ждёт обновлений по
        # делу, которое никто не обходит.
        logger.warning("Подписки на дела, которых нет в core: %s", unknown)
    return result


def resolve_pending(pending: PendingCaseSearch) -> PendingCaseSearch:
    """Спросить core, чем закончился поиск, и довести подписку до конца.

    Идемпотентна: уже развязанное ожидание второй раз не трогается. Это важно, потому что
    зовут её оба пути — страница ожидания и `resolve_pending_searches`, — и они легко
    сработают одновременно.

    Недоступность core — не развязка: оставляем ожидание висеть и пробуем позже. Пометить
    его провалившимся из-за нашего же таймаута значило бы соврать пользователю.
    """
    if not pending.is_pending:
        return pending

    try:
        task = core.get_search_task(pending.core_task_id)
    except core.CoreUnavailable as exc:
        logger.warning("Не удалось узнать судьбу задачи %s: %s", pending.core_task_id, exc)
        return pending

    status = task.get("status")
    if status in ("pending", "running"):
        return pending  # ещё ищем, это нормальное состояние

    if status == "failed":
        pending.last_error = (task.get("last_error") or "")[:500]
        pending.resolved_at = timezone.now()
        pending.save(update_fields=["last_error", "resolved_at"])
        logger.info(
            "Поиск по задаче %s провалился: %s", pending.core_task_id, pending.last_error
        )
        return pending

    if status != "success":
        # Незнакомый статус — не наша забота гадать. Ждём: пусть лучше висит, чем мы
        # объявим успехом то, чего не понимаем.
        logger.warning(
            "Задача %s в неизвестном статусе %r", pending.core_task_id, status
        )
        return pending

    case_id = task.get("case_id")
    if case_id is None:
        # success без дела — противоречие в ответе core. Разбираться с этим руками.
        logger.warning("Задача %s успешна, но дела в ответе нет", pending.core_task_id)
        return pending

    subscribe(pending.user, [case_id])
    pending.resolved_at = timezone.now()
    pending.save(update_fields=["resolved_at"])
    logger.info(
        "Поиск по задаче %s завершён: подписка на дело %s", pending.core_task_id, case_id
    )
    return pending


def subscribe(user, case_ids) -> list[CaseSubscription]:
    """Подписать пользователя на дела и синхронизировать мониторинг.

    Повторная подписка на то же дело не создаёт дубль и не роняет запрос: UNIQUE
    `(user, core_case_id)` не даст, а `update_or_create` вернёт существующую строку и
    заодно поднимет `is_active` — так «подписался снова после отписки» работает само.

    Синхронизация мониторинга — ПОСЛЕ коммита, и это существенно. Позови мы core внутри
    транзакции, он получил бы список без только что созданной подписки (её ещё не видно
    другим соединениям), и дело не встало бы на обход до следующей синхронизации.
    """
    subscriptions = []
    with transaction.atomic():
        for case_id in case_ids:
            subscription, _ = CaseSubscription.objects.update_or_create(
                user=user, core_case_id=case_id, defaults={"is_active": True}
            )
            subscriptions.append(subscription)

    sync_monitoring()
    return subscriptions


def unsubscribe(subscription: CaseSubscription) -> None:
    """Отписать и синхронизировать мониторинг.

    Строку не удаляем, а гасим флагом: так видно, что пользователь когда-то следил за
    делом. Для core разницы нет — он получит список без этого дела и снимет его с обхода,
    если больше никто не следит.
    """
    if subscription.is_active:
        subscription.is_active = False
        subscription.save(update_fields=["is_active"])

    # force_empty: пользователь отписался от последнего дела, и пустой список — правда, а
    # не наша авария. Без этого core отклонил бы его с 409, и последнее дело осталось бы
    # на обходе навсегда.
    sync_monitoring(force_empty=True)
