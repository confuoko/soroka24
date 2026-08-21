"""Раскладка результата сверки (CaseChanges) в поток доменных событий (OutboxEvent).

Раньше весь дифф уходил одним куском в JSONB-колонку Case.diff_history: оттуда нельзя
было дёшево выбрать «что изменилось у дела после такого-то момента», а строка дела
переписывалась на каждом обходе.

Теперь каждое атомарное изменение — отдельная строка outbox_event со своим типом,
временем обнаружения и компактным payload'ом. Клиентский сервис отбирает их по
created_at, начиная с момента, когда пользователь поставил дело на мониторинг.
"""
from datetime import date
from typing import Optional

from app.models.database import (
    CourtSession,
    Document,
    Event,
    Judge,
    OutboxEventType,
    PlaceHistory,
    Side,
)
from app.monitoring.case_update import CaseChanges
from app.repositories import CaseFieldChange


def _iso(value: Optional[date]) -> Optional[str]:
    """Дату или момент в ISO-строку (None остаётся None) — JSON не умеет ни то, ни другое.

    Годится для обоих: datetime — подкласс date, и у момента isoformat() отдаёт время со
    смещением, а у календарной даты — просто «2026-08-19».
    """
    return value.isoformat() if value is not None else None


def _event_to_dict(event: Event) -> dict:
    """Событие «Истории состояний» в компактный вид."""
    return {
        "uid": str(event.uid),
        "event_date": _iso(event.event_date),
        "state_description": event.state_description,
        "document_str": event.document_str,
        "published_at": _iso(event.published_at),
    }


def _place_to_dict(place: PlaceHistory) -> dict:
    """Строку истории местонахождения — в компактный вид."""
    return {
        "uid": str(place.uid),
        "place_date": _iso(place.place_date),
        "place_description": place.place_description,
        "comment": place.comment,
    }


def _session_to_dict(session: CourtSession) -> dict:
    """Судебное заседание — в компактный вид.

    session_date — момент со смещением: время входит в identity заседания и пользователю
    важно не меньше даты («заседание 14.08 в 10:00»). Смещение обязательно, иначе клиент
    не отличит московское заседание от владивостокского.
    """
    return {
        "uid": str(session.uid),
        "session_date": _iso(session.session_date),
        "place": session.place,
        "stage": session.stage,
        "result": session.result,
        "basis": session.basis,
    }


def _document_to_dict(document: Document) -> dict:
    """Документ — в компактный вид.

    Только метаданные: ни текста документа, ни ссылки на файл мы не храним.
    """
    return {
        "uid": str(document.uid),
        "document_date": _iso(document.document_date),
        "document_type": document.document_type,
    }


def _field_change_to_dict(change: CaseFieldChange) -> dict:
    """Изменение поля дела — в компактный вид.

    Даты приводим к ISO: JSON не умеет date, а в полях дела их большинство.
    """
    return {
        "field": change.field,
        "old": _iso(change.old) if isinstance(change.old, date) else change.old,
        "new": _iso(change.new) if isinstance(change.new, date) else change.new,
    }


def _judge_to_dict(judge: Judge) -> dict:
    return {"id": judge.id, "full_name": judge.full_name}


def _side_to_dict(side: Side) -> dict:
    return {"id": side.id, "full_name": side.full_name, "type": side.type.value}


def changes_to_events(changes: CaseChanges) -> list[tuple[OutboxEventType, dict]]:
    """Разложить результат сверки на плоский список событий (тип, payload).

    У НОВОЙ карточки возвращает пустой список. Первый обход — baseline: вся карточка на
    нём формально «новая» (десятки строк истории состояний, заседания, документы), а
    пользователь ставит дело на мониторинг ровно в этот момент — значит всё это попало бы
    ему в уведомления. Появление самой карточки клиент видит и без outbox.

    Вызывать сразу после update_case(), до коммита: у удалённых событий и
    местонахождений атрибуты в этот момент ещё загружены в сессии.
    """
    if changes.is_new:
        return []

    T = OutboxEventType
    events: list[tuple[OutboxEventType, dict]] = []

    for field_change in changes.field_changes:
        events.append((T.CASE_FIELD_CHANGED, _field_change_to_dict(field_change)))

    # Порядок веток — тот же, что у самой сверки в update_case: поля дела, события,
    # местонахождения, заседания, документы, судьи, стороны.
    for event_type, items, to_dict in (
        (T.EVENT_NEW, changes.new_events, _event_to_dict),
        (T.EVENT_UPDATED, changes.updated_events, _event_to_dict),
        (T.EVENT_REMOVED, changes.removed_events, _event_to_dict),
        (T.PLACE_NEW, changes.new_places, _place_to_dict),
        (T.PLACE_UPDATED, changes.updated_places, _place_to_dict),
        (T.PLACE_REMOVED, changes.removed_places, _place_to_dict),
        (T.SESSION_NEW, changes.new_sessions, _session_to_dict),
        (T.SESSION_UPDATED, changes.updated_sessions, _session_to_dict),
        (T.SESSION_REMOVED, changes.removed_sessions, _session_to_dict),
        # У документов нет ветки updated: изменяемых полей у них не осталось.
        (T.DOCUMENT_NEW, changes.new_documents, _document_to_dict),
        (T.DOCUMENT_REMOVED, changes.removed_documents, _document_to_dict),
        (T.JUDGE_ADDED, changes.added_judges, _judge_to_dict),
        (T.JUDGE_REMOVED, changes.removed_judges, _judge_to_dict),
        (T.SIDE_ADDED, changes.added_sides, _side_to_dict),
        (T.SIDE_REMOVED, changes.removed_sides, _side_to_dict),
    ):
        for item in items:
            events.append((event_type, to_dict(item)))

    return events
