"""Парсеры карточек дел: HTML на входе, ParsedCase на выходе.

Парсер — чистая функция от разметки. Ни сети, ни БД, ни прокси, ни капчи. Он не знает,
с какого портала пришла страница и как её добыли.

Класс — на ВЁРСТКУ, а не на портал. Это не мелочь: движок msudrf.ru обслуживает 63
региона и отдаёт две разные вёрстки карточки (типы B и C), причём какую именно — заранее
неизвестно, потому что регионы подключают по домену. Ходит на портал при этом один и тот
же клиент. Отсюда правило: новая вёрстка добавляет парсер, но не клиента.

    A — мировые суды Москвы (mos-sud.ru)
    B — движок msudrf.ru, вёрстка 1: метки в <h2>, у таблиц есть <thead>
    C — движок msudrf.ru, вёрстка 2: метки в <b>, <thead> нет, таблица сторон
        транспонирована
    D — мировые суды Санкт-Петербурга (mirsud.spb.ru), печатная форма карточки

Парсер выбирает функция get_parser ниже — по порталу и по самой странице. Не по
клиенту: клиент про вёрстку не знает.

Общее для всех: app/parsers/text.py (мелкие текстовые хелперы),
app/parsers/parsed_case.py (контракт вывода). Общее для B и C — app/parsers/msudrf_shared.py.
"""
from app.parsers.base import CaseParser, UnsupportedPage
from app.parsers.moscow_type_a import MoscowTypeAParser
from app.parsers.msudrf_shared import detect_page_type
from app.parsers.msudrf_type_b import MsudrfTypeBParser
from app.parsers.msudrf_type_c import MsudrfTypeCParser
from app.parsers.parsed_case import (
    UNSET,
    ParsedCase,
    ParsedDocument,
    ParsedEvent,
    ParsedPlace,
    ParsedSession,
    ParsedSide,
)
from app.parsers.spb_type_d import SpbTypeDParser


def get_parser(portal: str, html: str) -> CaseParser:
    """Чем разбирать эту страницу: по порталу и по самой странице.

    Обычная функция с обычными `if`, а не реестр и не фабрика. Парсеров четыре; словарь
    или класс-резолвер здесь не добавили бы ничего, кроме одного лишнего перехода при
    чтении. Появится пятый — добавится ветка.

    Почему нужен И портал, И разметка:

    * у mos-sud.ru и mirsud.spb.ru вёрстка одна на портал, и знать больше нечего;
    * у движка msudrf.ru их ДВЕ, и какая придёт — заранее неизвестно. Регионы подключают
      по домену, не открыв ни одной карточки: у Орловской области оказался тип B, у
      соседнего Пермского края тип C. Поэтому у msudrf вёрстку спрашиваем у страницы.

    Именно поэтому html — обязательный аргумент, хотя двум порталам из трёх он не нужен:
    иначе вызывающий должен был бы помнить, кому его передавать, а кому нет.
    """
    if portal == "mos-sud":
        return MoscowTypeAParser()

    if portal == "spb":
        return SpbTypeDParser()

    if portal == "msudrf":
        page_type = detect_page_type(html)
        if page_type == "B":
            return MsudrfTypeBParser()
        if page_type == "C":
            return MsudrfTypeCParser()
        # detect_page_type вернул None: ни <h2>-меток типа B, ни <b>-меток типа C.
        # Значит это либо вообще не карточка, либо у движка появилась третья вёрстка.
        raise UnsupportedPage(
            "Страница движка msudrf не опознана ни как вёрстка B, ни как C"
        )

    raise UnsupportedPage(f"Нет парсеров для портала {portal!r}")


__all__ = [
    "CaseParser",
    "UnsupportedPage",
    "detect_page_type",
    "get_parser",
    "MoscowTypeAParser",
    "MsudrfTypeBParser",
    "MsudrfTypeCParser",
    "SpbTypeDParser",
    "UNSET",
    "ParsedCase",
    "ParsedDocument",
    "ParsedEvent",
    "ParsedPlace",
    "ParsedSession",
    "ParsedSide",
]
