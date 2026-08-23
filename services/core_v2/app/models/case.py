"""Карточка дела и её адреса — центр домена.

Импортирует court.py и people.py, но НЕ импортирует своих детей (события, документы,
заседания, местонахождения): связи к ним объявлены строками имён, которые SQLAlchemy
разрешает лениво через реестр. Поэтому направление импортов одностороннее и цикла нет.

Отличия от старого core (см. services/core_v2_AUDIT.md, раздел 12):

* нет monitoring_enabled — пользовательский мониторинг живёт не в core;
* нет case_link_id, связи case_link и свойств related_cases/related_case_ids —
  группы связанных дел не создавал никто, кроме ручной правки в админке;
* нет связи instances — таблицу instance не заполнял ни парсер, ни задача.

last_checked_at и last_changed_at ОСТАЛИСЬ. В старом core по первому из них планировщик
выбирал, чья очередь обходиться, но сами по себе это факты о деле: «когда мы последний
раз смотрели страницу» и «когда на ней последний раз что-то менялось». Логики выбора дел
для обхода в core_v2 нет — её напишет Django.
"""
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import UTC_DATETIME, Base
from app.models.court import Court  # noqa: F401 — нужен реестру для Case.court
from app.models.people import Judge, Side, case_judge, case_side  # noqa: F401
from app.validators import is_synthetic_uid


class Case(Base):
    """Карточка дела в конкретном суде — центральная сущность: её и синхронизируем.

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
    # Дата принятия к производству — метка «Дата принятия к производству». Пока даёт её
    # только портал Санкт-Петербурга (страницы типа D), и только у гражданских дел.
    # С receipt_date НЕ взаимоисключающая и не дублирующая: обе стоят на одной карточке
    # и расходятся, когда заявление приняли не в день поступления (например, дело
    # 78MS0124-01-2026-003108-44 — поступило 10.08.2026, принято 13.08.2026). Поэтому
    # поле отдельное: складывать её в registration_date нельзя, там по смыслу КоАП.
    accepted_date: Mapped[date | None] = mapped_column(Date)
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
    # Текущее состояние дела (необязательное). Откуда берётся, зависит от портала:
    # у судов Москвы это метка «Текущее состояние» карточки, а у мировых судов Московской
    # области такой метки нет вовсе, и состоянием служит наименование последнего события
    # «Движения дела» — в том числе того, которое ещё не получило даты и потому в таблицу
    # event не попало.
    status: Mapped[str | None] = mapped_column(String)
    # Когда запись создана в БД (значение проставляет сервер БД).
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, server_default=func.now())
    # Когда строку последний раз трогали в БД (обновляется автоматически при UPDATE).
    # Показывать это пользователю как «дата обновления дела» НЕЛЬЗЯ: строку трогает любой
    # обход (например, отметкой last_checked_at), даже когда на портале ничего не
    # изменилось. Для пользователя есть last_checked_at и last_changed_at.
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), onupdate=func.now()
    )


    # Когда карточку последний раз ХОДИЛИ проверять — проставляется на каждом успешном
    # разборе, даже если ничего не изменилось. Отличать «проверили, изменений нет» от
    # «не проверяли ни разу» нужно всякий раз, когда решают, пора ли идти на портал.
    # Само это решение принимает не core: он лишь честно записывает, когда ходил.
    last_checked_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)
    # Когда на портале последний раз что-то РЕАЛЬНО изменилось (сверка дала непустой
    # diff). Именно это пользователь и называет «дата последнего обновления дела».
    last_changed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)

    # Суд карточки — ровно один (в отличие от судей и сторон).
    court: Mapped["Court"] = relationship()

    # Справочники many-to-many: у дела их может быть несколько.
    judges: Mapped[list["Judge"]] = relationship(secondary=case_judge)
    sides: Mapped[list["Side"]] = relationship(secondary=case_side)

    @property
    def public_uid(self) -> str | None:
        """УИД дела для показа наружу: None, если ключ карточки самодельный.

        У части карточек УИД на портале нет вовсе (архивные дела движка msudrf.ru, целые
        регионы вроде Магаданской области), и ключом им служит идентификатор, посчитанный
        от ссылки, — см. synthetic_uid в app/validators.py. Наружу его отдавать нельзя:
        в поле «УИД» пользователь увидел бы техническую строку и принял бы её за настоящий
        сквозной идентификатор. Внутри же он остаётся полноценным ключом карточки.

        Отдельного поля в БД для признака не нужно: самодельный ключ помечен приставкой,
        которая заведомо не проходит формат УИД.
        """
        return None if is_synthetic_uid(self.uid) else self.uid

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
    place_history: Mapped[list["PlaceHistory"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )
    court_sessions: Mapped[list["CourtSession"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", passive_deletes=True
    )


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
    last_success_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    # created_at заодно отвечает на вопрос «когда эту ссылку впервые увидели».
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), onupdate=func.now()
    )

    case: Mapped["Case"] = relationship(back_populates="urls")

    def __str__(self) -> str:
        return self.url
