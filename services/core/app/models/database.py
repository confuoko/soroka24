"""Слой доступа к данным core: подключение к БД и ORM-модели домена «дела и суды».

Всё в одном файле намеренно: движок, сессия, Base и модели рядом — так проще читать,
пока моделей немного. Стиль — SQLAlchemy 2.0 (типизированные Mapped[]).
"""
import enum
import uuid
from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    Enum,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.config import DATABASE_URL

# --- Подключение к БД ---------------------------------------------------------

# engine — единая точка соединения с PostgreSQL (внутри держит пул соединений).
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# SessionLocal() открывает новую сессию — через неё делаем запросы и коммитим.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope():
    """Сессия БД как контекст-менеджер: commit при успехе, rollback при ошибке."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Base — общий предок всех моделей; хранит метаданные таблиц (их читает Alembic).
class Base(DeclarativeBase):
    pass


# --- Перечисления -------------------------------------------------------------

# Тип стороны по делу (аналог Django choices).
class SideType(str, enum.Enum):
    PLAINTIFF = "Истец"
    DEFENDANT = "Ответчик"
    OTHER = "Другое"


# Статус задачи поиска/синхронизации дела по УИД.
class SearchStatus(str, enum.Enum):
    PENDING = "pending"    # создана, ждёт обработки
    RUNNING = "running"    # выполняется
    SUCCESS = "success"    # дело найдено и сохранено
    FAILED = "failed"      # не удалось (после всех попыток)


# --- Связующие таблицы many-to-many ------------------------------------------
# Таблицы-связки нужны для связи «многие-ко-многим»: у дела много судов/судей/сторон, а каждый из них — во многих делах.
# ondelete="CASCADE" на обоих концах => удаление любого конца стирает только строку-связь, дело и справочник живут.

case_court = Table(
    "case_court",
    Base.metadata,
    Column("case_id", ForeignKey("case.id", ondelete="CASCADE"), primary_key=True),
    Column("court_id", ForeignKey("court.id", ondelete="CASCADE"), primary_key=True),
)

case_judge = Table(
    "case_judge",
    Base.metadata,
    Column("case_id", ForeignKey("case.id", ondelete="CASCADE"), primary_key=True),
    Column("judge_id", ForeignKey("judge.id", ondelete="CASCADE"), primary_key=True),
)

case_side = Table(
    "case_side",
    Base.metadata,
    Column("case_id", ForeignKey("case.id", ondelete="CASCADE"), primary_key=True),
    Column("side_id", ForeignKey("side.id", ondelete="CASCADE"), primary_key=True),
)


# --- Модели -------------------------------------------------------------------


class Case(Base):
    """Судебное дело — центральная сущность: его парсим, мониторим."""

    __tablename__ = "case"

    # Порядковый уникальный номер записи (первичный ключ).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # УИД дела с сайта суда (например, 77MS0466-01-2026-003751-93) — уникальный бизнес-ключ.
    uid: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Ссылка на дело на сайте суда; уникальна, но может отсутствовать (у части дел ссылки нет).
    url: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    # Код дела: необязательный, может повторяться у разных дел (потому без unique).
    code: Mapped[str | None] = mapped_column(String)
    # Номер заявления (необязательный).
    application_number: Mapped[str | None] = mapped_column(String)
    # Номер входящего документа (необязательный).
    incoming_number: Mapped[str | None] = mapped_column(String)
    # Дата поступления дела (необязательная).
    receipt_date: Mapped[date | None] = mapped_column(Date)
    # Категория дела (необязательная).
    category: Mapped[str | None] = mapped_column(String)
    # Текущее состояние дела (необязательное).
    status: Mapped[str | None] = mapped_column(String)
    # Когда запись создана в БД (значение проставляет сервер БД).
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Когда запись последний раз обновлялась (обновляется автоматически при UPDATE).
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    # Группа связанных дел; при удалении группы поле обнуляется (SET NULL), дело живёт.
    case_link_id: Mapped[int | None] = mapped_column(
        ForeignKey("case_link.id", ondelete="SET NULL"), index=True
    )

    # Справочники many-to-many: у дела их может быть несколько.
    courts: Mapped[list["Court"]] = relationship(secondary=case_court)
    judges: Mapped[list["Judge"]] = relationship(secondary=case_judge)
    sides: Mapped[list["Side"]] = relationship(secondary=case_side)

    # Группа, в которую входит дело (все дела группы связаны между собой).
    case_link: Mapped["CaseLink | None"] = relationship(back_populates="cases")

    # Дочерние записи: удаляются вместе с делом (CASCADE + очистка «сирот»).
    events: Mapped[list["Event"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )
    instances: Mapped[list["Instance"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )
    place_history: Mapped[list["PlaceHistory"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )
    court_sessions: Mapped[list["CourtSession"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def related_cases(self) -> list["Case"]:
        """Другие дела из той же группы (пустой список, если группа не задана)."""
        if self.case_link is None:
            return []
        return [c for c in self.case_link.cases if c is not self]


class CaseLink(Base):
    """Группа связанных дел: хранит список дел, которые считаются связанными между собой."""

    __tablename__ = "case_link"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Когда группа создана в БД.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Дела, входящие в группу (все они связаны друг с другом).
    cases: Mapped[list["Case"]] = relationship(
        back_populates="case_link", passive_deletes=True
    )


class Court(Base):
    """Суд-справочник: общий для многих дел; поля предварительные, уточним на этапе парсеров."""

    __tablename__ = "court"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Название суда (обязательное).
    name: Mapped[str] = mapped_column(String)
    # Зона/регион суда (необязательное).
    zone: Mapped[str | None] = mapped_column(String)
    # Тип страницы суда — по нему выбирается парсер (необязательное).
    parser_type: Mapped[str | None] = mapped_column(String)
    # Базовый URL сайта суда (необязательное).
    base_url: Mapped[str | None] = mapped_column(String)


class Judge(Base):
    """Судья-справочник: общий для многих дел (у дела может быть несколько судей)."""

    __tablename__ = "judge"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ФИО судьи одной строкой (обязательное).
    full_name: Mapped[str] = mapped_column(String)


class Side(Base):
    """Сторона-справочник (истец/ответчик/другое): общая для многих дел."""

    __tablename__ = "side"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ФИО/название стороны (обязательное).
    full_name: Mapped[str] = mapped_column(String)
    # Тип стороны: истец / ответчик / другое (обязательное).
    type: Mapped[SideType] = mapped_column(Enum(SideType))


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
    # Дата события на сайте суда (необязательная).
    event_date: Mapped[date | None] = mapped_column(Date)
    # Описание состояния (обязательное, может быть длинным).
    state_description: Mapped[str] = mapped_column(Text)
    # Необязательная ссылка на документ; при удалении документа — обнуляется (SET NULL).
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL")
    )
    # Когда событие сохранено в БД.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Когда запись последний раз обновлялась (обновляется автоматически при UPDATE).
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="events")
    document: Mapped["Document | None"] = relationship()


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
    # Дата изменения местонахождения на сайте суда (необязательная).
    place_date: Mapped[date | None] = mapped_column(Date)
    # Описание местонахождения (обязательное).
    place_description: Mapped[str] = mapped_column(Text)
    # Комментарий (необязательный).
    comment: Mapped[str | None] = mapped_column(Text)
    # Когда запись сохранена в БД.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Когда запись последний раз обновлялась (обновляется автоматически при UPDATE).
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="place_history")


class Instance(Base):
    """Инстанция, через которую прошло дело (номер уникален внутри дела); удаляется вместе с делом."""

    __tablename__ = "instance"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Стабильный внешний идентификатор — генерируем сами.
    uid: Mapped[uuid.UUID] = mapped_column(
        Uuid, default=uuid.uuid4, unique=True, index=True
    )
    # Дело-владелец; при удалении дела инстанция удаляется (CASCADE).
    case_id: Mapped[int] = mapped_column(
        ForeignKey("case.id", ondelete="CASCADE"), index=True
    )
    # Номер инстанции (обязательный); уникален внутри дела (см. __table_args__).
    instance_number: Mapped[str] = mapped_column(String)

    case: Mapped["Case"] = relationship(back_populates="instances")

    # Номер инстанции уникален в рамках одного дела, но повторяем в разных делах.
    __table_args__ = (UniqueConstraint("case_id", "instance_number"),)


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
    # Дата документа (необязательная).
    document_date: Mapped[date | None] = mapped_column(Date)
    # Вид/тип документа (обязательный).
    document_type: Mapped[str] = mapped_column(String)
    # Текст документа (необязательный, может быть очень длинным).
    document_text: Mapped[str | None] = mapped_column(Text)
    # Когда документ сохранён в БД.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    case: Mapped["Case"] = relationship(back_populates="documents")

    # В рамках одного дела пара «вид документа + дата» уникальна.
    __table_args__ = (UniqueConstraint("case_id", "document_type", "document_date"),)


class CourtSession(Base):
    """Судебное заседание по делу; принадлежит делу, удаляется вместе с ним."""

    __tablename__ = "court_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Дело-владелец; при удалении дела заседание удаляется (CASCADE).
    case_id: Mapped[int] = mapped_column(
        ForeignKey("case.id", ondelete="CASCADE"), index=True
    )
    # Дата заседания (обязательная).
    session_date: Mapped[date] = mapped_column(Date)
    # Место проведения (необязательное).
    place: Mapped[str | None] = mapped_column(String)
    # Стадия заседания (обязательная).
    stage: Mapped[str] = mapped_column(String)
    # Результат заседания (необязательный).
    result: Mapped[str | None] = mapped_column(String)
    # Основание (необязательное).
    basis: Mapped[str | None] = mapped_column(String)

    case: Mapped["Case"] = relationship(back_populates="court_sessions")


class SearchTask(Base):
    """Задача поиска/синхронизации дела по УИД: статус, попытки, результат."""

    __tablename__ = "search_task"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Искомый УИД (не unique — по одному делу может быть несколько синхронизаций).
    uid: Mapped[str] = mapped_column(String, index=True)
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
    last_attempt_at: Mapped[datetime | None] = mapped_column()
    # Когда задача создана и последний раз обновлялась.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case | None"] = relationship()
