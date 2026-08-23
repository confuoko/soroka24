"""Зафиксированное изменение судебной карточки.

НЕ уведомление. Строка здесь означает ровно одно: «core обнаружил, что на портале
что-то изменилось». Кому и как об этом сообщать — не забота core, он про пользователей
не знает вовсе. Поэтому колонок доставки (user_id, sent, delivered) здесь нет и быть
не должно.

Таблица append-only: строки только добавляются, читаются по возрастанию id и никогда
не обновляются.
"""
from datetime import datetime

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import UTC_DATETIME, Base
from app.models.case import Case  # noqa: F401 — держит порядок создания таблиц
from app.models.enums import OutboxEventType


class OutboxEvent(Base):
    """Одно обнаруженное изменение по делу (outbox pattern).

    Пишется в ТОЙ ЖЕ транзакции, что и само изменение дела (см. app/services/discovery.py):
    поэтому событие не может потеряться и не может появиться без изменения в карточке.

    Таблица append-only: строки не редактируются и не удаляются. Полей доставки
    («отправлено», «попыток») здесь намеренно нет — на дело подписано несколько
    пользователей, и пометка на самой строке потеряла бы событие для всех, кроме первого.
    Кому что уже показано — знание читающего сервиса: он отбирает события по created_at,
    начиная с момента, с которого его интересуют изменения по делу.
    """

    __tablename__ = "outbox_event"

    # Составной индекс — ровно форма запроса «события дела после момента X»
    # (OutboxEventRepository.list_since).
    __table_args__ = (
        Index("ix_outbox_event_case_created", "case_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Карточка, по которой обнаружено изменение. Удалили дело — уносим и его события.
    case_id: Mapped[int] = mapped_column(
        ForeignKey("case.id", ondelete="CASCADE"), index=True
    )
    # Что именно произошло: по значению клиент выбирает текст уведомления.
    event_type: Mapped[OutboxEventType] = mapped_column(
        Enum(OutboxEventType), index=True
    )
    # Суть изменения: состав полей зависит от типа (см. app/outbox.py).
    payload: Mapped[dict] = mapped_column(JSONB)
    # Когда изменение ОБНАРУЖЕНО (момент коммита обхода). Это и есть курсор читающего.
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), index=True
    )

    def __str__(self) -> str:
        return f"{self.event_type.value} по делу #{self.case_id}"
