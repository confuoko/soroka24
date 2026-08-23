"""Characterization: единая операция сверки (update_case) и транзакционный outbox.

Это ядро, которое ТЗ требует перенести с сохранением поведения (PRIORITY 3, 19, 20).
Здесь фиксируется:

* первый импорт — что считается baseline;
* повторная сверка без изменений — нулевой diff;
* new / updated / removed по каждой дочерней сущности;
* реконсиляция судей и сторон;
* атомарность outbox: откат транзакции не оставляет событий;
* последовательность чтения outbox.

Тестам нужен настоящий PostgreSQL — они помечены маркером `db`.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.models.database import OutboxEvent, OutboxEventType
from app.monitoring.case_update import update_case
from app.monitoring.outbox import changes_to_events
from app.repositories.outbox_events import OutboxEventRepository

pytestmark = pytest.mark.db

CODE = "02-0123/2026"
UID = "77MS0002-01-2026-001579-64"
URL = "https://mos-sud.ru/2/services/cases/details/char-test-1"


def page(**overrides) -> dict:
    """Разбор страницы: минимальная карточка со всеми видами дочерних строк.

    Ключи перечислены явно и полностью — набор ключей сам является контрактом (риск R1),
    поэтому подмешивать их динамически нельзя.
    """
    data = {
        "status": "Рассмотрение",
        "category": "Гражданские дела",
        "receipt_date": dt.date(2026, 8, 1),
        "judge_names": ["Иванов И.И."],
        "sides": [{"role": "Истец", "full_name": "Петров П.П."}],
        "events": [
            {
                "event_date": dt.datetime(2026, 8, 10, 10, 0),
                "state_description": "Регистрация",
                "document_str": None,
                "published_at": None,
            },
        ],
        "place_history": [
            {
                "place_date": dt.date(2026, 8, 10),
                "place_description": "Судебный участок",
                "comment": None,
            },
        ],
        "court_sessions": [
            {
                "session_date": dt.datetime(2026, 8, 20, 15, 30),
                "place": "зал 1",
                "stage": "Первая инстанция",
                "result": None,
                "basis": None,
            },
        ],
        "documents": [
            {"document_date": dt.date(2026, 8, 21), "document_type": "Решение"},
        ],
        "url": URL,
    }
    data.update(overrides)
    return data


def sync(session, court, data: dict):
    return update_case(session, UID, data, court, CODE)


# ------------------------------------------------------------------ первый импорт
def test_first_import_reports_everything_as_new(session, court) -> None:
    changes = sync(session, court, page())

    assert changes.is_new is True
    assert len(changes.new_events) == 1
    assert len(changes.new_places) == 1
    assert len(changes.new_sessions) == 1
    assert len(changes.new_documents) == 1
    assert len(changes.added_judges) == 1
    assert len(changes.added_sides) == 1
    # У новой карточки отдельных изменений полей не бывает: сравнивать не с чем.
    assert changes.field_changes == []


def test_first_import_produces_no_outbox_events(session, court) -> None:
    """Baseline не порождает событий (ТЗ PRIORITY 20, outbox.py:117-118).

    Иначе на первом же обходе пользователь получил бы десятки уведомлений о строках,
    которые просто впервые попали в нашу БД.
    """
    changes = sync(session, court, page())
    assert changes_to_events(changes) == []


def test_case_fields_are_saved_on_first_import(session, court) -> None:
    changes = sync(session, court, page())
    case = changes.case
    assert case.status == "Рассмотрение"
    assert case.category == "Гражданские дела"
    assert case.receipt_date == dt.date(2026, 8, 1)
    assert case.card_key == f"{UID}|{court.code}|{CODE}"


def test_calendar_date_stays_a_date(session, court) -> None:
    """Дата поступления обязана остаться date, а не стать полночью (ТЗ PRIORITY 25)."""
    case = sync(session, court, page()).case
    assert isinstance(case.receipt_date, dt.date)
    assert not isinstance(case.receipt_date, dt.datetime)


def test_local_datetime_is_stored_as_utc(session, court) -> None:
    """Парсер отдал наивное локальное время — в базе лежит UTC-aware (риск R3)."""
    changes = sync(session, court, page())
    event = changes.new_events[0]
    assert event.event_date.tzinfo is not None
    assert event.event_date.utcoffset() == dt.timedelta(0)
    # 10:00 по Москве — это 07:00 UTC.
    assert event.event_date.hour == 7


# ------------------------------------------------------ повторная сверка без изменений
def test_second_sync_without_changes_reports_nothing(session, court) -> None:
    """Ключевое свойство: обход неизменившейся страницы даёт нулевой diff."""
    sync(session, court, page())
    session.flush()

    again = sync(session, court, page())

    assert again.is_new is False
    assert again.has_changes() is False
    assert again.field_changes == []
    assert again.new_events == [] and again.updated_events == []
    assert again.removed_events == []
    assert again.new_documents == [] and again.removed_documents == []
    assert again.added_judges == [] and again.removed_judges == []
    assert again.added_sides == [] and again.removed_sides == []
    assert changes_to_events(again) == []


def test_repeated_sync_keeps_child_uids(session, court) -> None:
    """uid дочерних строк детерминированы — повторный обход их не переписывает."""
    first = sync(session, court, page())
    first_uid = first.new_events[0].uid
    session.flush()

    sync(session, court, page())
    session.flush()

    stored = first.case.events
    assert len(stored) == 1
    assert stored[0].uid == first_uid


# ------------------------------------------------------------------------ new / updated
def test_new_event_is_detected(session, court) -> None:
    sync(session, court, page())
    session.flush()

    grown = page()
    grown["events"] = grown["events"] + [
        {
            "event_date": dt.datetime(2026, 8, 15, 11, 0),
            "state_description": "Судебное заседание",
            "document_str": None,
            "published_at": None,
        },
    ]
    changes = sync(session, court, grown)

    assert len(changes.new_events) == 1
    assert changes.new_events[0].state_description == "Судебное заседание"
    assert changes.removed_events == []

    types = [event_type for event_type, _ in changes_to_events(changes)]
    assert OutboxEventType.EVENT_NEW in types


def test_event_time_filled_in_later_is_an_update_not_a_new_row(session, court) -> None:
    """Дозаполнение времени = UPDATE: в identity события входит только дата (риск R3)."""
    sync(session, court, page())
    session.flush()

    later = page()
    later["events"][0]["event_date"] = dt.datetime(2026, 8, 10, 16, 45)
    changes = sync(session, court, later)

    assert changes.new_events == []
    assert changes.removed_events == []
    assert len(changes.updated_events) == 1


def test_removed_event_is_detected(session, court) -> None:
    sync(session, court, page())
    session.flush()

    shrunk = page()
    shrunk["events"] = []
    changes = sync(session, court, shrunk)

    assert len(changes.removed_events) == 1
    assert changes.new_events == []

    types = [event_type for event_type, _ in changes_to_events(changes)]
    assert OutboxEventType.EVENT_REMOVED in types


def test_case_field_change_is_reported_with_old_and_new(session, court) -> None:
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено"))

    assert len(changes.field_changes) == 1
    change = changes.field_changes[0]
    assert (change.field, change.old, change.new) == (
        "status",
        "Рассмотрение",
        "Рассмотрено",
    )


def test_absent_key_leaves_the_column_alone(session, court) -> None:
    """Риск R1, ядро вопроса: отсутствующий ключ ≠ значение None.

    Первый обход записал категорию. На втором ключа `category` в разборе нет вовсе —
    так ведут себя парсеры, у которых такого поля не бывает. Колонка обязана остаться
    прежней. Если бы вместо отсутствия пришёл None, категория обнулилась бы — и это
    правильно, потому что означало бы «метка со страницы исчезла».
    """
    first = sync(session, court, page())
    session.flush()
    assert first.case.category == "Гражданские дела"

    without_key = page()
    del without_key["category"]
    changes = sync(session, court, without_key)

    assert changes.case.category == "Гражданские дела"
    assert [c.field for c in changes.field_changes] == []


def test_present_none_nulls_the_column(session, court) -> None:
    """Обратная половина R1: ключ есть, значение None — метка со страницы исчезла."""
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(category=None))

    assert changes.case.category is None
    assert [c.field for c in changes.field_changes] == ["category"]


# ------------------------------------------------------------- судьи и стороны
def test_judge_and_side_reconciliation(session, court) -> None:
    sync(session, court, page())
    session.flush()

    changed = page(
        judge_names=["Сидоров С.С."],
        sides=[{"role": "Ответчик", "full_name": "Петров П.П."}],
    )
    changes = sync(session, court, changed)

    assert [j.full_name for j in changes.added_judges] == ["Сидоров С.С."]
    assert [j.full_name for j in changes.removed_judges] == ["Иванов И.И."]
    # Ключ стороны — (ФИО, роль): та же фамилия с другой ролью это ДРУГАЯ сторона.
    assert [s.role for s in changes.added_sides] == ["Ответчик"]
    assert [s.role for s in changes.removed_sides] == ["Истец"]


# ------------------------------------------------- порядок документов (риск R2)
def test_identical_document_rows_get_distinct_uids_by_position(session, court) -> None:
    """21 одинаковая строка «Приложение» — 21 разная строка в базе.

    Различает их только номер повторения, а он считается по позиции на странице.
    Поэтому порядок строк, который отдал парсер, менять нельзя.
    """
    many = page()
    many["documents"] = [
        {"document_date": dt.date(2026, 8, 21), "document_type": "Приложение"}
        for _ in range(21)
    ]
    changes = sync(session, court, many)

    assert len(changes.new_documents) == 21
    assert len({d.uid for d in changes.new_documents}) == 21


def test_duplicate_event_rows_collapse_into_one(session, court) -> None:
    """Дубль со страницы гасится: повторная вставка того же uid уронила бы транзакцию."""
    doubled = page()
    doubled["events"] = doubled["events"] * 2
    changes = sync(session, court, doubled)

    assert len(changes.new_events) == 1


# --------------------------------------------------------------------- outbox
def test_outbox_is_written_in_the_same_transaction(session, court) -> None:
    """Изменение судебных данных и OutboxEvent пишутся одной транзакцией."""
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено"))
    events = changes_to_events(changes)
    assert events

    OutboxEventRepository(session).emit(changes.case, events)
    session.flush()

    stored = session.scalars(
        select(OutboxEvent).where(OutboxEvent.case_id == changes.case.id)
    ).all()
    assert len(stored) == len(events)


def test_rollback_leaves_no_outbox_events(session, court) -> None:
    """Откат сверки не оставляет событий — свойство Transactional Outbox.

    Проверяем вложенной транзакцией (SAVEPOINT): внешнюю откатывает фикстура, а нам
    нужно увидеть откат именно нашей записи, не выходя из тестовой сессии.
    """
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено"))
    case_id = changes.case.id

    savepoint = session.begin_nested()
    OutboxEventRepository(session).emit(changes.case, changes_to_events(changes))
    session.flush()
    assert session.scalars(
        select(OutboxEvent).where(OutboxEvent.case_id == case_id)
    ).all()
    savepoint.rollback()

    assert (
        session.scalars(select(OutboxEvent).where(OutboxEvent.case_id == case_id)).all()
        == []
    )


def test_outbox_events_are_read_in_insertion_order(session, court) -> None:
    """События читаются последовательно и не теряются при равных метках времени."""
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено", category="Другая"))
    repo = OutboxEventRepository(session)
    repo.emit(changes.case, changes_to_events(changes))
    session.flush()

    stored = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.case_id == changes.case.id)
        .order_by(OutboxEvent.id)
    ).all()
    assert len(stored) == 2
    assert [row.id for row in stored] == sorted(row.id for row in stored)


def test_outbox_event_payload_has_no_delivery_fields(session, court) -> None:
    """Core не знает получателей: ни user_id, ни sent/delivered (ТЗ PRIORITY 22)."""
    sync(session, court, page())
    session.flush()
    changes = sync(session, court, page(status="Рассмотрено"))

    for _, payload in changes_to_events(changes):
        forbidden = {"user_id", "sent", "delivered", "notification_status", "email"}
        assert not (forbidden & set(payload)), payload
