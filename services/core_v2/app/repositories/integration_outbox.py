"""Доступ к очереди сообщений на публикацию наружу (IntegrationOutboxEvent).

Две работы, у каждой свой вызывающий:

    emit              — обход: записать сообщения в той же транзакции, что и изменение
    take_unpublished  — publisher: забрать порцию неопубликованного
    mark_published    — publisher: отметить отправленное

В отличие от домен-лога, эта таблица ОБНОВЛЯЕТСЯ: published_at по определению меняется.
Ничего другого здесь не обновляется и не удаляется — сообщение это факт.
"""
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import IntegrationOutboxEvent

if TYPE_CHECKING:
    # Только для типа: настоящий импорт был бы циклом (app/integration_events.py берёт
    # DomainEvent из app/outbox.py, а тот — CaseFieldChange отсюда, из репозиториев).
    from app.integration_events import IntegrationEvent


class IntegrationOutboxRepository:
    """Чтение и запись очереди на публикацию. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def emit(self, events: list["IntegrationEvent"]) -> list[IntegrationOutboxEvent]:
        """Записать сообщения к публикации.

        Вызывать в ТОЙ ЖЕ транзакции, что и изменение карточки, — в этом весь смысл
        transactional outbox: сообщение не может потеряться (коммитится вместе с
        изменением) и не может появиться без изменения (откат уносит оба). Коммит — на
        вызывающей стороне.

        Пустой список — самый частый случай: холостой обход не меняет ничего. Тогда в БД
        не уходит ни одной строки.

        occurred_at и version не задаём: их ставит БД своими server_default. Момент берётся
        из func.now(), то есть из начала транзакции, — у всех сообщений одного обхода он
        одинаковый, и это правильно: изменения обнаружены одним походом.
        """
        if not events:
            return []

        rows = [
            IntegrationOutboxEvent(
                event_type=event.event_type,
                case_id=event.case_id,
                entity_id=event.entity_id,
            )
            for event in events
        ]
        self._session.add_all(rows)
        self._session.flush()  # чтобы id сообщения был известен ещё до коммита
        return rows

    def take_unpublished(self, limit: int = 100) -> list[IntegrationOutboxEvent]:
        """Забрать порцию неопубликованных сообщений, по порядку появления.

        FOR UPDATE SKIP LOCKED — чтобы второй запущенный publisher не разослал те же
        сообщения повторно, а взял следующие. Publisher нужен один, но защита стоит
        дёшево, а цена ошибки — дубли у каждого подписчика.

        Порядок по id, а не по occurred_at: id строго возрастает и не зависит от часов, а
        occurred_at у всех сообщений одного обхода одинаковый.

        Блокировка держится до конца транзакции вызывающего — то есть publisher обязан
        закоммитить (или откатить) порцию, прежде чем брать следующую.
        """
        return list(
            self._session.scalars(
                select(IntegrationOutboxEvent)
                .where(IntegrationOutboxEvent.published_at.is_(None))
                .order_by(IntegrationOutboxEvent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )

    def mark_published(
        self,
        rows: list[IntegrationOutboxEvent],
        published_at: Optional[datetime] = None,
    ) -> None:
        """Отметить сообщения отправленными.

        Момент берём из питона, а не из func.now(): now() отдал бы время НАЧАЛА транзакции
        publisher'а, то есть момент до отправки, а нам нужен момент после неё. Разница
        невелика, но published_at существует именно для того, чтобы по нему считать
        задержку доставки, и врать в нём нельзя.

        Коммит — на вызывающей стороне. Публикация в брокер уже произошла, поэтому упавший
        здесь коммит означает повторную отправку на следующем круге; так и задумано, см.
        докстринг модели про at-least-once.
        """
        moment = published_at or datetime.now(timezone.utc)
        for row in rows:
            row.published_at = moment
