"""Pydantic-схемы запросов и ответов API дел."""
from typing import Optional

from pydantic import BaseModel

from app.models.database import SearchStatus


class CaseSyncRequest(BaseModel):
    """Тело запроса POST /cases: УИД дела для синхронизации."""

    uid: str  # уникальный идентификатор дела (например, 77MS0466-01-2026-003751-93)


class CaseSyncResponse(BaseModel):
    """Ответ POST /cases: либо id существующего дела, либо id запущенной задачи."""

    status: str  # exists | processing | invalid_uid | unsupported_court
    case_id: Optional[int] = None  # заполнен, если дело уже есть в БД
    task_id: Optional[int] = None  # заполнен, если запущена фоновая синхронизация


class SearchTaskResponse(BaseModel):
    """Ответ GET /cases/tasks/{id}: текущее состояние задачи поиска."""

    task_id: int
    uid: str
    status: SearchStatus  # pending | running | success | failed
    case_id: Optional[int] = None  # появляется, когда дело найдено/создано
    attempts: int  # сколько было попыток зайти на страницу
    last_error: Optional[str] = None  # текст последней ошибки, если была
