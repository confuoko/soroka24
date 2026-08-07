"""Тесты сверки «Истории местонахождения» со страницей.

Главный случай — портал мировых судов Москвы отдаёт две побайтово одинаковые строки
(та же дата, то же местонахождение, пустой комментарий). uid считается по
дело + дата + описание, поэтому у дубля он тот же, и вторая вставка раньше роняла
UNIQUE ix_place_history_uid, а с ним и всю транзакцию дела.
"""
from datetime import date

from app.models.database import Case
from app.repositories.place_history import PlaceHistoryRepository, place_history_uid

CASE_UID = "77MS0002-01-2026-000001-11"
# Номер дела: часть ключа карточки, поэтому обязателен.
CASE_CODE = "02-0001/2/2026"

# Ровно то, что лежит в снапшоте дела 77MS0002-01-2026-001236-26: два одинаковых <tr>.
DUPLICATE_ROWS = [
    {"place_date": date(2026, 6, 8), "place_description": "В канцелярии", "comment": None},
    {"place_date": date(2026, 6, 8), "place_description": "В канцелярии", "comment": None},
]


def _case(session, court) -> Case:
    case = Case(uid=CASE_UID, court=court, code=CASE_CODE)
    session.add(case)
    session.flush()
    return case


def test_duplicate_rows_collapsed_into_one(session, court) -> None:
    """Два одинаковых местонахождения на странице → одна строка в БД, без IntegrityError."""
    case = _case(session, court)

    new, updated, removed = PlaceHistoryRepository(session).sync_place_history(
        case, DUPLICATE_ROWS
    )

    assert len(new) == 1
    assert updated == []
    assert removed == []

    # Именно на flush раньше падало: duplicate key value violates unique constraint
    # "ix_place_history_uid".
    session.flush()

    assert len(case.place_history) == 1
    assert case.place_history[0].uid == place_history_uid(
        CASE_UID, date(2026, 6, 8), "В канцелярии"
    )


def test_second_sync_of_same_page_changes_nothing(session, court) -> None:
    """Повторный парсинг той же страницы не даёт диффа.

    Без этого схлопнутый дубль выглядел бы то новой строкой, то пропавшей, и пользователь
    получал бы ложное «появилось новое местонахождение» на каждом обходе.
    """
    case = _case(session, court)
    repo = PlaceHistoryRepository(session)
    repo.sync_place_history(case, DUPLICATE_ROWS)
    session.flush()

    new, updated, removed = repo.sync_place_history(case, DUPLICATE_ROWS)

    assert (new, updated, removed) == ([], [], [])
    assert len(case.place_history) == 1


def test_same_description_different_dates_kept_separately(session, court) -> None:
    """Одинаковое описание на разные даты — разные строки (как у 77MS0002-01-2026-001575-76)."""
    case = _case(session, court)

    new, _, _ = PlaceHistoryRepository(session).sync_place_history(
        case,
        [
            {"place_date": date(2026, 7, 16), "place_description": "В канцелярии", "comment": None},
            {"place_date": date(2026, 7, 21), "place_description": "В канцелярии", "comment": None},
        ],
    )
    session.flush()

    assert len(new) == 2
    assert len(case.place_history) == 2
