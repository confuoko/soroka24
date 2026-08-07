"""Тесты сверки «Истории состояний» со страницей.

Та же ловушка, что и в истории местонахождения: uid события считается по
дело + дата + описание состояния, поэтому две одинаковые строки на странице дают один
uid, и вторая вставка роняет UNIQUE ix_event_uid вместе со всей транзакцией дела.
"""
from datetime import date

from app.models.database import Case
from app.repositories.events import EventRepository, event_uid

CASE_UID = "77MS0002-01-2026-000002-22"
# Номер дела: часть ключа карточки, поэтому обязателен.
CASE_CODE = "02-0002/2/2026"

DUPLICATE_EVENTS = [
    {"event_date": date(2026, 6, 8), "state_description": "Завершено", "document_str": None},
    {"event_date": date(2026, 6, 8), "state_description": "Завершено", "document_str": None},
]


def _case(session, court) -> Case:
    case = Case(uid=CASE_UID, court=court, code=CASE_CODE)
    session.add(case)
    session.flush()
    return case


def test_duplicate_events_collapsed_into_one(session, court) -> None:
    """Два одинаковых события на странице → одно событие в БД, без IntegrityError."""
    case = _case(session, court)

    new, updated, removed = EventRepository(session).sync_events(case, DUPLICATE_EVENTS)

    assert len(new) == 1
    assert updated == []
    assert removed == []

    session.flush()

    assert len(case.events) == 1
    assert case.events[0].uid == event_uid(CASE_UID, date(2026, 6, 8), "Завершено")


def test_second_sync_of_same_page_changes_nothing(session, court) -> None:
    """Повторный парсинг той же страницы не даёт диффа."""
    case = _case(session, court)
    repo = EventRepository(session)
    repo.sync_events(case, DUPLICATE_EVENTS)
    session.flush()

    new, updated, removed = repo.sync_events(case, DUPLICATE_EVENTS)

    assert (new, updated, removed) == ([], [], [])
    assert len(case.events) == 1


def test_same_state_different_dates_kept_separately(session, court) -> None:
    """Одинаковое состояние на разные даты — разные события."""
    case = _case(session, court)

    new, _, _ = EventRepository(session).sync_events(
        case,
        [
            {"event_date": date(2026, 6, 8), "state_description": "Завершено", "document_str": None},
            {"event_date": date(2026, 7, 8), "state_description": "Завершено", "document_str": None},
        ],
    )
    session.flush()

    assert len(new) == 2
    assert len(case.events) == 2
