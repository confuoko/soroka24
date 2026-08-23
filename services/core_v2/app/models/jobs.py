"""Задача поиска/синхронизации — запись об одной попытке обхода.

Существует потому, что HTTP-эндпоинт обязан ответить за миллисекунды, а поход в суд
занимает 25-35 секунд с прокси и платной капчей. Это единственная наблюдаемая снаружи
ручка на асинхронную работу: клиент создаёт задачу и потом спрашивает её статус.
"""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import UTC_DATETIME, Base
from app.models.case import Case  # noqa: F401 — нужен реестру для SearchTask.case
from app.models.enums import SearchStatus


class SearchTask(Base):
    """Задача поиска/синхронизации дела: статус, попытки, результат.

    У задачи ровно один вход из двух:

    * uid — так приходят дела мировых судов Москвы: на портале есть поиск по УИД;
    * source_url — так приходят дела остальных порталов (msudrf.ru и прочие): поиска
      по УИД там нет, зато карточка доступна по прямой ссылке.

    Во втором случае УИД на момент создания задачи НЕИЗВЕСТЕН — за ним надо сходить в
    портал, а это 25-35 секунд с капчей и прокси. Поэтому эндпоинт задачу только
    создаёт, а uid дописывается уже в задаче, когда страница получена.
    """

    __tablename__ = "search_task"

    # Задача без обоих входов бессмысленна: по ней нельзя ни найти дело, ни открыть его.
    __table_args__ = (
        CheckConstraint(
            "uid IS NOT NULL OR source_url IS NOT NULL", name="ck_search_task_uid_or_url"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Искомый УИД (не unique — по одному делу может быть несколько синхронизаций).
    # Пусто, пока задачу завели по ссылке и до портала ещё не дошли.
    uid: Mapped[str | None] = mapped_column(String, index=True)
    # Прямая ссылка на карточку дела, если дело пришло ссылкой, а не УИД.
    source_url: Mapped[str | None] = mapped_column(String, index=True)
    # Текущий статус задачи.
    status: Mapped[SearchStatus] = mapped_column(
        Enum(SearchStatus), default=SearchStatus.PENDING
    )
    # Найденное/созданное дело; при удалении дела ссылка обнуляется (SET NULL).
    case_id: Mapped[int | None] = mapped_column(
        ForeignKey("case.id", ondelete="SET NULL"), index=True
    )
    # Сколько было попыток зайти на страницу.
    attempts: Mapped[int] = mapped_column(default=0)
    # HTTP-статус последнего захода на страницу (200/403/…), если известен.
    page_status: Mapped[int | None] = mapped_column()
    # Текст последней ошибки (необязательный).
    last_error: Mapped[str | None] = mapped_column(Text)
    # Когда последний раз пытались зайти на страницу.
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    # Когда задача создана и последний раз обновлялась.
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case | None"] = relationship()
