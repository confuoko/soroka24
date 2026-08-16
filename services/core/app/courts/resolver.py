"""Выбор клиента суда: по УИД дела или по ссылке на его карточку.

Речь именно о КЛИЕНТЕ — о том, каким кодом идти на портал. Сам суд карточки определяется
не здесь, а по справочнику (app/repositories/courts.py): по номеру участка из таблицы
результатов либо по хосту ссылки.

Два способа, потому что порталы устроены по-разному:

* по УИД — там, где на портале есть поиск по нему: мировые суды Москвы (mos-sud.ru);
* по ссылке — там, где поиска нет, зато карточка открывается по прямому адресу:
  мировые суды на движке msudrf.ru и мировые суды Санкт-Петербурга (mirsud.spb.ru);
  какие регионы подключены — видно по COURT_BY_DOMAIN ниже, это единственный источник
  списка.

Больше пока ничего: остальные регионы либо на других движках, либо на том же msudrf.ru,
но с непроверенной разметкой. Чтобы добавить регион — допиши строку в COURT_BY_PREFIX
(префикс УИД -> клиент) или в COURT_BY_DOMAIN (домен портала -> клиент).

Петербург стоит особняком: там один хост на все 211 судов региона, поэтому суд
определяется не по нему, а по номеру участка в пути ссылки — см.
CourtRepository.get_by_url.

Тип страницы здесь НЕ выбирается: резолвер отображает хост сразу в класс клиента, а тип
у клиента — константа класса (CourtClient.page_type), по которой парсер достаётся из
реестра. Поэтому регион движка с другой разметкой подключается не развилкой внутри
клиента, а своим классом: у msudrf.ru таких два — MsudrfCourtClient (тип B) и
MsudrfTypeCCourtClient (тип C, парсер ещё не написан, доменов на него пока не отображено).
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
    BLG_DOMAIN,
    EAO_DOMAIN,
    IRK_DOMAIN,
    IWN_DOMAIN,
    KBR_DOMAIN,
    KCHR_DOMAIN,
    KIR_DOMAIN,
    KLG_DOMAIN,
    KLN_DOMAIN,
    KMCH_DOMAIN,
    KMR_DOMAIN,
    KRD_DOMAIN,
    KRG_DOMAIN,
    KST_DOMAIN,
    LO_DOMAIN,
    LPK_DOMAIN,
    MO_DOMAIN,
    VLD_DOMAIN,
    VOL_DOMAIN,
    VRN_DOMAIN,
    ZBK_DOMAIN,
)
from app.courts.msudrf_court import MsudrfCourtClient
from app.courts.spb_mir_court import DOMAIN as SPB_DOMAIN
from app.courts.spb_mir_court import SpbMirCourtClient

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
    BLG_DOMAIN: MsudrfCourtClient,  # 80 мировых судов Белгородской области
    VOL_DOMAIN: MsudrfCourtClient,  # 145 мировых судов Волгоградской области
    VLD_DOMAIN: MsudrfCourtClient,  # 68 мировых судов Вологодской области
    VRN_DOMAIN: MsudrfCourtClient,  # 117 мировых судов Воронежской области
    # 12 мировых судов Еврейской автономной области. Разметку карточки здесь ещё НЕ
    # смотрели (портал встретил капчей) — если она окажется второй разметкой движка,
    # регион надо будет перевести на MsudrfTypeCCourtClient, когда напишут парсер типа C.
    EAO_DOMAIN: MsudrfCourtClient,
    ZBK_DOMAIN: MsudrfCourtClient,  # 68 мировых судов Забайкальского края
    IWN_DOMAIN: MsudrfCourtClient,  # 62 мировых суда Ивановской области
    IRK_DOMAIN: MsudrfCourtClient,  # 135 мировых судов Иркутской области
    KBR_DOMAIN: MsudrfCourtClient,  # 50 мировых судов Кабардино-Балкарской Республики
    KLN_DOMAIN: MsudrfCourtClient,  # 50 мировых судов Калининградской области
    KLG_DOMAIN: MsudrfCourtClient,  # 55 мировых судов Калужской области
    KMCH_DOMAIN: MsudrfCourtClient,  # 37 мировых судов Камчатского края
    KCHR_DOMAIN: MsudrfCourtClient,  # 26 мировых судов Карачаево-Черкесской Республики
    KMR_DOMAIN: MsudrfCourtClient,  # 147 мировых судов Кемеровской области — Кузбасса
    KIR_DOMAIN: MsudrfCourtClient,  # 80 мировых судов Кировской области
    KST_DOMAIN: MsudrfCourtClient,  # 49 мировых судов Костромской области
    KRD_DOMAIN: MsudrfCourtClient,  # 270 мировых судов Краснодарского края
    KRG_DOMAIN: MsudrfCourtClient,  # 53 мировых суда Курганской области
    LO_DOMAIN: MsudrfCourtClient,  # 87 мировых судов Ленинградской области
    LPK_DOMAIN: MsudrfCourtClient,  # 64 мировых суда Липецкой области
    # Магаданской области здесь НЕТ намеренно: у неё вторая разметка движка (тип C) и,
    # что важнее, на карточках нет УИД — сохранять дело было бы нечем. Подробности и
    # проверка — в комментарии к MAG_DOMAIN (app/courts/msudrf_court.py).
    # 211 мировых судов Санкт-Петербурга. Отдельный движок и единственный хост на весь
    # регион: суд определяется не по нему, а по номеру участка в пути ссылки
    # (CourtRepository.get_by_url).
    SPB_DOMAIN: SpbMirCourtClient,
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
