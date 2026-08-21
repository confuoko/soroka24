"""Тесты сверки «Истории состояний» со страницей.

Та же ловушка, что и в истории местонахождения: uid события считается по
дело + дата + описание состояния, поэтому две одинаковые строки на странице дают один
uid, и вторая вставка роняет UNIQUE ix_event_uid вместе со всей транзакцией дела.
"""
from datetime import date, datetime

from app.models.database import Case
from app.repositories.events import EventRepository, event_uid

CASE_UID = "77MS0002-01-2026-000002-22"
# Номер дела: часть ключа карточки, поэтому обязателен.
CASE_CODE = "02-0002/2/2026"

DUPLICATE_EVENTS = [
    {"event_date": datetime(2026, 6, 8, 10, 0), "state_description": "Завершено", "document_str": None},
    {"event_date": datetime(2026, 6, 8, 10, 0), "state_description": "Завершено", "document_str": None},
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
    same_event = [{"event_date": datetime(2026, 6, 8, 10, 0), "state_description": "Завершено"}]

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
        case.card_key, datetime(2026, 6, 8, 10, 0), "Завершено"
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
            {"event_date": datetime(2026, 6, 8, 10, 0), "state_description": "Завершено", "document_str": None},
            {"event_date": datetime(2026, 7, 8, 10, 0), "state_description": "Завершено", "document_str": None},
        ],
    )
    session.flush()

    assert len(new) == 2
    assert len(case.events) == 2


def test_published_at_is_saved_and_updated_separately(session, court) -> None:
    """«Дата размещения» — изменяемое поле: портал проставляет её позже самого события.

    Проверяем именно её в одиночку: раньше ветка обновления сверяла только document_str,
    и правка любого другого поля молча терялась бы, а событие не попало бы в updated.
    """
    case = _case(session, court)
    repo = EventRepository(session)
    page = [
        {
            "event_date": datetime(2026, 6, 8, 10, 0),
            "state_description": "Завершено",
            "document_str": None,
            "published_at": None,
        }
    ]
    repo.sync_events(case, page)
    session.flush()

    republished = [dict(page[0], published_at=date(2026, 6, 20))]
    new, updated, removed = repo.sync_events(case, republished)

    assert (new, removed) == ([], [])
    assert len(updated) == 1
    assert case.events[0].published_at == date(2026, 6, 20)


def test_published_at_stays_out_of_event_identity(session, court) -> None:
    """Смена «Даты размещения» не пересоздаёт событие: uid считается без неё.

    Иначе на каждой публикации портала событие удалялось бы и заводилось заново, а в
    истории дела это выглядело бы как новое событие.
    """
    case = _case(session, court)
    repo = EventRepository(session)
    base = {
        "event_date": datetime(2026, 6, 8, 10, 0),
        "state_description": "Завершено",
        "document_str": None,
    }
    repo.sync_events(case, [dict(base, published_at=date(2026, 6, 10))])
    session.flush()
    first_uid = case.events[0].uid

    repo.sync_events(case, [dict(base, published_at=date(2026, 6, 20))])
    session.flush()

    assert len(case.events) == 1
    assert case.events[0].uid == first_uid
