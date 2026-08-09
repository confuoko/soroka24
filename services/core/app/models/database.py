"""Слой доступа к данным core: подключение к БД и ORM-модели домена «дела и суды».

Всё в одном файле намеренно: движок, сессия, Base и модели рядом — так проще читать,
пока моделей немного. Стиль — SQLAlchemy 2.0 (типизированные Mapped[]).
"""
import enum
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
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
# Таблицы-связки нужны для связи «многие-ко-многим»: у дела много судей/сторон, а каждый из них — во многих делах.
# ondelete="CASCADE" на обоих концах => удаление любого конца стирает только строку-связь, дело и справочник живут.
# Суда в этом списке нет: у карточки он ровно один и хранится обычным внешним ключом.

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
    """Карточка дела в конкретном суде — центральная сущность: её парсим и мониторим.

    Единица учёта — не «дело вообще», а его КАРТОЧКА: тройка «УИД + суд + номер дела».

    Суд в ключе потому, что УИД сквозной и не меняется, когда дело идёт по инстанциям:
    один и тот же УИД встречается на странице участка мирового судьи и на странице
    районного суда — это разные карточки с разным содержимым.

    Номер дела — потому что по одному УИД в одном суде тоже бывает несколько дел:
    приказное производство, его отмена, затем исковое. Без номера в ключе они затирали
    друг друга при каждом обходе.

    Ссылок на одну карточку может вести несколько (http/https, другой порядок параметров,
    сменившийся адрес) — они лежат в CaseUrl.
    """

    __tablename__ = "case"

    # Карточка — это тройка «УИД + суд + номер дела».
    __table_args__ = (
        UniqueConstraint("uid", "court_id", "code", name="uq_case_uid_court_code"),
    )

    # Порядковый уникальный номер записи (первичный ключ).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # УИД дела с сайта суда (например, 77MS0466-01-2026-003751-93). Сам по себе НЕ
    # уникален — уникальна тройка (см. __table_args__).
    uid: Mapped[str] = mapped_column(String, index=True)
    # Суд, которому принадлежит карточка. Обязателен: карточки без суда не бывает.
    # Берётся НЕ из УИД, а из того же источника, откуда пришло дело: из номера участка в
    # таблице результатов поиска либо из хоста ссылки (см. app/monitoring/tasks.py).
    # ondelete намеренно не задан: суд из справочника не удаляют, а если удалят —
    # пусть операция упадёт, чем карточка молча останется без суда.
    court_id: Mapped[int] = mapped_column(ForeignKey("court.id"), index=True)
    # Номер дела ~ материала (например, «05-0444/1/2026» или «М-2342/463/2026»).
    # Часть ключа карточки, поэтому обязателен и НЕ обновляется при обходе: изменившийся
    # номер означает другую карточку, а не изменение этой. Источник — первый столбец
    # таблицы результатов поиска, а не карточка: на карточке он приходит под разными
    # метками («Номер дела» у КоАП, «Номер заявления» у гражданских) и потому не всегда.
    code: Mapped[str] = mapped_column(String)
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
    # Когда строку последний раз трогали в БД (обновляется автоматически при UPDATE).
    # Показывать это пользователю как «дата обновления дела» НЕЛЬЗЯ: на каждом обходе
    # дозаписывается diff_history, то есть строка обновляется всегда, даже когда на
    # портале ничего не изменилось. Для пользователя есть last_checked_at и
    # last_changed_at.
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    # Дело переобходится по расписанию (см. sync_monitored_cases в app/monitoring/tasks.py).
    # Флаг именно у КАРТОЧКИ, а не отдельная таблица подписок: кто из пользователей следит
    # за делом — знание клиентского сервиса, core про пользователей не знает вовсе.
    monitoring_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), index=True
    )
    # Когда карточку последний раз ХОДИЛИ проверять — проставляется на каждом успешном
    # разборе, даже если ничего не изменилось. По этому полю планировщик выбирает, чья
    # очередь обходиться.
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    # Когда на портале последний раз что-то РЕАЛЬНО изменилось (сверка дала непустой
    # diff). Именно это пользователь и называет «дата последнего обновления дела».
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime)

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

    # Суд карточки — ровно один (в отличие от судей и сторон).
    court: Mapped["Court"] = relationship()

    # Справочники many-to-many: у дела их может быть несколько.
    judges: Mapped[list["Judge"]] = relationship(secondary=case_judge)
    sides: Mapped[list["Side"]] = relationship(secondary=case_side)

    # Группа, в которую входит дело (все дела группы связаны между собой).
    case_link: Mapped["CaseLink | None"] = relationship(back_populates="cases")

    @property
    def card_key(self) -> str:
        """Ключ карточки строкой: «УИД | код суда | номер дела».

        От него считаются детерминированные uid дочерних строк — событий, документов,
        заседаний, местонахождений (см. app/repositories/). Именно от КАРТОЧКИ, а не от
        УИД дела: по одному УИД карточек бывает несколько (разные суды — дело шло по
        инстанциям; разные номера — приказное производство и последовавшее исковое), а
        uid дочерних строк уникален глобально. Считай мы его от одного УИД, одинаковые
        события соседних карточек столкнулись бы на UNIQUE-индексе, и вторая карточка
        просто не сохранилась бы.

        Все три части неизменны: суд и номер входят в ключ карточки и не обновляются при
        обходе, поэтому uid дочерних строк стабилен между парсингами.
        """
        return f"{self.uid}|{self.court.code}|{self.code}"

    # Дочерние записи: удаляются вместе с делом (CASCADE + очистка «сирот»).
    urls: Mapped[list["CaseUrl"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )
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


class CaseUrl(Base):
    """Адрес, по которому открывается карточка дела; удаляется вместе с карточкой.

    Почему отдельная таблица, а не поле у дела: на одну и ту же карточку ведёт несколько
    адресов — http и https, другой порядок параметров, сменившийся после переезда участка
    адрес. Пока ссылка была полем, каждая новая перезаписывала предыдущую, старая
    переставала находиться, и по ней заводилась лишняя задача с походом через капчу.

    url уникален ГЛОБАЛЬНО, а не в пределах дела: весь смысл таблицы в том, чтобы по
    присланному адресу сразу понять, какая это карточка.
    """

    __tablename__ = "case_url"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Карточка-владелец; при удалении карточки ссылка удаляется (CASCADE).
    case_id: Mapped[int] = mapped_column(
        ForeignKey("case.id", ondelete="CASCADE"), index=True
    )
    # Адрес в канонической форме (см. canonical_case_url в app/validators.py).
    url: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Когда по этому адресу последний раз удалось получить страницу. По нему выбираем,
    # какой ссылкой ходить при повторном обходе: рабочая важнее просто известной.
    last_success_at: Mapped[datetime | None] = mapped_column()
    # created_at заодно отвечает на вопрос «когда эту ссылку впервые увидели».
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="urls")

    def __str__(self) -> str:
        return self.url


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
    """Суд-справочник: общий для многих дел.

    Суд дела определяется НЕ по УИД, а по тому же источнику, откуда пришло само дело:
    у дел из поиска по УИД (мировые суды Москвы) — по номеру участка из таблицы
    результатов, у дел, пришедших ссылкой (мировые суды Московской области) — по хосту
    этой ссылки. И номер участка, и хост выводятся из полей ниже прямо в момент поиска
    (app/repositories/courts.py) — отдельных колонок под них нет намеренно: справочник
    маленький, выигрыша от индекса на нём нет, а производная колонка протухала бы при
    любой правке названия или адреса.
    """

    __tablename__ = "court"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Классификационный код суда (напр. 01MS0001) — уникальный бизнес-ключ, по нему upsert.
    # Первые 4 символа («77MS») — регион плюс звено суда: по ним поиск участка
    # ограничивается регионом.
    code: Mapped[str] = mapped_column(String, unique=True, index=True)
    # Название суда (обязательное). Из него берётся номер судебного участка
    # («Судебный участок № 235 ...»), по которому дело привязывается к суду.
    # Не путать номер участка с числом в коде: они совпадают не всегда — у 36 московских
    # судов расходятся (участок № 463 — это код 77MS0466, а 77MS0463 — другой суд).
    name: Mapped[str] = mapped_column(String)
    # Уровень (звено) суда: мировой / общей юрисдикции / апелляция / кассация.
    level: Mapped[CourtLevel] = mapped_column(Enum(CourtLevel))
    # Регион (субъект РФ), к которому относится суд.
    region: Mapped[str] = mapped_column(String)
    # Базовый URL сайта суда (необязательное). Его хост — ключ, по которому определяется
    # суд дела, пришедшего ссылкой: на msudrf.ru у каждого участка свой поддомен.
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
    last_attempt_at: Mapped[datetime | None] = mapped_column()
    # Когда задача создана и последний раз обновлялась.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case | None"] = relationship()


