"""Определение суда: по УИД дела или по ссылке на его карточку.

Два способа, потому что порталы устроены по-разному:

* по УИД — там, где на портале есть поиск по нему (Москва, mos-sud.ru);
* по ссылке — там, где поиска нет, зато карточка открывается по прямому адресу
  (msudrf.ru и большинство региональных порталов).

Чтобы добавить новый суд — допиши строку в COURT_BY_PREFIX (префикс УИД -> клиент)
или в COURT_BY_DOMAIN (домен портала -> клиент).
"""
from urllib.parse import urlsplit

from app.browser import ProxySettings
from app.captcha import AttemptSink
from app.courts.base import CourtClient, UnsupportedCourt
from app.courts.moscow_mir_court import MoscowMirCourtClient
from app.courts.msudrf_court import DOMAIN as MSUDRF_DOMAIN
from app.courts.msudrf_court import MsudrfCourtClient

# Соответствие: префикс УИД -> класс клиента суда.
# Здесь только порталы с поиском по УИД. Для остальных регионов дело можно завести
# лишь ссылкой, поэтому их префиксов тут нет — и это осознанно.
COURT_BY_PREFIX = {
    "77MS": MoscowMirCourtClient,  # мировые суды города Москвы
}

# Соответствие: домен портала -> класс клиента суда. Совпадение по концу имени хоста,
# поэтому одна строка накрывает все поддомены (95.mo.msudrf.ru, 1.bkr.msudrf.ru, ...).
COURT_BY_DOMAIN = {
    MSUDRF_DOMAIN: MsudrfCourtClient,  # 6063 мировых суда из 72 регионов
}


def define_court_by_uid(
    uid: str,
    proxy: ProxySettings | None = None,
    headless: bool = True,
    on_captcha_attempt: AttemptSink | None = None,
) -> CourtClient:
    """Определить суд по префиксу УИД (например, 77MS -> мировые суды Москвы).

    proxy — арендованный из пула прокси, через который клиент пойдёт на портал.
    on_captcha_attempt — куда сообщать о расходах на капчу (учёт ведёт вызывающий).
    """
    # Проверяем известные префиксы и возвращаем первый подходящий клиент.
    for prefix, court_client_cls in COURT_BY_PREFIX.items():
        if uid.startswith(prefix):
            # экземпляр клиента суда
            return court_client_cls(
                proxy=proxy, headless=headless, on_captcha_attempt=on_captcha_attempt
            )
    # Ни один префикс не подошёл — по УИД такое дело не найти (возможно, его портал
    # поддержан, но только по ссылке).
    raise UnsupportedCourt(uid)


def define_court_by_url(
    url: str,
    proxy: ProxySettings | None = None,
    headless: bool = True,
    on_captcha_attempt: AttemptSink | None = None,
) -> CourtClient:
    """Определить суд по домену ссылки на карточку дела."""
    host = (urlsplit(url).hostname or "").lower()
    for domain, court_client_cls in COURT_BY_DOMAIN.items():
        # Сравниваем по границе имени, а не через `in`: иначе «msudrf.ru.evil.com»
        # тоже подошёл бы, и мы пошли бы браузером куда угодно.
        if host == domain or host.endswith(f".{domain}"):
            return court_client_cls(
                proxy=proxy, headless=headless, on_captcha_attempt=on_captcha_attempt
            )
    raise UnsupportedCourt(url)


def is_supported_url(url: str) -> bool:
    """Умеем ли мы открывать дела с этого портала? Нужно API до создания задачи."""
    try:
        define_court_by_url(url)
    except UnsupportedCourt:
        return False
    return True
