"""Инфраструктура похода на портал: пул прокси и учёт разгаданных капч.

К домену «дела и суды» отношения не имеет — это учёт того, чем и за сколько мы добывали
страницы. Держится отдельно именно поэтому.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.database import UTC_DATETIME, Base


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
    # До каких порталов этот адрес доходит: ключи из SITE_PROBES (app/courts/site_probe.py) —
    # mos-sud, msudrf, spb. Годность у прокси РАЗНАЯ: провайдер может резать CONNECT на
    # одни домены и пропускать другие, поэтому это набор, а не одно значение — один и тот
    # же адрес берёт mos-sud и не берёт msudrf, а другой наоборот.
    #
    # Заполняет check_proxy.py --sites: он и так строит матрицу «прокси × портал».
    # Пустой список = годность НЕ ПРОВЕРЯЛИ (не «никуда не годится»): такой прокси пул
    # выдаёт, но в последнюю очередь — см. ProxyRepository.lease.
    portals: Mapped[list[str]] = mapped_column(
        ARRAY(String), default=list, server_default=text("'{}'::varchar[]")
    )
    # Когда прокси последний раз выдавался из пула — по этому полю идёт ротация.
    # NULL = им ещё не ходили, такой берём первым.
    last_used_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME, index=True)
    created_at: Mapped[datetime] = mapped_column(UTC_DATETIME, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), onupdate=func.now()
    )

    def __str__(self) -> str:
        # Пароль наружу не отдаём: этот текст уходит в логи и в списки админки.
        return f"{self.scheme}://{self.host}:{self.port}"


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
    requested_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    solved_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    # Сколько ждали ответа (мс).
    latency_ms: Mapped[int | None] = mapped_column()
    # Когда строка записана — по нему считаются расходы за период.
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, server_default=func.now(), index=True
    )

    def __str__(self) -> str:
        return f"{self.provider} #{self.provider_task_id}: {self.cost} {self.currency}"
