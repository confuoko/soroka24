"""Обновление дела по данным парсера: сверка судей, сторон, событий и местонахождений + diff.

Вынесено из Celery-таска отдельной функцией, чтобы её можно было тестировать на
чистой сессии БД, без Chromium и брокера. Источник истины — страница суда:
судьи/стороны/события/местонахождения приводятся к тому, что на ней сейчас, а метод
возвращает CaseChanges — что появилось, что изменилось, что отвязано/удалено.
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.database import Case, Court, Event, Judge, PlaceHistory, Side
from app.repositories import (
    CaseRepository,
    EventRepository,
    JudgeRepository,
    PlaceHistoryRepository,
    SideRepository,
)


@dataclass
class CaseChanges:
    """Что изменилось по делу за одну синхронизацию."""

    case: Case                    # созданное/обновлённое дело (аггрегат)
    new_events: list[Event]       # новые строки «Истории состояний»
    updated_events: list[Event]   # события, у которых поменялся document_str
    removed_events: list[Event]   # события, пропавшие со страницы (удалены)
    new_places: list[PlaceHistory]      # новые строки «Истории местонахождения»
    updated_places: list[PlaceHistory]  # местонахождения, у которых поменялся comment
    removed_places: list[PlaceHistory]  # местонахождения, пропавшие со страницы (удалены)
    added_judges: list[Judge]     # привязанные судьи
    removed_judges: list[Judge]   # отвязанные судьи
    added_sides: list[Side]       # привязанные стороны
    removed_sides: list[Side]     # отвязанные стороны

    def has_changes(self) -> bool:
        return any(
            (
                self.new_events,
                self.updated_events,
                self.removed_events,
                self.new_places,
                self.updated_places,
                self.removed_places,
                self.added_judges,
                self.removed_judges,
                self.added_sides,
                self.removed_sides,
            )
        )


def _reconcile(current: list, desired: list) -> tuple[list, list]:
    """Свести список связей дела к desired: вернуть (added, removed).

    Мутирует current in place (append/remove), чтобы сработали ORM-связи many-to-many.
    Сравнение по идентичности объектов — desired приходит из get_or_create_many,
    т.е. это те же экземпляры, что уже могут быть в current.
    """
    added = [obj for obj in desired if obj not in current]
    removed = [obj for obj in current if obj not in desired]
    for obj in added:
        current.append(obj)
    for obj in removed:
        current.remove(obj)
    return added, removed


def update_case(
    session: Session, uid: str, data: dict, court: Court
) -> CaseChanges:
    """Создать/обновить дело по данным парсера и вернуть diff изменений.

    Предполагается, что суд уже найден в справочнике (резолвится до вызова).
    Работает в рамках переданной сессии; коммит — на вызывающей стороне.
    """
    # 1. Дело: создать/обновить поля и идемпотентно привязать суд.
    case = CaseRepository(session).upsert_by_uid(uid, data)
    if court not in case.courts:
        case.courts.append(court)

    # 2. Судьи — полная сверка со страницей.
    desired_judges = JudgeRepository(session).get_or_create_many(
        data.get("judge_names", [])
    )
    added_judges, removed_judges = _reconcile(case.judges, desired_judges)

    # 3. Стороны — полная сверка со страницей.
    desired_sides = SideRepository(session).get_or_create_many(
        data.get("sides", [])
    )
    added_sides, removed_sides = _reconcile(case.sides, desired_sides)

    # 4. События «Истории состояний» — сверка со страницей (new/updated/removed).
    new_events, updated_events, removed_events = EventRepository(
        session
    ).sync_events(case, data.get("events", []))

    # 5. «История местонахождения» — сверка со страницей (new/updated/removed).
    #    data.get(..., []) — парсер другого типа страницы может не отдавать этот ключ.
    new_places, updated_places, removed_places = PlaceHistoryRepository(
        session
    ).sync_place_history(case, data.get("place_history", []))

    return CaseChanges(
        case=case,
        new_events=new_events,
        updated_events=updated_events,
        removed_events=removed_events,
        new_places=new_places,
        updated_places=updated_places,
        removed_places=removed_places,
        added_judges=added_judges,
        removed_judges=removed_judges,
        added_sides=added_sides,
        removed_sides=removed_sides,
    )
