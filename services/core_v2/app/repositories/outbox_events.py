"""Доступ к потоку событий об изменениях по делам (OutboxEvent).

Таблица append-only: строки только добавляются и читаются в порядке обнаружения.
Никакого UPDATE здесь нет и быть не должно — событие это факт, а факты не переписывают.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Case, OutboxEvent, OutboxEventType


class OutboxEventRepository:
    """Чтение и запись событий об изменениях. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def emit(
        self, case: Case, events: list[tuple[OutboxEventType, dict]]
    ) -> list[OutboxEvent]:
        """Записать обнаруженные изменения по делу.

        Вызывать в ТОЙ ЖЕ транзакции, что и само обновление карточки: в этом и весь смысл
        outbox. Событие не может потеряться (коммитится вместе с изменением) и не может
        появиться без изменения (откат уносит оба). Коммит — на вызывающей стороне.

        Список событий обычно пуст — холостой обход не меняет ничего. Тогда в БД не
        уходит ни одной строки, и это нормальный, самый частый случай.
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

        Так читающий сервис набирает изменения: since — момент, с которого его интересуют
        события по делу (обычно время последнего уже показанного).

        Строгое «больше» — чтобы повторный вызов с временем последнего события не отдал
        его снова.

        Сортировка по (created_at, id), а не по одному created_at: метка берётся из
        func.now(), то есть из момента НАЧАЛА транзакции, поэтому у всех событий одного
        обхода она одинаковая. Без id в сортировке их взаимный порядок был бы
        неопределён, а он значим — события выкладываются в том порядке, в каком их нашла
        сверка.
        """
        query = (
            select(OutboxEvent)
            .where(OutboxEvent.case_id == case_id, OutboxEvent.created_at > since)
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
        )
        if limit:
            query = query.limit(limit)
        return list(self._session.scalars(query))
