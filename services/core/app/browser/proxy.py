"""Пул прокси: описание одного прокси и аренда прокси из БД перед походом в суд.

Сам поход через прокси устроен не напрямую: Chromium в наши прокси ходить не умеет,
между ним и прокси стоит локальный релей — см. app/browser/relay.py.
"""
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

from app.config import COURT_PROXY_REQUIRED
from app.models.database import session_scope
from app.repositories.proxies import ProxyRepository

logger = logging.getLogger(__name__)


class ProxyUnavailable(RuntimeError):
    """Пул пуст (или все прокси выключены), а COURT_PROXY_REQUIRED=1.

    Ходить на портал напрямую запрещено, поэтому браузер даже не запускаем.
    Ошибка временная: прокси могут включить обратно, и следующая попытка пройдёт.
    """


@dataclass(frozen=True)
class ProxySettings:
    """Настройки одного прокси в виде, не зависящем от сессии БД.

    Наружу из lease_proxy() отдаётся именно этот dataclass, а не ORM-модель: сессия
    к тому моменту уже закрыта, и таскать по коду отвязанный объект не хочется.
    """

    scheme: str
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    # Прокси выбран человеком (флаг --proxy у скриптов), а не выдан пулом. Клиент суда,
    # за которым закреплён свой прокси, такой выбор не перебивает: иначе флаг молча
    # игнорировался бы, и отладить конкретный адрес было бы нечем.
    explicit: bool = False

    @property
    def server(self) -> str:
        """Адрес прокси без учётных данных."""
        return f"{self.scheme}://{self.host}:{self.port}"

    def __str__(self) -> str:
        # Пароль наружу не отдаём: этот текст уходит в логи.
        return self.server


def parse_proxy_url(url: str) -> ProxySettings:
    """Разобрать строку вида http://user:pass@host:port в ProxySettings.

    Нужна скрипту scripts/check_proxy.py: провайдеры отдают прокси именно строкой.
    """
    parsed = urlparse(url)
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"В строке прокси нет хоста или порта: {url}")
    return ProxySettings(
        scheme=parsed.scheme or "http",
        host=parsed.hostname,
        port=parsed.port,
        # unquote — в пароле могут быть %-экранированные символы.
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        # Строку прокси передают руками, значит это осознанный выбор.
        explicit=True,
    )


def lease_proxy() -> Optional[ProxySettings]:
    """Взять прокси из пула в БД перед походом в суд.

    Транзакция короткая (только выбор строки и отметка времени) и закрывается ДО
    запуска браузера — блокировку строки на время сетевой работы не держим.

    None — ходим напрямую; это разрешено только при COURT_PROXY_REQUIRED=0.
    """
    with session_scope() as session:
        proxy = ProxyRepository(session).lease()
        if proxy is None:
            if COURT_PROXY_REQUIRED:
                raise ProxyUnavailable(
                    "Пул прокси пуст (нет включённых прокси в таблице proxy), "
                    "а COURT_PROXY_REQUIRED=1 — идти на портал напрямую запрещено"
                )
            logger.warning("Пул прокси пуст, идём на портал напрямую")
            return None
        # Забираем значения ДО выхода из session_scope — дальше объект отвязан.
        return ProxySettings(
            scheme=proxy.scheme,
            host=proxy.host,
            port=proxy.port,
            username=proxy.username,
            password=proxy.password,
        )


def lease_pinned_proxy(proxy_id: int) -> Optional[ProxySettings]:
    """Взять из пула закреплённый за порталом прокси по его id в таблице proxy.

    Временная мера для порталов, до которых доходит не всякий адрес: пул выбирает по
    LRU и про сайты ничего не знает, поэтому обычный lease() выдаёт годный прокси лишь
    иногда, а остальные заходы сгорают в ретраях. Правильное решение — учить пул
    выбирать по адресу назначения, и тогда эта функция уходит.

    Берём именно ID, а не строку подключения: логин и пароль остаются в БД и не
    попадают в исходники (см. докстринг app/browser/relay.py).

    None — прокси с таким id нет или он выключен в админке. Исключение НЕ бросаем:
    вызывающий откатывается на обычную аренду, и портал остаётся рабочим, пока в пуле
    есть хоть что-то подходящее.
    """
    with session_scope() as session:
        proxy = ProxyRepository(session).lease_by_id(proxy_id)
        if proxy is None:
            logger.warning(
                "Закреплённый прокси id=%s недоступен (нет в пуле или выключен) — "
                "берём обычный из пула",
                proxy_id,
            )
            return None
        return ProxySettings(
            scheme=proxy.scheme,
            host=proxy.host,
            port=proxy.port,
            username=proxy.username,
            password=proxy.password,
        )
