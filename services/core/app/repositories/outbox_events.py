"""Доступ к потоку доменных событий по делам (OutboxEvent) в БД."""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import Case, OutboxEvent, OutboxEventType


class OutboxEventRepository:
    """Чтение и запись событий мониторинга. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def emit(
        self, case: Case, events: list[tuple[OutboxEventType, dict]]
    ) -> list[OutboxEvent]:
        """Записать обнаруженные изменения по делу.

        Вызывать в ТОЙ ЖЕ транзакции, что и само обновление карточки: в этом и смысл
        outbox — изменение и факт события коммитятся атомарно, поэтому событие не может
        потеряться и не может появиться без изменения. Коммит — на вызывающей стороне.

        Список событий обычно пуст (холостой обход) — тогда в БД ничего не уходит.
        """
        if not events:
            return []

        rows = [
            OutboxEvent(case_id=case.id, event_type=event_type, payload=payload)
            for event_type, payload in events
        ]
        self._session.add_all(rows)
        self._session.flush()  # чтобы получить id ещё до commit
        return rows

    def list_since(
        self, case_id: int, since: datetime, limit: Optional[int] = None
    ) -> list[OutboxEvent]:
        """События дела, обнаруженные ПОСЛЕ указанного момента, в порядке обнаружения.

        Так клиентский сервис набирает уведомления: since — момент, когда пользователь
        поставил дело на мониторинг (или время последнего показанного ему события).
        Строгое «больше» — чтобы повторный вызов с временем последнего события не отдал
        его снова.
        """
        query = (
            select(OutboxEvent)
            .where(OutboxEvent.case_id == case_id, OutboxEvent.created_at > since)
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
        )
        if limit:
            query = query.limit(limit)
        return list(self._session.scalars(query))
