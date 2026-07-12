"""Доступ к задачам поиска/синхронизации (SearchTask) в БД."""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import SearchStatus, SearchTask


class SearchTaskRepository:
    """Чтение и запись задач поиска. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, uid: str) -> SearchTask:
        """Создать новую задачу в статусе PENDING."""
        task = SearchTask(uid=uid, status=SearchStatus.PENDING)
        self._session.add(task)
        self._session.flush()  # чтобы получить task.id
        return task

    def get(self, task_id: int) -> Optional[SearchTask]:
        """Задача по id (или None)."""
        return self._session.get(SearchTask, task_id)

    def get_active_by_uid(self, uid: str) -> Optional[SearchTask]:
        """Незавершённая задача по этому УИД (PENDING/RUNNING) — для идемпотентности."""
        return self._session.scalar(
            select(SearchTask).where(
                SearchTask.uid == uid,
                SearchTask.status.in_([SearchStatus.PENDING, SearchStatus.RUNNING]),
            )
        )

    def mark_running(self, task: SearchTask) -> None:
        """Задача пошла в работу: статус RUNNING, +1 попытка, отметка времени."""
        task.status = SearchStatus.RUNNING
        task.attempts += 1
        task.last_attempt_at = datetime.utcnow()

    def mark_success(self, task: SearchTask, case_id: int) -> None:
        """Успех: статус SUCCESS, привязываем найденное дело."""
        task.status = SearchStatus.SUCCESS
        task.case_id = case_id
        task.last_error = None

    def mark_failed(self, task: SearchTask, error: str, page_status: Optional[int] = None) -> None:
        """Провал: статус FAILED, сохраняем текст ошибки и (если есть) статус страницы."""
        task.status = SearchStatus.FAILED
        task.last_error = error
        if page_status is not None:
            task.page_status = page_status
