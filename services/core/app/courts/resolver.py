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
MsudrfTypeCCourtClient (тип C — Пермский край и Адыгея). Отображение домена на тип при этом
ОЖИДАНИЕ, а не приговор: фактическую вёрстку клиент определяет по самой странице
(detect_page_type), потому что движок общий на 72 региона и отдать может любую из двух.
"""
from urllib.parse import urlsplit

from app.browser import ProxySettings
from app.captcha import AttemptSink
from app.courts.base import CourtClient, UnsupportedCourt
from app.courts.moscow_mir_court import MoscowMirCourtClient
from app.courts.msudrf_court import (
    ALT_DOMAIN,
    ADG_DOMAIN,
    AMR_DOMAIN,
    ARH_DOMAIN,
    AST_DOMAIN,
    BKR_DOMAIN,
    BLG_DOMAIN,
    BUR_DOMAIN,
    CHAO_DOMAIN,
    CHEL_DOMAIN,
    CHV_DOMAIN,
    DAG_DOMAIN,
    HAK_DOMAIN,
    EAO_DOMAIN,
    ING_DOMAIN,
    IRK_DOMAIN,
    HBR_DOMAIN,
    IWN_DOMAIN,
    KAR_DOMAIN,
    KBR_DOMAIN,
    KCHR_DOMAIN,
    KIR_DOMAIN,
    KLG_DOMAIN,
    KLN_DOMAIN,
    KMCH_DOMAIN,
    KMR_DOMAIN,
    KOMI_DOMAIN,
    KRD_DOMAIN,
    JRS_DOMAIN,
    KRG_DOMAIN,
    KST_DOMAIN,
    LO_DOMAIN,
    LPK_DOMAIN,
    MO_DOMAIN,
    MRM_DOMAIN,
    NNOV_DOMAIN,
    NVG_DOMAIN,
    OMS_DOMAIN,
    ORL_DOMAIN,
    PERM_DOMAIN,
    PNZ_DOMAIN,
    PRM_DOMAIN,
    RALT_DOMAIN,
    RIZ_DOMAIN,
    ROS_DOMAIN,
    SAH_DOMAIN,
    SAM_DOMAIN,
    SAR_DOMAIN,
    SML_DOMAIN,
    SVD_DOMAIN,
    TMB_DOMAIN,
    TMS_DOMAIN,
    TULA_DOMAIN,
    TUVA_DOMAIN,
    TWR_DOMAIN,
    TYUM_DOMAIN,
    UDM_DOMAIN,
    ULN_DOMAIN,
    VLD_DOMAIN,
    VOL_DOMAIN,
    VRN_DOMAIN,
    YAK_DOMAIN,
    ZBK_DOMAIN,
)
from app.courts.msudrf_court import MsudrfCourtClient, MsudrfTypeCCourtClient
from app.courts.spb_mir_court import DOMAIN as SPB_DOMAIN
from app.courts.spb_mir_court import SpbMirCourtClient
from app.validators import host_variants

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
    MRM_DOMAIN: MsudrfCourtClient,  # 48 мировых судов Мурманской области
    NNOV_DOMAIN: MsudrfCourtClient,  # 179 мировых судов Нижегородской области
    NVG_DOMAIN: MsudrfCourtClient,  # 41 мировой суд Новгородской области
    OMS_DOMAIN: MsudrfCourtClient,  # 114 мировых судов Омской области
    # 48 мировых судов Орловской области. Разметка типа B, но с ДРУГИМ порядком колонок в
    # «Движении дела» — из-за этого региона парсер типа B перевели с номеров колонок на
    # поиск по шапке (см. ORL_DOMAIN в app/courts/msudrf_court.py).
    ORL_DOMAIN: MsudrfCourtClient,
    PNZ_DOMAIN: MsudrfCourtClient,  # 76 мировых судов Пензенской области
    PRM_DOMAIN: MsudrfCourtClient,  # 109 мировых судов Приморского края
    # 146 мировых судов Пермского края — ВТОРАЯ вёрстка движка, тип C. Домен отличается от
    # Приморского края выше одной буквой (perm/prm), и различает их только сверка по границе
    # имени в define_court_by_url — как у Алтая (ALT/RALT).
    PERM_DOMAIN: MsudrfTypeCCourtClient,
    # 14 мировых судов Республики Алтай. Домен отличается от Алтайского края (ALT_DOMAIN)
    # одной буквой, и различает их только точка в define_court_by_url — см. RALT_DOMAIN.
    RALT_DOMAIN: MsudrfCourtClient,
    # 24 мировых суда Республики Адыгея — тоже вторая вёрстка движка, тип C, но таблица
    # «Движения дела» на одну колонку короче, чем в Пермском крае.
    ADG_DOMAIN: MsudrfTypeCCourtClient,
    HAK_DOMAIN: MsudrfCourtClient,  # 35 мировых судов Республики Хакасия
    # 26 мировых судов Республики Тыва — вторая вёрстка движка, тип C.
    TUVA_DOMAIN: MsudrfTypeCCourtClient,
    ROS_DOMAIN: MsudrfCourtClient,  # 230 мировых судов Ростовской области
    # 70 мировых судов Рязанской области — тоже тип C.
    RIZ_DOMAIN: MsudrfTypeCCourtClient,
    SAM_DOMAIN: MsudrfCourtClient,  # 162 мировых суда Самарской области
    # 134 суда Саратовской и 219 Свердловской области подключены ВСЛЕПУЮ: живых карточек мы
    # не видели. Ожидание типа B — самое частое; фактическую вёрстку определит сама страница
    # (detect_page_type), а расхождение с ожиданием попадёт в лог.
    SAR_DOMAIN: MsudrfCourtClient,
    SVD_DOMAIN: MsudrfCourtClient,
    SAH_DOMAIN: MsudrfCourtClient,  # 33 мировых суда Сахалинской области
    SML_DOMAIN: MsudrfCourtClient,  # 56 мировых судов Смоленской области
    TMB_DOMAIN: MsudrfCourtClient,  # 60 мировых судов Тамбовской области
    # 83 мировых суда Тверской области, тоже вслепую. У 69MS0045 адрес в справочнике записан
    # склеенно (26twr.msudrf.ru) — оба написания раскрывает host_variants.
    TWR_DOMAIN: MsudrfCourtClient,
    TMS_DOMAIN: MsudrfCourtClient,  # 56 мировых судов Томской области
    TULA_DOMAIN: MsudrfCourtClient,  # 83 мировых суда Тульской области
    # 74 мировых суда Тюменской области (код 72MS). Ещё 78 судов, которые справочник
    # относит к этому же региону, — это Ханты-Мансийский АО (86MS) на портале mirsud86.ru,
    # он не поддержан вовсе. Подключено вслепую: карточек не смотрели.
    TYUM_DOMAIN: MsudrfCourtClient,
    UDM_DOMAIN: MsudrfCourtClient,  # 85 мировых судов Удмуртской Республики
    ULN_DOMAIN: MsudrfCourtClient,  # 71 мировой суд Ульяновской области
    HBR_DOMAIN: MsudrfCourtClient,  # 75 мировых судов Хабаровского края
    # 183 мировых суда Челябинской области — вторая вёрстка движка, тип C.
    CHEL_DOMAIN: MsudrfTypeCCourtClient,
    # 68 мировых судов Чувашской Республики. Подключено вслепую: карточек не смотрели.
    CHV_DOMAIN: MsudrfCourtClient,
    CHAO_DOMAIN: MsudrfCourtClient,  # 4 мировых суда Чукотского АО — самый малый регион
    JRS_DOMAIN: MsudrfCourtClient,  # 70 мировых судов Ярославской области
    BKR_DOMAIN: MsudrfCourtClient,  # 215 мировых судов Республики Башкортостан
    BUR_DOMAIN: MsudrfCourtClient,  # 54 мировых суда Республики Бурятия
    DAG_DOMAIN: MsudrfCourtClient,  # 131 мировой суд Республики Дагестан
    KAR_DOMAIN: MsudrfCourtClient,  # 38 мировых судов Республики Карелия
    KOMI_DOMAIN: MsudrfCourtClient,  # 60 мировых судов Республики Коми
    # 23 мировых суда Республики Ингушетия. Разметку карточки здесь ещё НЕ смотрели —
    # карточки для проверки не было, регион подключён «по домену», как и ЕАО выше. Если
    # окажется, что это вторая разметка движка, регион надо будет перевести на
    # MsudrfTypeCCourtClient, когда напишут парсер типа C.
    ING_DOMAIN: MsudrfCourtClient,
    # 63 мировых суда Республики Саха (Якутия). Разметка типа B проверена на живой
    # карточке, но у архивных дел портала УИД на карточке нет вовсе — такие карточки
    # сохраняются под самодельным ключом от ссылки (см. synthetic_uid в app/validators.py).
    YAK_DOMAIN: MsudrfCourtClient,
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


def _client_for_host(host: str):
    """Класс клиента для хоста при ТОЧНОЙ сверке по границе имени (или None)."""
    for domain, court_client_cls in COURT_BY_DOMAIN.items():
        # Сравниваем по границе имени, а не через `in`: иначе «msudrf.ru.evil.com»
        # тоже подошёл бы, и мы пошли бы браузером куда угодно.
        if host == domain or host.endswith(f".{domain}"):
            return court_client_cls
    return None


def define_court_by_url(
    url: str,
    proxy: ProxySettings | None = None,
    headless: bool = True,
    on_captcha_attempt: AttemptSink | None = None,
) -> CourtClient:
    """Определить суд по домену ссылки на карточку дела.

    Две попытки, и порядок между ними принципиален:

    1. точная сверка хоста по границе имени;
    2. только если не совпало — те же сверки для остальных написаний хоста
       (host_variants в app/validators.py): метку участка отделяют от домена региона
       точкой, дефисом или вовсе не отделяют, и все три написания живые.

    Почему именно в таком порядке: «склеенный» вариант в первую очередь склеил бы разные
    регионы — ralt.msudrf.ru (Республика Алтай) заканчивается на alt.msudrf.ru (Алтайский
    край), и дело привязалось бы к чужому суду. При точной сверке первым проходом такой
    хост совпадает сам с собой и до перебора не доходит.
    """
    host = (urlsplit(url).hostname or "").lower()

    for candidate in host_variants(host) or [host]:
        court_client_cls = _client_for_host(candidate)
        if court_client_cls is not None:
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
