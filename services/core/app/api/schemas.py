"""Pydantic-схемы запросов и ответов API дел.

Это НЕ модели БД: таблиц они не создают, миграций не требуют — только описывают,
какие поля попадут в JSON. Модели БД лежат в app/models/database.py.
"""
import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.database import CourtLevel, SearchStatus, SideType


class CaseSyncRequest(BaseModel):
    """Тело запроса POST /search_case: чем искать дело.

    Одно поле на оба способа: у порталов вроде mos-sud.ru есть поиск по УИД, а у
    msudrf.ru и большинства региональных его нет — там дело открывается только прямой
    ссылкой. Что именно прислали, сервер определяет сам по схеме (http:// или https://).
    """

    # УИД дела (77MS0466-01-2026-003751-93) либо ссылка на карточку
    # (https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=...).
    query: str
    # Перепарсить дело, даже если оно уже есть в БД (иначе сразу вернём его id).
    force: bool = False


class CaseSyncResponse(BaseModel):
    """Ответ POST /search_case: либо id существующего дела, либо id запущенной задачи."""

    # exists | processing | invalid_query | invalid_uid | link_required | unsupported_court
    status: str
    case_id: Optional[int] = None  # заполнен, если дело уже есть в БД
    task_id: Optional[int] = None  # заполнен, если запущена фоновая синхронизация
    # Пояснение для пользователя, когда запрос отклонён: какой суд определился и что
    # с этим делать. Заполняется у link_required и unsupported_court.
    message: Optional[str] = None


class SearchTaskResponse(BaseModel):
    """Ответ GET /search_case/tasks/{id}: текущее состояние задачи поиска."""

    task_id: int
    # Пусто, пока дело завели ссылкой и до портала ещё не дошли: УИД станет известен
    # только когда задача откроет страницу.
    uid: Optional[str] = None
    # Ссылка, которой завели дело (у задач по УИД пусто).
    source_url: Optional[str] = None
    status: SearchStatus  # pending | running | success | failed
    case_id: Optional[int] = None  # появляется, когда дело найдено/создано
    attempts: int  # сколько было попыток зайти на страницу
    last_error: Optional[str] = None  # текст последней ошибки, если была


# --- Схемы карточки дела (GET /cases/{case_id}) -------------------------------
# from_attributes=True разрешает собирать схему прямо из ORM-объекта
# (CaseDetailResponse.model_validate(case)) вместо перечисления полей руками.


class _FromORM(BaseModel):
    """Общая база: читать значения из атрибутов ORM-объекта."""

    model_config = ConfigDict(from_attributes=True)


class CourtOut(_FromORM):
    """Суд в ответе API."""

    id: int
    code: str
    name: str
    level: CourtLevel
    region: str
    base_url: Optional[str] = None


class CaseUrlOut(_FromORM):
    """Адрес, по которому открывается карточка дела.

    Их несколько: на одну карточку ведут http и https, разный порядок параметров,
    сменившийся после переезда участка адрес.
    """

    url: str
    # Когда по этому адресу последний раз удалось получить страницу (None — ни разу).
    last_success_at: Optional[datetime] = None


class JudgeOut(_FromORM):
    """Судья в ответе API."""

    id: int
    full_name: str


class SideOut(_FromORM):
    """Сторона по делу в ответе API."""

    id: int
    full_name: str
    # Роль как на портале: «Истец», «Взыскатель», «Должник», «Подсудимый»…
    role: Optional[str] = None
    type: SideType  # грубая классификация: Истец | Ответчик | Другое


class EventOut(_FromORM):
    """Событие «Истории состояний» в ответе API."""

    uid: uuid.UUID
    event_date: date  # NOT NULL в БД: входит в identity события (см. event_uid)
    state_description: str
    document_str: Optional[str] = None


class PlaceHistoryOut(_FromORM):
    """Строка «Истории местонахождения» в ответе API."""

    uid: uuid.UUID
    place_date: date  # NOT NULL в БД: входит в identity строки (см. place_history_uid)
    place_description: str
    comment: Optional[str] = None


class InstanceOut(_FromORM):
    """Инстанция, через которую прошло дело."""

    uid: uuid.UUID
    instance_number: str


class DocumentOut(_FromORM):
    """Документ по делу — только метаданные.

    document_text не отдаём, потому что мы его и не храним: ни текст документа, ни ссылку
    на файл парсер не сохраняет. Колонка в БД осталась пустой для совместимости.
    """

    uid: uuid.UUID
    # NOT NULL в БД: дата входит в identity документа (см. document_uid).
    document_date: date
    document_type: str


class CourtSessionOut(_FromORM):
    """Судебное заседание по делу."""

    uid: uuid.UUID
    # NOT NULL в БД: дата И время входят в identity заседания (см. court_session_uid).
    session_date: datetime
    place: Optional[str] = None
    stage: str
    result: Optional[str] = None
    basis: Optional[str] = None


class CaseDetailResponse(_FromORM):
    """Ответ GET /cases/{case_id}: дело со всеми привязанными сущностями."""

    id: int
    uid: str
    code: Optional[str] = None
    application_number: Optional[str] = None
    incoming_number: Optional[str] = None
    receipt_date: Optional[date] = None
    registration_date: Optional[date] = None
    first_instance_date: Optional[date] = None
    first_instance_decision: Optional[str] = None
    decision_effective_date: Optional[date] = None
    superior_case_number: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    court: CourtOut
    urls: list[CaseUrlOut] = []
    judges: list[JudgeOut]
    sides: list[SideOut]
    events: list[EventOut]
    place_history: list[PlaceHistoryOut]
    instances: list[InstanceOut]
    documents: list[DocumentOut]
    court_sessions: list[CourtSessionOut]

    # Группа связанных дел: id самой группы и id остальных дел в ней.
    case_link_id: Optional[int] = None
    related_case_ids: list[int] = []
