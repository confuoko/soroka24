"""Реестр парсеров: по типу страницы (page_type) выбираем нужную стратегию.

Чтобы добавить новый тип страницы — реализуй CaseParser и допиши строку
в PARSER_BY_PAGE_TYPE (по аналогии с COURT_BY_PREFIX в courts/resolver.py).
"""
from app.parsers.base import CaseParser
from app.parsers.moscow_type_a import MoscowTypeAParser
from app.parsers.msudrf_type_b import MsudrfTypeBParser
from app.parsers.msudrf_type_c import MsudrfTypeCParser
from app.parsers.spb_type_d import SpbTypeDParser

# Соответствие: тип страницы -> класс парсера.
PARSER_BY_PAGE_TYPE: dict[str, type[CaseParser]] = {
    "A": MoscowTypeAParser,  # мировые суды Москвы (mos-sud.ru)
    # Мировые суды на движке msudrf.ru (список регионов — в COURT_BY_DOMAIN).
    "B": MsudrfTypeBParser,
    # ЗАГЛУШКА: вторая разметка того же движка (Брянская область). Разбор не написан,
    # parse() падает с NotImplementedError. Ни один клиент суда тип C не отдаёт, так что
    # попасть сюда сейчас неоткуда — строка стоит, чтобы тип был заведён явно.
    "C": MsudrfTypeCParser,
    # ЗАГЛУШКА: мировые суды Санкт-Петербурга (mirsud.spb.ru). Разбор тоже не написан,
    # но, в отличие от типа C, портал к резолверу УЖЕ подключён — сюда реально попадают
    # задачи, и падают здесь намеренно: снимок страницы к этому моменту уже лежит в S3,
    # ради накопления образцов всё и затевалось (см. app/parsers/spb_type_d.py).
    "D": SpbTypeDParser,
}


def get_parser(page_type: str) -> CaseParser:
    """Вернуть экземпляр парсера для типа страницы."""
    parser_cls = PARSER_BY_PAGE_TYPE.get(page_type)
    if parser_cls is None:
        raise ValueError(f"Нет парсера для типа страницы: {page_type!r}")
    return parser_cls()
