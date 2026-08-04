"""Тесты сверки судебных заседаний со страницей.

Ключевое отличие от событий и местонахождений: в identity заседания входит ВРЕМЯ
(court_session_uid), а result/basis/place изменяемые — у будущего заседания результата
ещё нет, потом он появляется, и это должен быть UPDATE той же строки, а не новое
заседание.
"""
from datetime import datetime

from app.models.database import Case
from app.repositories.court_sessions import CourtSessionRepository, court_session_uid

CASE_UID = "77MS0002-01-2026-000005-55"

# Как у 77MS0002-01-2026-001503-98: беседа прошла, судебное заседание ещё впереди.
PAGE_ROWS = [
    {
        "session_date": datetime(2026, 7, 30, 16, 50),
        "place": "2 - 124489, Зеленоград корп. 706",
        "stage": "Беседа",
        "result": "Проведена",
        "basis": None,
    },
    {
        "session_date": datetime(2026, 8, 14, 10, 0),
        "place": "2 - 124489, Зеленоград корп. 706",
        "stage": "Судебное заседание",
        "result": None,
        "basis": None,
    },
]


def _case(session) -> Case:
    case = Case(uid=CASE_UID)
    session.add(case)
    session.flush()
    return case


def test_sessions_created_from_page(session) -> None:
    """Заседания со страницы попадают в БД, время сохраняется."""
    case = _case(session)

    new, updated, removed = CourtSessionRepository(session).sync_court_sessions(
        case, PAGE_ROWS
    )
    session.flush()

    assert len(new) == 2
    assert (updated, removed) == ([], [])
    assert len(case.court_sessions) == 2

    by_stage = {s.stage: s for s in case.court_sessions}
    assert by_stage["Беседа"].session_date == datetime(2026, 7, 30, 16, 50)
    assert by_stage["Беседа"].result == "Проведена"
    # У будущего заседания результата ещё нет.
    assert by_stage["Судебное заседание"].result is None
    assert by_stage["Судебное заседание"].uid == court_session_uid(
        CASE_UID, datetime(2026, 8, 14, 10, 0), "Судебное заседание"
    )


def test_second_sync_of_same_page_changes_nothing(session) -> None:
    """Повторный парсинг той же страницы не даёт диффа.

    Самая опасная ошибка в этой сущности — нестабильный uid: при ней пользователь получал
    бы «назначено новое заседание» на каждом обходе.
    """
    case = _case(session)
    repo = CourtSessionRepository(session)
    repo.sync_court_sessions(case, PAGE_ROWS)
    session.flush()

    new, updated, removed = repo.sync_court_sessions(case, PAGE_ROWS)

    assert (new, updated, removed) == ([], [], [])
    assert len(case.court_sessions) == 2


def test_appearing_result_is_an_update_not_a_new_session(session) -> None:
    """У заседания появился результат → updated, а не new: identity не изменилась."""
    case = _case(session)
    repo = CourtSessionRepository(session)
    repo.sync_court_sessions(case, PAGE_ROWS)
    session.flush()

    # То же заседание, но портал уже проставил результат и основание.
    held = dict(PAGE_ROWS[1], result="Отложено", basis="Неявка подсудимого")

    new, updated, removed = repo.sync_court_sessions(case, [PAGE_ROWS[0], held])
    session.flush()

    assert new == []
    assert len(updated) == 1
    assert removed == []
    assert updated[0].result == "Отложено"
    assert updated[0].basis == "Неявка подсудимого"
    assert len(case.court_sessions) == 2


def test_same_day_same_stage_different_time_are_two_sessions(session) -> None:
    """Два заседания в один день с одной стадией — разные строки: время входит в identity."""
    case = _case(session)

    new, _, _ = CourtSessionRepository(session).sync_court_sessions(
        case,
        [
            {"session_date": datetime(2026, 8, 14, 10, 0), "place": None,
             "stage": "Судебное заседание", "result": None, "basis": None},
            {"session_date": datetime(2026, 8, 14, 15, 0), "place": None,
             "stage": "Судебное заседание", "result": None, "basis": None},
        ],
    )
    session.flush()

    assert len(new) == 2
    assert len({s.uid for s in case.court_sessions}) == 2


def test_duplicate_rows_collapsed_into_one(session) -> None:
    """Две побайтово одинаковые строки → одно заседание, flush не падает.

    Портал уже отдавал такие дубли в «Истории местонахождения» и ронял этим всю
    транзакцию дела на UNIQUE-индексе. Здесь индекс ix_court_session_uid такой же.
    """
    case = _case(session)

    new, _, _ = CourtSessionRepository(session).sync_court_sessions(
        case, [PAGE_ROWS[0], dict(PAGE_ROWS[0])]
    )
    session.flush()

    assert len(new) == 1
    assert len(case.court_sessions) == 1


def test_session_gone_from_page_is_removed(session) -> None:
    """Заседание пропало со страницы → удаляем: страница — источник истины."""
    case = _case(session)
    repo = CourtSessionRepository(session)
    repo.sync_court_sessions(case, PAGE_ROWS)
    session.flush()

    new, updated, removed = repo.sync_court_sessions(case, [PAGE_ROWS[0]])
    session.flush()

    assert (new, updated) == ([], [])
    assert len(removed) == 1
    assert removed[0].stage == "Судебное заседание"
    assert len(case.court_sessions) == 1
