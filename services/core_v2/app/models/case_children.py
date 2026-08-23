"""Строки внутри карточки: события, история местонахождения, заседания, документы.

Четыре сущности в одном модуле, потому что устроены они одинаково и синхронизируются
одинаково: у каждой детерминированный uid, посчитанный от card_key, полная сверка со
страницей (что появилось / что изменилось / что пропало) и каскадное удаление вместе с
карточкой. Читать их имеет смысл вместе.

После удаления Event.document_id между ними НЕТ НИ ОДНОЙ связи — каждая зависит только
от Case. Это единственная межродственная связь, которая была, и она ничем не
заполнялась.

Ключевая асимметрия, которую нельзя нарушить при правках: моменты (event_date,
session_date) хранятся в UTC, а в identity входят в ПОЛЕВОМ времени суда. У события в
identity входит только дата, у заседания — дата со временем. Подробнее — в докстрингах
функций *_uid в app/repositories/.
"""
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import UTC_DATETIME, Base
from app.models.case import Case  # noqa: F401 — нужен реестру для back_populates


class Event(Base):
    """Событие по делу с сайта суда — по ним детектим изменения; принадлежит делу, удаляется вместе с ним."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Стабильный внешний идентификатор (событий много) — генерируем сами.
    uid: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=uuid.uuid4, unique=True, index=True
    )
    # Дело-владелец; при удалении дела событие удаляется (CASCADE).
    case_id: Mapped[int] = mapped_column(
        ForeignKey("case.id", ondelete="CASCADE"), index=True
    )
    # Момент события (обязательный). Время у порталов есть не всегда: у msudrf и СПб
    # колонка «Время события» есть, у Москвы её нет вовсе — там время равно местной
    # полуночи. Отличить «время неизвестно» от «событие в 00:00» по этому полю нельзя,
    # и это осознанный размен: так же устроено разбор заседаний (_parse_datetime).
    #
    # В identity события (event_uid) входит только ДАТА этого момента в поясе суда, без
    # времени: перенос заседания на час должен приезжать обновлением той же строки, а не
    # новым событием. Подробнее — в докстринге event_uid.
    event_date: Mapped[datetime] = mapped_column(UTC_DATETIME)
    # Описание состояния (обязательное, может быть длинным).
    state_description: Mapped[str] = mapped_column(Text)
    # Название документа-основания текстом (на портале ссылок обычно нет — только имя).
    document_str: Mapped[str | None] = mapped_column(Text)
    # Дата публикации события на портале (метка «Дата размещения», страницы типа B).
    # В identity НЕ входит: портал проставляет её позже самого события и потом правит,
    # а от изменения identity уехали бы uid всех уже сохранённых событий.
    published_at: Mapped[date | None] = mapped_column(Date)
    # Когда событие сохранено в БД.
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, server_default=func.now())
    # Когда запись последний раз обновлялась (обновляется автоматически при UPDATE).
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="events")


class PlaceHistory(Base):
    """Запись истории местонахождения дела; принадлежит делу, удаляется вместе с ним."""

    __tablename__ = "place_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Стабильный внешний идентификатор (записей много) — генерируем сами.
    uid: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=uuid.uuid4, unique=True, index=True
    )
    # Дело-владелец; при удалении дела запись удаляется (CASCADE).
    case_id: Mapped[int] = mapped_column(
        ForeignKey("case.id", ondelete="CASCADE"), index=True
    )
    # Дата изменения местонахождения на сайте суда (обязательная): вместе с
    # place_description образует identity строки, из которой считается uid
    # (place_history_uid). Без даты uid не вычислить, поэтому NOT NULL.
    place_date: Mapped[date] = mapped_column(Date)
    # Описание местонахождения (обязательное).
    place_description: Mapped[str] = mapped_column(Text)
    # Комментарий (необязательный).
    comment: Mapped[str | None] = mapped_column(Text)
    # Когда запись сохранена в БД.
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, server_default=func.now())
    # Когда запись последний раз обновлялась (обновляется автоматически при UPDATE).
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="place_history")


class CourtSession(Base):
    """Судебное заседание по делу; принадлежит делу, удаляется вместе с ним."""

    __tablename__ = "court_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Стабильный внешний идентификатор (заседаний много) — генерируем сами.
    uid: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=uuid.uuid4, unique=True, index=True
    )
    # Дело-владелец; при удалении дела заседание удаляется (CASCADE).
    case_id: Mapped[int] = mapped_column(
        ForeignKey("case.id", ondelete="CASCADE"), index=True
    )
    # Дата и ВРЕМЯ заседания (обязательные): портал отдаёт их одной колонкой
    # («30.07.2026 16:50»), и вместе со stage они образуют identity заседания, из которой
    # считается uid (court_session_uid). Без даты uid не вычислить, поэтому NOT NULL.
    session_date: Mapped[datetime] = mapped_column(UTC_DATETIME)
    # Место проведения — «зал» портала: номер участка и адрес (необязательное, изменяемое).
    place: Mapped[str | None] = mapped_column(String)
    # Стадия заседания (обязательная): «Судебное заседание», «Беседа». Входит в identity.
    stage: Mapped[str] = mapped_column(String)
    # Результат заседания (необязательный, изменяемый): у будущего заседания его ещё нет,
    # потом появляется «Отложено» / «Рассмотрение завершено».
    result: Mapped[str | None] = mapped_column(String)
    # Основание (необязательное, изменяемое): например «Неявка подсудимого» при отложении.
    basis: Mapped[str | None] = mapped_column(String)
    # Когда заседание сохранено в БД.
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, server_default=func.now())
    # Когда запись последний раз обновлялась (обновляется автоматически при UPDATE).
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="court_sessions")


class Document(Base):
    """Документ по делу. Удаляется вместе с делом; на него могут ссылаться события."""

    __tablename__ = "document"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Стабильный внешний идентификатор — генерируем сами.
    uid: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=uuid.uuid4, unique=True, index=True
    )
    # Дело-владелец; при удалении дела документ удаляется (CASCADE).
    case_id: Mapped[int] = mapped_column(
        ForeignKey("case.id", ondelete="CASCADE"), index=True
    )
    # Дата документа (обязательная): вместе с document_type и номером повторения строки на
    # странице образует identity документа, из которой считается uid (document_uid).
    # Без даты uid не вычислить, поэтому NOT NULL.
    document_date: Mapped[date] = mapped_column(Date)
    # Вид/тип документа (обязательный). Входит в identity.
    document_type: Mapped[str] = mapped_column(String)
    # Когда документ сохранён в БД.
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, server_default=func.now())
    # Когда запись последний раз обновлялась. Для документа фактически всегда равен
    # created_at: изменяемых полей у него нет (дата и вид входят в identity, текст не
    # храним). Колонка — для симметрии с остальными дочерними сущностями дела.
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="documents")

    # UniqueConstraint по (case_id, document_type, document_date) СНЯТ: портал отдаёт до 21
    # одинаковой строки за одну дату, и мы храним их все, различая номером повторения
    # внутри uid. Уникальность держит ix_document_uid.
