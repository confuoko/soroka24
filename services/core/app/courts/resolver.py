"""Выбор клиента суда: по УИД дела или по ссылке на его карточку.

Речь именно о КЛИЕНТЕ — о том, каким кодом идти на портал. Сам суд карточки определяется
не здесь, а по справочнику (app/repositories/courts.py): по номеру участка из таблицы
результатов либо по хосту ссылки.

Два способа, потому что порталы устроены по-разному:

* по УИД — там, где на портале есть поиск по нему: мировые суды Москвы (mos-sud.ru);
* по ссылке — там, где поиска нет, зато карточка открывается по прямому адресу:
  мировые суды Московской области (*.mo.msudrf.ru), Алтайского края (*.alt.msudrf.ru),
  Амурской области (*.amr.msudrf.ru), Архангельской области с Ненецким АО
  (*.arh.msudrf.ru) и Астраханской области (*.ast.msudrf.ru).

Больше пока ничего: остальные регионы либо на других движках, либо на том же msudrf.ru,
но с непроверенной разметкой. Чтобы добавить регион — допиши строку в COURT_BY_PREFIX
(префикс УИД -> клиент) или в COURT_BY_DOMAIN (домен портала -> клиент).
"""
from urllib.parse import urlsplit

from app.browser import ProxySettings
from app.captcha import AttemptSink
from app.courts.base import CourtClient, UnsupportedCourt
from app.courts.moscow_mir_court import MoscowMirCourtClient
from app.courts.msudrf_court import (
    ALT_DOMAIN,
    AMR_DOMAIN,
    ARH_DOMAIN,
    AST_DOMAIN,
    MO_DOMAIN,
)
from app.courts.msudrf_court import MsudrfCourtClient

# Соответствие: префикс УИД -> класс клиента суда.
# Здесь только порталы с поиском по УИД. Для остальных регионов дело можно завести
# лишь ссылкой, поэтому их префиксов тут нет — и это осознанно.
COURT_BY_PREFIX = {
    "77MS": MoscowMirCourtClient,  # мировые суды города Москвы
}

# Соответствие: домен портала -> класс клиента суда. Совпадение по концу имени хоста,
# поэтому одна строка накрывает все поддомены региона (95.mo.msudrf.ru, 148.mo.msudrf.ru).
COURT_BY_DOMAIN = {
    MO_DOMAIN: MsudrfCourtClient,  # 374 мировых суда Московской области
    ALT_DOMAIN: MsudrfCourtClient,  # 143 мировых суда Алтайского края
    AMR_DOMAIN: MsudrfCourtClient,  # 49 мировых судов Амурской области
    ARH_DOMAIN: MsudrfCourtClient,  # 72 суда Архангельской области и Ненецкого АО
    AST_DOMAIN: MsudrfCourtClient,  # 53 мировых суда Астраханской области
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
