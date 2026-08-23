"""Доступ к задачам поиска/синхронизации (SearchTask) в БД."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SearchStatus, SearchTask


class SearchTaskRepository:
    """Чтение и запись задач поиска. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, uid: Optional[str] = None, source_url: Optional[str] = None
    ) -> SearchTask:
        """Создать новую задачу в статусе PENDING.

        Задают одно из двух: uid — если у портала есть поиск по УИД (Москва),
        source_url — если дело пришло ссылкой и УИД станет известен только после
        похода на страницу.
        """
        if not uid and not source_url:
            raise ValueError("Задаче нужен либо УИД, либо ссылка на карточку дела")
        task = SearchTask(uid=uid, source_url=source_url, status=SearchStatus.PENDING)
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

    def get_active_by_url(self, source_url: str) -> Optional[SearchTask]:
        """Незавершённая задача по этой ссылке (PENDING/RUNNING) — для идемпотентности.

        Отдельно от get_active_by_uid: у задачи, заведённой ссылкой, УИД появляется
        только после похода на портал, поэтому дедуплицировать по нему нечем.
        """
        return self._session.scalar(
            select(SearchTask).where(
                SearchTask.source_url == source_url,
                SearchTask.status.in_([SearchStatus.PENDING, SearchStatus.RUNNING]),
            )
        )

    def set_uid(self, task: SearchTask, uid: str) -> None:
        """Записать УИД, найденный на странице дела, в задачу, заведённую по ссылке."""
        task.uid = uid

    def mark_running(self, task: SearchTask) -> None:
        """Задача пошла в работу: статус RUNNING, +1 попытка, отметка времени."""
        task.status = SearchStatus.RUNNING
        task.attempts += 1
        task.last_attempt_at = datetime.now(timezone.utc)

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
