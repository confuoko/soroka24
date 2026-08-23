"""Выбор парсера: по порталу и по самой странице.

Здесь же закреплён главный архитектурный инвариант переноса (ТЗ PRIORITY 12 и 14):

    у движка msudrf.ru ОДИН клиент и ДВА парсера,
    и новая вёрстка добавляет парсер, а не клиента.

В старом core у msudrf было два клиента, второй состоял целиком из строки
`page_type = "C"`, и парсер выбирался по этой константе. Теперь у клиентов вёрстки нет
вовсе — её спрашивают у страницы.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.courts import MoscowClient, MsudrfClient, SpbClient
from app.courts.resolver import COURT_BY_DOMAIN, COURT_BY_PREFIX
from app.parsers import (
    MoscowTypeAParser,
    MsudrfTypeBParser,
    MsudrfTypeCParser,
    SpbTypeDParser,
    UnsupportedPage,
    detect_page_type,
    get_parser,
)

HTML_DIR = Path(__file__).resolve().parents[1] / "html_examples"

# Живые карточки обеих вёрсток движка.
MSUDRF_TYPE_B_PAGE = "mo_case_5_415323702.html"          # Московская область
MSUDRF_TYPE_C_PAGE = "case_96_nouid-59MS0096-8ea6caa0770d.html"  # Пермский край


def html(filename: str) -> str:
    return (HTML_DIR / filename).read_text(encoding="utf-8")


# ------------------------------------------------- один клиент, две вёрстки
def test_one_msudrf_client_serves_both_layouts() -> None:
    """ГЛАВНЫЙ ТЕСТ ФАЗЫ. Тот же клиент, разные парсеры — решает страница.

    Требование ТЗ §26: «msudrf B → BParser, msudrf C → CParser. Один MsudrfClient должен
    работать с обоими». Проверяем на живых карточках двух регионов.
    """
    portal = MsudrfClient.portal

    b_parser = get_parser(portal, html(MSUDRF_TYPE_B_PAGE))
    c_parser = get_parser(portal, html(MSUDRF_TYPE_C_PAGE))

    assert isinstance(b_parser, MsudrfTypeBParser)
    assert isinstance(c_parser, MsudrfTypeCParser)
    # И оба разбора действительно непустые — то есть парсер выбран верно, а не «просто
    # не упало».
    assert not b_parser.parse(html(MSUDRF_TYPE_B_PAGE)).is_empty()
    assert not c_parser.parse(html(MSUDRF_TYPE_C_PAGE)).is_empty()


def test_every_msudrf_domain_maps_to_the_single_client() -> None:
    """Все регионы движка сидят на ОДНОМ клиенте.

    Если здесь появится второй класс, значит кто-то снова начал делить клиентов по
    вёрстке — а вёрстка к способу похода на портал отношения не имеет.
    """
    clients = set(COURT_BY_DOMAIN.values()) | set(COURT_BY_PREFIX.values())
    msudrf_clients = {c for c in clients if getattr(c, "portal", None) == "msudrf"}

    assert msudrf_clients == {MsudrfClient}


def test_clients_do_not_declare_a_layout() -> None:
    """У клиента нет атрибута page_type: вёрстка — свойство страницы, а не похода."""
    for client_cls in (MoscowClient, MsudrfClient, SpbClient):
        assert not hasattr(client_cls, "page_type"), client_cls.__name__


def test_clients_do_not_parse() -> None:
    """Клиент не выбирает и не вызывает парсер (ТЗ PRIORITY 6)."""
    for client_cls in (MoscowClient, MsudrfClient, SpbClient):
        assert not hasattr(client_cls, "parse"), client_cls.__name__


# --------------------------------------------------------- порталы с одной вёрсткой
def test_single_layout_portals_do_not_need_the_page() -> None:
    """У Москвы и Петербурга вёрстка одна на портал — страницу можно не смотреть."""
    assert isinstance(get_parser("mos-sud", ""), MoscowTypeAParser)
    assert isinstance(get_parser("spb", ""), SpbTypeDParser)


def test_every_known_portal_has_a_parser() -> None:
    """У каждого портала, на который мы умеем ходить, есть чем разбирать страницу.

    Связь «клиент — парсер» больше не выражена одной строкой реестра, поэтому разъехаться
    она может только здесь: добавили клиента и забыли ветку в get_parser. Цена ошибки —
    поход на портал (прокси, капча, полминуты) и отказ уже на разборе.
    """
    portals = {
        c.portal for c in set(COURT_BY_DOMAIN.values()) | set(COURT_BY_PREFIX.values())
    }
    pages = {"msudrf": html(MSUDRF_TYPE_B_PAGE)}

    for portal in sorted(portals):
        parser = get_parser(portal, pages.get(portal, ""))
        assert parser is not None, portal


# ------------------------------------------------------------------------ отказы
def test_unknown_portal_is_rejected() -> None:
    """Неизвестный портал — явная ошибка, а не None где-то дальше по коду."""
    with pytest.raises(UnsupportedPage):
        get_parser("no-such-portal", "")


def test_unrecognised_msudrf_layout_is_rejected() -> None:
    """Вёрстку движка не опознали — честный отказ.

    OLD: клиент нёс ожидаемую вёрстку константой класса, неопознанная страница
         разбиралась ожидаемым парсером, разбор выходил пустым, и его отсекала проверка
         «пустой разбор» в Celery-задаче.
    NEW: UnsupportedPage сразу.
    REASON: ожидаемой вёрстки больше нет — её носил клиент, а клиент про вёрстку теперь
         не знает. Итог в обоих случаях окончательный отказ, но теперь по нему видно, что
         портал сменил разметку, а не «дело почему-то пустое».
    """
    with pytest.raises(UnsupportedPage):
        get_parser("msudrf", "<html><head></head><body></body></html>")


# --------------------------------------- вёрстку определяем по самой странице
@pytest.mark.parametrize(
    "filename, expected",
    [
        # Тип B: Московская область, Якутия, Орловская область (другой порядок колонок),
        # Липецкая область (тела вкладок в div#cont1…3).
        ("mo_case_5_415323702.html", "B"),
        ("case_sakha45_nouid-14MS0054-972273874cab.html", "B"),
        ("case_3sev_nouid-57MS0035-faca1208385d.html", "B"),
        ("case_elec-r1_48MS0012-01-2026-001030-63.html", "B"),
        ("case_abakan1_19MS0001-01-2026-004064-29.html", "B"),  # Хакасия
        ("case_okt6_61MS0033-01-2026-002493-56.html", "B"),     # Ростовская область
        ("case_10_nouid-65MS0010-33a3e9527ac3.html", "B"),      # Сахалин
        ("case_bond_nouid-68MS0001-909bec32420a.html", "B"),    # Тамбов, материал
        # Тип C: Пермский край, Адыгея, Тыва, Рязанская область.
        ("case_96_nouid-59MS0096-8ea6caa0770d.html", "C"),
        ("case_maikop1_nouid-01MS0001-049286050778.html", "C"),
        ("case_adg1_nouid-01MS0022-7c5751b4a3f0.html", "C"),
        ("case_kizil1_nouid-17MS0001-21378c2ab9c5.html", "C"),
        ("case_57_nouid-62MS0068-c95d1c425ddd.html", "C"),
    ],
)
def test_page_type_is_detected_from_the_markup(filename: str, expected: str) -> None:
    """Вёрстку карточки опознаём по странице, а не по домену портала.

    Домен говорит лишь, какую вёрстку мы ОЖИДАЕМ у региона, а движок общий на 63 региона
    и отдать может любую из двух: у Орловской области тип B, у соседнего Пермского края
    тип C.
    """
    assert detect_page_type(html(filename)) == expected


def test_page_that_is_not_a_card_has_no_type() -> None:
    """Не карточка (или снова новая вёрстка) — типа нет, и выдумывать его нельзя."""
    assert detect_page_type("<html><head></head><body></body></html>") is None
    # Страница поиска московского портала — к вёрсткам движка отношения не имеет.
    assert detect_page_type(html("case_details_page.html")) is None
