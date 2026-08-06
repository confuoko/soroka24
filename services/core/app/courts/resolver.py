"""Определение суда по УИД дела.

По префиксу УИД выбираем нужный класс-клиент суда. Чтобы добавить новый суд —
допиши строку в COURT_BY_PREFIX (префикс -> класс клиента).
"""
from app.browser import ProxySettings
from app.courts.base import CourtClient, UnsupportedCourt
from app.courts.moscow_mir_court import MoscowMirCourtClient

# Соответствие: префикс УИД -> класс клиента суда.
COURT_BY_PREFIX = {
    "77MS": MoscowMirCourtClient,  # мировые суды города Москвы
}


def define_court_by_uid(uid: str, proxy: ProxySettings | None = None) -> CourtClient:
    """Определить суд по префиксу УИД (например, 77MS -> мировые суды Москвы).

    proxy — арендованный из пула прокси, через который клиент пойдёт на портал.
    """
    # Проверяем известные префиксы и возвращаем первый подходящий клиент.
    for prefix, court_client_cls in COURT_BY_PREFIX.items():
        if uid.startswith(prefix):
            return court_client_cls(proxy=proxy)  # создаём экземпляр клиента суда
    # Ни один префикс не подошёл — такой суд пока не поддержан.
    raise UnsupportedCourt(uid)
