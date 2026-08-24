"""Раскладка результата сверки (CaseChanges) в поток domain events об изменениях.

    ParsedCase -> sync_case -> CaseChanges -> changes_to_events -> DomainEvent
                                                                    │
                                                     ┌──────────────┴──────────────┐
                                                     ▼                             ▼
                                              OutboxEvent              IntegrationOutboxEvent
                                              домен-лог, богатый       публичный контракт,
                                              payload, чтение по HTTP  скудный, уезжает в
                                                                       RabbitMQ

Каждое атомарное изменение — отдельный DomainEvent со своим типом и компактным payload.
Раньше весь дифф уходил одним куском в JSONB-колонку Case.diff_history: оттуда нельзя было
дёшево выбрать «что изменилось у дела после такого-то момента», а строка дела
переписывалась на каждом обходе.

## DomainEvent, а не кортеж

DomainEvent несёт не только тип и payload, но и ССЫЛКУ на изменившуюся строку (entity).
Она нужна одному потребителю — сборке integration event, которому нужен id сущности
(«новое событие по делу, id 712»). В payload его нет и быть не должно: там лежит uid,
детерминированный ключ строки, а не её номер в нашей базе, и добавить туда id значило бы
поменять публичный формат GET /cases/{id}/events.

ВАЖНО про порядок: у только что созданных строк id появляется только после flush. Значит,
entity.id читать можно лишь ПОСЛЕ OutboxEventRepository.emit (он флашит) — см.
app/integration_events.py. До флаша там будет None, и молча: ошибки не случится, просто
у всех новых событий entity_id окажется пустым.

## Это НЕ уведомления

Строка outbox_event означает ровно одно: **core обнаружил, что на портале что-то
изменилось.** Она не значит, что кому-то надо что-то отправить: core не знает ни
пользователей, ни подписок, ни каналов доставки. Поэтому в payload нет и не должно быть
ни user_id, ни sent, ни delivered — кому и как сообщать, решает тот, кто эти события
читает.

## Главное свойство: атомарность

changes_to_events вызывается сразу после sync_case и ДО коммита, а запись событий идёт
в ТОЙ ЖЕ транзакции, что и само изменение карточки (см. OutboxEventRepository.emit).
Отсюда два следствия, ради которых всё и сделано:

* событие не может потеряться — оно коммитится вместе с изменением;
* событие не может появиться без изменения — откат уносит и то и другое.

Есть и техническая причина звать это до коммита: у удалённых событий и местонахождений
атрибуты в этот момент ещё загружены в сессию, а после коммита читать их было бы уже
нечем.
"""
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from app.models import (
    CourtSession,
    Document,
    Event,
    Judge,
    OutboxEventType,
    PlaceHistory,
    Side,
)
from app.repositories import CaseFieldChange
from app.services.case_sync import CaseChanges


@dataclass(frozen=True)
class DomainEvent:
    """Одно атомарное изменение по делу, обнаруженное сверкой.

    Факт внутри backend, а не запись в БД: из него делаются и строка домен-лога
    (outbox_event), и публичный integration event. Сам он ничего о доставке не знает.

    entity — изменившаяся строка (Event, CourtSession, Document, PlaceHistory, Judge,
    Side) либо None у изменения скалярного поля дела: у «status стал другим» отдельной
    сущности нет. Держим сам объект, а не его id, потому что у новых строк id появляется
    только после flush, а DomainEvent собирается до него.
    """

    event_type: OutboxEventType
    payload: dict
    entity: Optional[Any] = None


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


def changes_to_events(changes: CaseChanges) -> list[DomainEvent]:
    """Разложить результат сверки на плоский список domain events.

    **У НОВОЙ карточки возвращает пустой список, и это не оптимизация.** Первый обход —
    baseline: вся карточка на нём формально «новая», в ней десятки строк истории
    состояний, заседания и документы. Но это не изменения — это то, что на портале было
    и до нас. Выпустить по ним события значило бы сказать «вот что поменялось» про то,
    что не менялось.

    Появление самой карточки читающий видит и без outbox: у него есть id дела.

    Вызывать сразу после sync_case и ДО коммита — см. докстринг модуля.
    """
    if changes.is_new:
        return []

    T = OutboxEventType
    events: list[DomainEvent] = []

    # У изменения скалярного поля дела сущности нет: «status стал другим» — это про саму
    # карточку, и её id integration event несёт отдельным полем.
    for field_change in changes.field_changes:
        events.append(
            DomainEvent(T.CASE_FIELD_CHANGED, _field_change_to_dict(field_change))
        )

    # Порядок веток — тот же, что у самой сверки в sync_case: поля дела, события,
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
            events.append(DomainEvent(event_type, to_dict(item), entity=item))

    return events
