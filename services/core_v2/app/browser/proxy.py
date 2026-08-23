"""Как выглядит один прокси. Ни БД, ни аренды — только данные.

Аренда прокси из пула живёт в app/services/proxy_pool.py, и это не вкусовщина.
В старом core lease_proxy лежала здесь же и открывала сессию БД, из-за чего пакет
app/browser зависел от app/repositories. Это и был тот самый цикл импортов, из-за
которого в app/repositories/courts.py приходилось делать отложенный импорт внутри
функции. Здесь пакет браузера не знает про БД вовсе.

Сам поход через прокси устроен не напрямую: Chromium в наши прокси ходить не умеет,
между ним и прокси стоит локальный релей — см. app/browser/relay.py.
"""
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse


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
    )