class CaptchaSolve(Base):
    """Одна оплаченная капча: сколько стоила, кому её списали и в рамках чего решали.

    Зачем таблица: порталы закрывают карточки дел проверкой, каждая разгадка стоит
    денег, и хочется знать цену не «в среднем за месяц», а по конкретному делу — во
    сколько обошлось его первое скачивание и во сколько обходится мониторинг. Считать
    расход постфактум нельзя: у сервиса нет фиксированного тарифа (цена плавает от
    нагрузки), поэтому стоимость каждой разгадки берётся из ответа сервиса и пишется
    сюда сразу, отдельной короткой транзакцией.

    Строк на одну задачу может быть много: один поход решает до CAPTCHA_ATTEMPTS капч,
    а сама задача ретраится, и каждая попытка стоит отдельных денег.

    Связи объявлены обычными внешними ключами, без relationship: таблица техническая,
    ходить из неё в дело незачем — её читают отчётами (СУММА по делу или по задаче).
    """

    __tablename__ = "captcha_solve"

    # Идемпотентность: id задачи у сервиса уникален, поэтому повторная запись того же
    # решения (ретрай воркера, двойной вызов колбэка) не удвоит расход в отчёте.
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_task_id", name="uq_captcha_solve_provider_task"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Сервис-решатель. Сейчас всегда rucaptcha, но платить может прийтись и другому,
    # и тогда цены надо будет различать по этому полю.
    provider: Mapped[str] = mapped_column(String(32), default="rucaptcha")
    # id задачи на стороне сервиса — по нему можно спросить у него же детали.
    provider_task_id: Mapped[int] = mapped_column(BigInteger)
    # Задача синхронизации, в рамках которой решали капчу. SET NULL: удалённая задача
    # не должна стирать историю расходов.
    search_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_task.id", ondelete="SET NULL"), index=True
    )
    # Дело, ради которого всё затевалось. Пусто в момент разгадки, если задачу завели
    # ссылкой: УИД (а значит и карточку) мы узнаём только с полученной страницы, и
    # проставляется поле уже потом (CaptchaSolveRepository.attach_case).
    case_id: Mapped[int | None] = mapped_column(
        ForeignKey("case.id", ondelete="SET NULL"), index=True
    )
    # Суд, чей портал показал проверку — для среза «какой портал дороже».
    court_id: Mapped[int | None] = mapped_column(ForeignKey("court.id"), index=True)
    # Хост участка, на котором показали капчу (в УИД он не виден).
    host: Mapped[str | None] = mapped_column(String)
    # Исход попытки: solved (ответ получен) или timeout (не дождались).
    status: Mapped[str] = mapped_column(String(16))
    # Сколько списал сервис. NULL — цена НЕИЗВЕСТНА (таймаут либо сервис не прислал
    # поле), а не ноль: в отчётах такие строки надо считать отдельно, иначе расход
    # выглядит меньше настоящего.
    cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 5))
    # Валюта баланса личного кабинета (у rucaptcha — рубли).
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    # Сколько исполнителей брались за задачу (диагностика: дорогие капчи — сложные).
    solve_count: Mapped[int | None] = mapped_column()
    # Какая это по счёту проверка за один поход браузера (1..CAPTCHA_ATTEMPTS).
    attempt_no: Mapped[int | None] = mapped_column()
    # Номер ретрая celery-задачи: по нему видно, что дело переспрашивали.
    celery_retry: Mapped[int | None] = mapped_column()
    # Картинка капчи в S3 — лежит рядом с ценой, видно, за что заплатили.
    captcha_bucket: Mapped[str | None] = mapped_column(String)
    captcha_key: Mapped[str | None] = mapped_column(String)
    # Когда отправили на распознавание и когда получили ответ.
    requested_at: Mapped[datetime | None] = mapped_column()
    solved_at: Mapped[datetime | None] = mapped_column()
    # Сколько ждали ответа (мс).
    latency_ms: Mapped[int | None] = mapped_column()
    # Когда строка записана — по нему считаются расходы за период.
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)

    def __str__(self) -> str:
        return f"{self.provider} #{self.provider_task_id}: {self.cost} {self.currency}"
