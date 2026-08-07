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


def _case(session, court, code: str = CASE_CODE) -> Case:
    case = Case(uid=CASE_UID, court=court, code=code)
    session.add(case)
    session.flush()
    return case


def test_same_event_on_two_cards_of_one_uid(session, court) -> None:
    """Одинаковое событие у двух карточек одного УИД сохраняется у обеих.

    Регрессия на живой отказ: uid события считался от УИД дела, а индекс на нём глобальный
    — вторая карточка того же УИД (другое производство в том же суде) падала на
    ix_event_uid, и вся её транзакция откатывалась. Теперь uid считается от карточки.
    """
    first = _case(session, court, code="02-0002/2/2026")
    second = _case(session, court, code="02-0777/2/2026")
    same_event = [{"event_date": date(2026, 6, 8), "state_description": "Завершено"}]

    repo = EventRepository(session)
    repo.sync_events(first, same_event)
    repo.sync_events(second, same_event)
    session.flush()

    assert first.events[0].uid != second.events[0].uid
    assert len(first.events) == 1 and len(second.events) == 1


def test_duplicate_events_collapsed_into_one(session, court) -> None:
    """Два одинаковых события на странице → одно событие в БД, без IntegrityError."""
    case = _case(session, court)

    new, updated, removed = EventRepository(session).sync_events(case, DUPLICATE_EVENTS)

    assert len(new) == 1
    assert updated == []
    assert removed == []

    session.flush()

    assert len(case.events) == 1
    assert case.events[0].uid == event_uid(
        case.card_key, date(2026, 6, 8), "Завершено"
    )


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
