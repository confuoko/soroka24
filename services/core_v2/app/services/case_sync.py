"""CaseSync — единственная операция, которая приводит БД к состоянию страницы суда.

    ParsedCase → sync_case → состояние PostgreSQL равно состоянию страницы → CaseChanges

Источник истины — страница. Судьи, стороны, события, местонахождения, заседания и
документы приводятся к тому, что на ней сейчас: чего там нет, того нет и у нас.
Функция возвращает CaseChanges — что появилось, что изменилось, что пропало.

Операция ОДНА. Первое обнаружение дела и повторная синхронизация существующего
отличаются только тем, как добыта страница; сохраняются они одинаково и этим кодом.
Отдельного «refresh» рядом с «sync» здесь нет и быть не должно.

В старом core этот код лежал в app/monitoring/case_update.py. Имя пакета вводило в
заблуждение: к пользовательскому мониторингу он отношения не имел — это ядро.

Функция чистая относительно инфраструктуры: ни сети, ни браузера, ни брокера. Работает
в рамках переданной сессии, коммит — забота вызывающего.
"""
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import (
    Case,
    Court,
    CourtSession,
    Document,
    Event,
    Judge,
    PlaceHistory,
    Side,
)
from app.parsers.parsed_case import ParsedCase
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
    # Карточка заведена этим же обходом. Тогда всё её содержимое формально «новое», и
    # событий об изменениях по нему не выпускается — это baseline, а не изменения
    # (см. app/outbox.py).
    is_new: bool
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


def sync_case(
    session: Session,
    uid: str,
    parsed: ParsedCase,
    court: Court,
    code: str,
    source_url: str | None = None,
) -> CaseChanges:
    """Привести карточку в БД к состоянию страницы и вернуть список изменений.

    Суд, номер дела и адрес страницы приходят АРГУМЕНТАМИ, а не внутри parsed, и это не
    случайность: ни одно из трёх не является содержимым карточки. Суд определяется по
    справочнику (по номеру участка из таблицы результатов или по хосту ссылки), номер —
    из той же строки таблицы результатов, адрес знает тот, кто ходил на портал. Все три
    известны до разбора; парсер про них ничего не знает.

    В старом core адрес передавался внутри словаря разбора, ключом "url", который
    дописывала туда Celery-задача уже после парсинга.

    Работает в рамках переданной сессии; коммит — на вызывающей стороне.
    """
    # 1. Карточка: найти по тройке «УИД + суд + номер» или создать, обновить поля.
    cases = CaseRepository(session)
    case, field_changes, is_new = cases.upsert_by_uid_court_code(
        uid, court, code, parsed.card_fields()
    )
    # Адрес карточки — в список адресов, а не в поле дела: их у карточки несколько.
    # Сюда он приходит либо от парсера, либо от задачи, которую завели ссылкой.
    # Раз мы дошли до разбора, значит по этому адресу страница открылась — отмечаем.
    if source_url:
        cases.add_url(case, source_url)
        cases.mark_url_success(source_url)

    # 2. Судьи — полная сверка со страницей.
    desired_judges = JudgeRepository(session).get_or_create_many(
        parsed.judge_names
    )
    added_judges, removed_judges = _reconcile(case.judges, desired_judges)

    # 3. Стороны — полная сверка со страницей.
    desired_sides = SideRepository(session).get_or_create_many(
        parsed.sides
    )
    added_sides, removed_sides = _reconcile(case.sides, desired_sides)

    # 4. События «Истории состояний» — сверка со страницей (new/updated/removed).
    new_events, updated_events, removed_events = EventRepository(
        session
    ).sync_events(case, parsed.events)

    # 5. «История местонахождения» — сверка со страницей (new/updated/removed).
    #    У порталов, где такого блока нет, список пуст — это не то же самое, что
    #    «блока не было»: пустой список означает «на странице ни одной строки», и
    #    существующие строки в БД корректно удаляются.
    new_places, updated_places, removed_places = PlaceHistoryRepository(
        session
    ).sync_place_history(case, parsed.place_history)

    # 6. Судебные заседания — сверка со страницей (new/updated/removed).
    #    У приказных дел вкладки заседаний нет совсем — список пуст.
    new_sessions, updated_sessions, removed_sessions = CourtSessionRepository(
        session
    ).sync_court_sessions(case, parsed.court_sessions)

    # 7. Документы — сверка со страницей (new/removed, изменяемых полей нет).
    new_documents, removed_documents = DocumentRepository(session).sync_documents(
        case, parsed.documents
    )

    return CaseChanges(
        case=case,
        is_new=is_new,
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
