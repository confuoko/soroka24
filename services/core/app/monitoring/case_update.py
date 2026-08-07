"""Обновление дела по данным парсера: сверка судей, сторон, событий, местонахождений и заседаний + diff.

Вынесено из Celery-таска отдельной функцией, чтобы её можно было тестировать на
чистой сессии БД, без Chromium и брокера. Источник истины — страница суда:
судьи/стороны/события/местонахождения приводятся к тому, что на ней сейчас, а метод
возвращает CaseChanges — что появилось, что изменилось, что отвязано/удалено.
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.database import (
    Case,
    Court,
    CourtSession,
    Document,
    Event,
    Judge,
    PlaceHistory,
    Side,
)
from app.repositories import (
    CaseRepository,
    CaseFieldChange,
    CourtSessionRepository,
    DocumentRepository,
    EventRepository,
    JudgeRepository,
    PlaceHistoryRepository,
    SideRepository,
)


@dataclass
class CaseChanges:
    """Что изменилось по делу за одну синхронизацию."""

    case: Case                    # созданное/обновлённое дело (аггрегат)
    # Изменившиеся скалярные поля дела (статус, решение, даты…). У нового дела пуст.
    field_changes: list[CaseFieldChange]
    new_events: list[Event]       # новые строки «Истории состояний»
    updated_events: list[Event]   # события, у которых поменялся document_str
    removed_events: list[Event]   # события, пропавшие со страницы (удалены)
    new_places: list[PlaceHistory]      # новые строки «Истории местонахождения»
    updated_places: list[PlaceHistory]  # местонахождения, у которых поменялся comment
    removed_places: list[PlaceHistory]  # местонахождения, пропавшие со страницы (удалены)
    new_sessions: list[CourtSession]      # назначенные судебные заседания
    updated_sessions: list[CourtSession]  # заседания, у которых поменялся place/result/basis
    removed_sessions: list[CourtSession]  # заседания, пропавшие со страницы (сняты)
    # У документов нет ветки updated: изменяемых полей не осталось — дата и вид входят в
    # identity, а текст документа мы не храним.
    new_documents: list[Document]         # новые документы по делу
    removed_documents: list[Document]     # документы, пропавшие со страницы
    added_judges: list[Judge]     # привязанные судьи
    removed_judges: list[Judge]   # отвязанные судьи
    added_sides: list[Side]       # привязанные стороны
    removed_sides: list[Side]     # отвязанные стороны

    def has_changes(self) -> bool:
        return any(
            (
                self.field_changes,
                self.new_events,
                self.updated_events,
                self.removed_events,
                self.new_places,
                self.updated_places,
                self.removed_places,
                self.new_sessions,
                self.updated_sessions,
                self.removed_sessions,
                self.new_documents,
                self.removed_documents,
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
    session: Session, uid: str, data: dict, court: Court, code: str
) -> CaseChanges:
    """Создать/обновить дело по данным парсера и вернуть diff изменений.

    Предполагается, что суд и номер дела уже известны (оба резолвятся до вызова: суд — по
    номеру участка из таблицы результатов или по хосту ссылки, номер — из той же строки
    таблицы). Оба входят в ключ карточки, поэтому приходят аргументами, а не в data.

    Работает в рамках переданной сессии; коммит — на вызывающей стороне.
    """
    # 1. Карточка: найти по тройке «УИД + суд + номер» или создать, обновить поля.
    cases = CaseRepository(session)
    case, field_changes = cases.upsert_by_uid_court_code(uid, court, code, data)
    # Адрес карточки — в список адресов, а не в поле дела: их у карточки несколько.
    # Сюда он приходит либо от парсера, либо от задачи, которую завели ссылкой.
    # Раз мы дошли до разбора, значит по этому адресу страница открылась — отмечаем.
    if data.get("url"):
        cases.add_url(case, data["url"])
        cases.mark_url_success(data["url"])

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

    # 6. Судебные заседания — сверка со страницей (new/updated/removed).
    #    data.get(..., []) — у приказных дел вкладки заседаний на странице нет совсем.
    new_sessions, updated_sessions, removed_sessions = CourtSessionRepository(
        session
    ).sync_court_sessions(case, data.get("court_sessions", []))

    # 7. Документы — сверка со страницей (new/removed, изменяемых полей нет).
    new_documents, removed_documents = DocumentRepository(session).sync_documents(
        case, data.get("documents", [])
    )

    return CaseChanges(
        case=case,
        field_changes=field_changes,
        new_events=new_events,
        updated_events=updated_events,
        removed_events=removed_events,
        new_places=new_places,
        updated_places=updated_places,
        removed_places=removed_places,
        new_sessions=new_sessions,
        updated_sessions=updated_sessions,
        removed_sessions=removed_sessions,
        new_documents=new_documents,
        removed_documents=removed_documents,
        added_judges=added_judges,
        removed_judges=removed_judges,
        added_sides=added_sides,
        removed_sides=removed_sides,
    )
