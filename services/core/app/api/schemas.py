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
    """Тело запроса POST /search_case: УИД дела для синхронизации."""

    uid: str  # уникальный идентификатор дела (например, 77MS0466-01-2026-003751-93)
    # Перепарсить дело, даже если оно уже есть в БД (иначе сразу вернём его id).
    force: bool = False


class CaseSyncResponse(BaseModel):
    """Ответ POST /search_case: либо id существующего дела, либо id запущенной задачи."""

    status: str  # exists | processing | invalid_uid | unsupported_court
    case_id: Optional[int] = None  # заполнен, если дело уже есть в БД
    task_id: Optional[int] = None  # заполнен, если запущена фоновая синхронизация


class SearchTaskResponse(BaseModel):
    """Ответ GET /search_case/tasks/{id}: текущее состояние задачи поиска."""

    task_id: int
    uid: str
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


class JudgeOut(_FromORM):
    """Судья в ответе API."""

    id: int
    full_name: str


class SideOut(_FromORM):
    """Сторона по делу в ответе API."""

    id: int
    full_name: str
    type: SideType  # Истец | Ответчик | Другое


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
    """Документ по делу.

    document_text намеренно не отдаём — он может быть очень большим; колонка в БД
    остаётся, в JSON не попадает.
    """

    uid: uuid.UUID
    document_date: Optional[date] = None
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
    url: Optional[str] = None
    code: Optional[str] = None
    application_number: Optional[str] = None
    incoming_number: Optional[str] = None
    receipt_date: Optional[date] = None
    category: Optional[str] = None
    status: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    courts: list[CourtOut]
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
