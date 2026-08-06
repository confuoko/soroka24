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
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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


# Уровень (звено) суда — по нему различаем справочники судов разных инстанций.
class CourtLevel(str, enum.Enum):
    MIRSUD = "mirsud"    # мировой суд
    GENERAL = "general"  # суд общей юрисдикции (районный/городской)
    APPEAL = "appeal"    # апелляционный
    KAS = "kas"          # кассационный


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
    # Дата поступления дела — метка «Дата поступления» (гражданские дела).
    # С registration_date взаимоисключающие: на карточке всегда ровно одна из двух.
    receipt_date: Mapped[date | None] = mapped_column(Date)
    # Дата регистрации — метка «Дата регистрации» (дела по КоАП). Раньше склеивалась
    # с receipt_date в одно поле, теперь хранится отдельно.
    registration_date: Mapped[date | None] = mapped_column(Date)
    # Дата рассмотрения дела в первой инстанции (метка «Дата рассмотрения дела в первой
    # инстанции»).
    first_instance_date: Mapped[date | None] = mapped_column(Date)
    # Решение первой инстанции строкой как есть: «Удовлетворено, 21.05.2026». Дату из неё
    # не выделяем — она совпадает с first_instance_date (проверено на всех делах, где
    # решение есть). Формат тот же, что у status.
    first_instance_decision: Mapped[str | None] = mapped_column(String)
    # Дата вступления решения в силу (метка «Дата вступления решения в силу»).
    decision_effective_date: Mapped[date | None] = mapped_column(Date)
    # Номер дела в вышестоящей инстанции, напр. «10-0014/2025» (метка «Номер дела
    # вышестоящей инстанции»). Не путать с code — это номер ДРУГОГО дела.
    superior_case_number: Mapped[str | None] = mapped_column(String)
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

    # История парсингов дела: по одной записи на КАЖДЫЙ вызов парсинга, включая
    # «изменений нет» и «сайт суда не открылся». Формат записи и дозапись —
    # в app/monitoring/parse_history.py (append_parse_entry).
    # ВАЖНО: SQLAlchemy не отслеживает мутацию списка на месте (diff_history.append(...)
    # НЕ попадёт в UPDATE). Дозаписывать только переприсваиванием всего списка.
    diff_history: Mapped[list[dict]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
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

    @property
    def related_case_ids(self) -> list[int]:
        """id других дел из той же группы — в таком виде их отдаёт API."""
        return [c.id for c in self.related_cases]


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
    # Классификационный код суда (напр. 01MS0001) — уникальный бизнес-ключ, по нему upsert.
    code: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Название суда (обязательное).
    name: Mapped[str] = mapped_column(String)
    # Уровень (звено) суда: мировой / общей юрисдикции / апелляция / кассация.
    level: Mapped[CourtLevel] = mapped_column(Enum(CourtLevel))
    # Регион (субъект РФ), к которому относится суд.
    region: Mapped[str] = mapped_column(String)
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
    # Роль ровно так, как её называет портал: «Истец», «Взыскатель», «Должник»,
    # «Привлекаемое лицо», «Подсудимый», «Обвиняемый», «Административный истец»…
    # Словарь ролей у судов открытый, поэтому храним текстом, а не enum'ом.
    # Пара (full_name, role) — ключ дедупа справочника.
    role: Mapped[str | None] = mapped_column(String)
    # Грубая классификация роли для фильтров: истец / ответчик / другое (обязательная).
    # Всё, что не истец и не ответчик, схлопывается в «Другое» — точная роль в role.
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
    # Дата события на сайте суда (обязательная): вместе с state_description образует
    # identity события, из которой считается uid (event_uid). Без даты uid не
    # вычислить, поэтому NOT NULL.
    event_date: Mapped[date] = mapped_column(Date)
    # Описание состояния (обязательное, может быть длинным).
    state_description: Mapped[str] = mapped_column(Text)
    # Название документа-основания текстом (на портале ссылок обычно нет — только имя).
    document_str: Mapped[str | None] = mapped_column(Text)
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
    # Дата изменения местонахождения на сайте суда (обязательная): вместе с
    # place_description образует identity строки, из которой считается uid
    # (place_history_uid). Без даты uid не вычислить, поэтому NOT NULL.
    place_date: Mapped[date] = mapped_column(Date)
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
    # Дата документа (обязательная): вместе с document_type и номером повторения строки на
    # странице образует identity документа, из которой считается uid (document_uid).
    # Без даты uid не вычислить, поэтому NOT NULL.
    document_date: Mapped[date] = mapped_column(Date)
    # Вид/тип документа (обязательный). Входит в identity.
    document_type: Mapped[str] = mapped_column(String)
    # Текст документа. НАМЕРЕННО не заполняется: ни текст, ни файл документа мы не храним —
    # в БД идут только метаданные (дата и вид). Колонка оставлена для совместимости.
    document_text: Mapped[str | None] = mapped_column(Text)
    # Когда документ сохранён в БД.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Когда запись последний раз обновлялась. Для документа фактически всегда равен
    # created_at: изменяемых полей у него нет (дата и вид входят в identity, текст не
    # храним). Колонка — для симметрии с остальными дочерними сущностями дела.
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="documents")

    # UniqueConstraint по (case_id, document_type, document_date) СНЯТ: портал отдаёт до 21
    # одинаковой строки за одну дату, и мы храним их все, различая номером повторения
    # внутри uid. Уникальность держит ix_document_uid.


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
    session_date: Mapped[datetime] = mapped_column(DateTime)
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
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Когда запись последний раз обновлялась (обновляется автоматически при UPDATE).
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="court_sessions")


class Proxy(Base):
    """Прокси, через который браузер ходит на портал суда.

    Пул живёт в БД, а не в переменных окружения: список меняется часто (прокси
    покупаются и протухают), и править его хочется через админку, не передеплоивая
    воркеры. Перед каждым походом за делом берётся один прокси — самый давно не
    использованный (см. ProxyRepository.lease).
    """

    __tablename__ = "proxy"

    # host+port — естественный ключ: дважды один и тот же прокси не заводим.
    __table_args__ = (UniqueConstraint("host", "port", name="uq_proxy_host_port"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # http или socks5. Внимание: Chromium не умеет socks5 с логином/паролем,
    # поэтому socks5 годится только без учётных данных.
    scheme: Mapped[str] = mapped_column(String(16), default="http")
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column()
    # Учётные данные прокси (у большинства платных они есть).
    username: Mapped[str | None] = mapped_column(String(255))
    password: Mapped[str | None] = mapped_column(String(255))
    # Ручной выключатель: выключенный прокси не попадает в выдачу пула.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), index=True
    )
    # Заметка для человека: провайдер, до какого числа оплачен и т.п.
    comment: Mapped[str | None] = mapped_column(String(255))
    # Когда прокси последний раз выдавался из пула — по этому полю идёт ротация.
    # NULL = им ещё не ходили, такой берём первым.
    last_used_at: Mapped[datetime | None] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    def __str__(self) -> str:
        # Пароль наружу не отдаём: этот текст уходит в логи и в списки админки.
        return f"{self.scheme}://{self.host}:{self.port}"


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
