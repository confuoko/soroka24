"""Outbox: изменение карточки и факт события пишутся одной транзакцией.

Сценарии из ТЗ §26 «Outbox»: первый импорт не создаёт события, последующее изменение
создаёт, откат сверки не оставляет строк, события читаются последовательно.

Отдельный смысл здесь у теста test_rollback_leaves_no_events: он проверяет не удобство,
а то единственное свойство, ради которого outbox вообще устроен именно так. Если запись
события уедет из транзакции изменения, событие сможет появиться без изменения (или
изменение без события), и читающий сервис начнёт врать.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select

from app.models import OutboxEvent, OutboxEventType
from app.outbox import changes_to_events
from app.parsers import ParsedCase, ParsedEvent, ParsedSide
from app.repositories import OutboxEventRepository
from app.services import sync_case

pytestmark = pytest.mark.db

UID = "77MS0002-01-2026-004321-11"
CODE = "02-0777/2026"


def page(**overrides) -> ParsedCase:
    """Разбор страницы: минимальная карточка с одним событием и одной стороной."""
    parsed = ParsedCase(
        status="Рассмотрение",
        category="Гражданские дела",
        judge_names=["Иванов И.И."],
        sides=[ParsedSide(role="Истец", full_name="Петров П.П.")],
        events=[
            ParsedEvent(
                event_date=dt.datetime(2026, 8, 10, 10, 0),
                state_description="Регистрация",
            )
        ],
    )
    for name, value in overrides.items():
        setattr(parsed, name, value)
    return parsed


def sync(session, court, parsed: ParsedCase):
    return sync_case(session, UID, parsed, court, CODE)


def stored(session, case_id: int) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.case_id == case_id)
            .order_by(OutboxEvent.id)
        )
    )


# ------------------------------------------------------------------ baseline
def test_first_import_produces_no_events(session, court) -> None:
    """Первый обход не создаёт ни одного события об изменении.

    Вся карточка на нём формально «новая» — десятки строк истории, заседания, документы.
    Но это не изменения: так на портале было и до нас. Появление самого дела читающий
    видит и без outbox, у него есть id.
    """
    changes = sync(session, court, page())

    assert changes.is_new is True
    assert changes_to_events(changes) == []


def test_baseline_writes_nothing_to_the_table(session, court) -> None:
    """И в таблице после первого обхода тоже ничего нет."""
    changes = sync(session, court, page())
    OutboxEventRepository(session).emit(changes.case, changes_to_events(changes))
    session.flush()

    assert stored(session, changes.case.id) == []


# ------------------------------------------------------- последующие изменения
def test_field_change_produces_an_event(session, court) -> None:
    changes = sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено"))
    events = changes_to_events(changes)

    assert [event.event_type for event in events] == [OutboxEventType.CASE_FIELD_CHANGED]
    assert events[0].payload == {
        "field": "status",
        "old": "Рассмотрение",
        "new": "Рассмотрено",
    }
    # У изменения скалярного поля сущности нет: поменялась сама карточка.
    assert events[0].entity is None


def test_new_event_on_the_page_produces_an_event(session, court) -> None:
    changes = sync(session, court, page())
    session.flush()

    grown = page()
    grown.events = grown.events + [
        ParsedEvent(
            event_date=dt.datetime(2026, 8, 15, 11, 0),
            state_description="Судебное заседание",
        )
    ]
    events = changes_to_events(sync(session, court, grown))

    assert [event.event_type for event in events] == [OutboxEventType.EVENT_NEW]
    # Ссылка на саму новую строку — из неё integration event берёт entity_id.
    assert events[0].entity is not None


def test_removed_event_produces_an_event(session, court) -> None:
    changes = sync(session, court, page())
    session.flush()

    events = changes_to_events(sync(session, court, page(events=[])))

    assert [event.event_type for event in events] == [OutboxEventType.EVENT_REMOVED]


def test_side_reconciliation_produces_two_events(session, court) -> None:
    """Смена роли стороны — это отвязка одной и привязка другой.

    Ключ стороны — (ФИО, роль), поэтому та же фамилия с другой ролью это ДРУГАЯ сторона.
    """
    sync(session, court, page())
    session.flush()

    changed = page(sides=[ParsedSide(role="Ответчик", full_name="Петров П.П.")])
    types = {event.event_type for event in changes_to_events(sync(session, court, changed))}

    assert types == {OutboxEventType.SIDE_ADDED, OutboxEventType.SIDE_REMOVED}


def test_idle_crawl_produces_nothing(session, court) -> None:
    """Обход неизменившейся страницы не даёт ни одного события."""
    sync(session, court, page())
    session.flush()

    assert changes_to_events(sync(session, court, page())) == []


# --------------------------------------------------------------- атомарность
def test_change_and_event_are_written_together(session, court) -> None:
    """Событие лежит в базе вместе с изменением, которое его вызвало."""
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено"))
    OutboxEventRepository(session).emit(changes.case, changes_to_events(changes))
    session.flush()

    rows = stored(session, changes.case.id)
    assert len(rows) == 1
    assert rows[0].event_type is OutboxEventType.CASE_FIELD_CHANGED
    assert changes.case.status == "Рассмотрено"


def test_rollback_leaves_no_events(session, court) -> None:
    """ГЛАВНОЕ СВОЙСТВО. Откат уносит и изменение, и событие.

    Проверяем вложенной транзакцией (SAVEPOINT): внешнюю откатывает фикстура, а увидеть
    надо откат именно нашей записи, не выходя из тестовой сессии.

    Если запись события уедет из транзакции изменения, сломается ровно это: событие
    сможет появиться без изменения, и читающий сервис сообщит о том, чего не было.
    """
    sync(session, court, page())
    session.flush()
    changes = sync(session, court, page(status="Рассмотрено"))
    case_id = changes.case.id

    savepoint = session.begin_nested()
    OutboxEventRepository(session).emit(changes.case, changes_to_events(changes))
    session.flush()
    assert stored(session, case_id), "событие должно было появиться до откáта"
    savepoint.rollback()

    assert stored(session, case_id) == []


# ------------------------------------------------------------------- чтение
def test_events_are_read_in_the_order_they_were_found(session, court) -> None:
    """Порядок чтения — порядок обнаружения.

    Метка created_at берётся из func.now(), то есть из момента НАЧАЛА транзакции, и у
    всех событий одного обхода она одинаковая. Поэтому сортировка идёт по (created_at, id)
    — без id взаимный порядок был бы неопределён.
    """
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено", category="Другая"))
    repo = OutboxEventRepository(session)
    repo.emit(changes.case, changes_to_events(changes))
    session.flush()

    rows = repo.list_since(changes.case.id, dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc))

    assert len(rows) == 2
    assert [row.id for row in rows] == sorted(row.id for row in rows)


def test_reading_from_the_last_moment_returns_nothing(session, court) -> None:
    """Сравнение строгое: с моментом последнего события те же события не придут снова."""
    sync(session, court, page())
    session.flush()
    changes = sync(session, court, page(status="Рассмотрено"))
    repo = OutboxEventRepository(session)
    repo.emit(changes.case, changes_to_events(changes))
    session.flush()

    everything = repo.list_since(
        changes.case.id, dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    )
    last_moment = everything[-1].created_at

    assert repo.list_since(changes.case.id, last_moment) == []


def test_limit_is_respected(session, court) -> None:
    sync(session, court, page())
    session.flush()
    changes = sync(session, court, page(status="Рассмотрено", category="Другая"))
    repo = OutboxEventRepository(session)
    repo.emit(changes.case, changes_to_events(changes))
    session.flush()

    rows = repo.list_since(
        changes.case.id, dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc), limit=1
    )

    assert len(rows) == 1


def test_events_of_another_case_are_not_returned(session, court) -> None:
    """События отбираются по делу: чужие не приходят."""
    sync(session, court, page())
    session.flush()
    changes = sync(session, court, page(status="Рассмотрено"))
    repo = OutboxEventRepository(session)
    repo.emit(changes.case, changes_to_events(changes))
    session.flush()

    assert repo.list_since(
        changes.case.id + 10_000, dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    ) == []


# ------------------------------------------------------ payload это не уведомление
def test_payload_has_no_delivery_fields(session, court) -> None:
    """Core не знает получателей: ни user_id, ни sent, ни delivered (ТЗ PRIORITY 22)."""
    sync(session, court, page())
    session.flush()
    changed = page(status="Рассмотрено", events=[], sides=[], judge_names=[])

    forbidden = {"user_id", "sent", "delivered", "notification_status", "email", "telegram_id"}
    for event in changes_to_events(sync(session, court, changed)):
        assert not (forbidden & set(event.payload)), event.payload


def test_payload_is_json_serialisable(session, court) -> None:
    """Payload уходит в JSONB — значит ни date, ни datetime, ни UUID в нём быть не может.

    Все они приводятся к строкам в app/outbox.py. Проверяем настоящим json.dumps: именно
    на нём споткнулась бы забытая дата.
    """
    sync(session, court, page())
    session.flush()

    grown = page(status="Рассмотрено")
    grown.events = grown.events + [
        ParsedEvent(
            event_date=dt.datetime(2026, 8, 15, 11, 0),
            state_description="Судебное заседание",
            published_at=dt.date(2026, 8, 16),
        )
    ]

    events = changes_to_events(sync(session, court, grown))
    assert events
    for event in events:
        json.dumps(event.payload, ensure_ascii=False)  # упадёт, если внутри осталась дата
