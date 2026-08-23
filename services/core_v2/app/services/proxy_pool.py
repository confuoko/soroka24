"""Аренда прокси из пула перед походом на портал суда.

Лежит здесь, а не в app/browser, потому что открывает сессию БД. Пакет браузера про
базу знать не должен: в старом core эта функция жила рядом с ProxySettings и тянула за
собой app/repositories, из-за чего возникал цикл импортов.

Аренда — не забота клиента суда: клиент получает готовый ProxySettings аргументом.
Решение «каким прокси идти» принимает тот, кто запускает поход.
"""
import logging
from typing import Optional

from app.browser.proxy import ProxySettings
from app.config import COURT_PROXY_REQUIRED
from app.database import session_scope
from app.repositories.proxies import ProxyRepository

logger = logging.getLogger(__name__)


class ProxyUnavailable(RuntimeError):
    """Пул пуст (или все прокси выключены), а COURT_PROXY_REQUIRED=1.

    Ходить на портал напрямую запрещено, поэтому браузер даже не запускаем.
    Ошибка временная: прокси могут включить обратно, и следующая попытка пройдёт.
    """


def lease_proxy(portal: Optional[str] = None) -> Optional[ProxySettings]:
    """Взять прокси из пула в БД перед походом в суд.

    portal — куда собираемся идти (mos-sud / msudrf / spb). Пул отдаст адрес, который до
    этого портала доходит: провайдеры режут CONNECT выборочно, и прокси, берущий
    mos-sud, до msudrf может не дойти. None — портал не определён, фильтра нет.

    Транзакция короткая (только выбор строки и отметка времени) и закрывается ДО
    запуска браузера — блокировку строки на время сетевой работы не держим.

    None — ходим напрямую; это разрешено только при COURT_PROXY_REQUIRED=0.
    """
    with session_scope() as session:
        proxy = ProxyRepository(session).lease(portal=portal)
        if proxy is None:
            # Пул может быть не пуст вовсе: до этого портала просто не доходит ни один
            # адрес. Отличать важно — лечится это по-разному (докупить прокси против
            # прогнать check_proxy.py --sites и заполнить portals).
            where = f" для портала {portal}" if portal else ""
            if COURT_PROXY_REQUIRED:
                raise ProxyUnavailable(
                    f"В пуле нет подходящего прокси{where} (таблица proxy), "
                    "а COURT_PROXY_REQUIRED=1 — идти на портал напрямую запрещено"
                )
            logger.warning("В пуле нет подходящего прокси%s, идём напрямую", where)
            return None
        # Забираем значения ДО выхода из session_scope — дальше объект отвязан.
        return ProxySettings(
            scheme=proxy.scheme,
            host=proxy.host,
            port=proxy.port,
            username=proxy.username,
            password=proxy.password,
        )
