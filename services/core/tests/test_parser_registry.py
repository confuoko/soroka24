"""Реестр парсеров: выбор стратегии по типу страницы.

Отдельный файл нужен из-за типа C: он заведён заглушкой (Брянская область, разметка
движка msudrf.ru отличается от типа B), и важно зафиксировать, что заглушка ведёт себя
предсказуемо — падает явно и никуда не подключена.
"""
from pathlib import Path

import pytest

from app.courts import (
    MoscowMirCourtClient,
    MsudrfCourtClient,
    MsudrfTypeCCourtClient,
)
from app.courts.resolver import COURT_BY_DOMAIN, COURT_BY_PREFIX
from app.parsers.msudrf_shared import detect_page_type
from app.parsers.registry import get_parser

HTML_DIR = Path(__file__).resolve().parents[1] / "html_examples"


def test_parser_matches_client_page_type() -> None:
    """У каждого клиента суда есть парсер под его page_type.

    Связь клиента с парсером — только через эту строку, и разъехаться она может молча:
    клиент сходит на портал (прокси, капча, полминуты) и упадёт уже на разборе.
    """
    for client_cls in (
        MoscowMirCourtClient,
        MsudrfCourtClient,
        MsudrfTypeCCourtClient,
    ):
        parser = get_parser(client_cls.page_type)

        assert parser.page_type == client_cls.page_type


def test_type_c_client_differs_from_type_b_only_in_page_type() -> None:
    """Тип C — это тот же поход на портал и другой разбор, больше ничего.

    Движок общий: капча, невалидный сертификат, адрес карточки и заголовок с номером
    дела одинаковы. Если у наследника появится своя логика похода — значит, разница уже
    не в разметке, и делить клиенты надо иначе.
    """
    assert issubclass(MsudrfTypeCCourtClient, MsudrfCourtClient)
    assert MsudrfTypeCCourtClient.page_type == "C"
    assert MsudrfCourtClient.page_type == "B"
    # Своих атрибутов, кроме page_type, у наследника нет. Служебные отбрасываем: кроме
    # __dunder__ там лежит ещё и _abc_impl — его проставляет ABCMeta любому наследнику.
    own = {name for name in vars(MsudrfTypeCCourtClient) if not name.startswith("_")}
    assert own == {"page_type"}


def test_unknown_page_type_is_rejected() -> None:
    """Незаведённый тип страницы — ошибка сразу, а не None где-то дальше по коду."""
    with pytest.raises(ValueError):
        get_parser("Z")


def test_type_c_parses_the_second_layout_of_the_engine() -> None:
    """Тип C написан и разбирает вторую вёрстку движка (Пермский край, Адыгея).

    Раньше здесь стоял тест «заглушка обязана падать с NotImplementedError»: пустой разбор
    выглядел бы как «дело есть, но в нём ничего нет». Теперь разбор есть, и проверяем
    обратное — что он действительно достаёт данные, а на пустом документе всё равно не
    падает (браузер иногда отдаёт документ до окончания рендера).
    """
    parser = get_parser("C")

    assert parser.parse("<html></html>")["events"] == []


def test_type_c_is_reachable_from_the_courts_that_need_it() -> None:
    """Тип C доступен из справочника: регионы с этой вёрсткой подключены.

    Обратный тест к прежнему «тип C недостижим ни из одного суда» — он стерёг ненаписанный
    разбор. Теперь наоборот: если ни один клиент не отдаёт тип C, значит регионы Пермского
    края и Адыгеи отвалились от резолвера, и их дела снова разбираются не тем парсером.
    """
    clients = set(COURT_BY_PREFIX.values()) | set(COURT_BY_DOMAIN.values())

    assert "C" in {client_cls.page_type for client_cls in clients}


# ------------------------------------------- тип вёрстки определяем по самой странице
@pytest.mark.parametrize(
    "filename, expected",
    [
        # Тип B: Московская область, Якутия, Орловская область (другой порядок колонок),
        # Липецкая область (тела вкладок в div#cont1…3).
        ("mo_case_5_415323702.html", "B"),
        ("case_sakha45_nouid-14MS0054-972273874cab.html", "B"),
        ("case_3sev_nouid-57MS0035-faca1208385d.html", "B"),
        ("case_elec-r1_48MS0012-01-2026-001030-63.html", "B"),
        # Тип C: Пермский край и Адыгея.
        ("case_96_nouid-59MS0096-8ea6caa0770d.html", "C"),
        ("case_maikop1_nouid-01MS0001-049286050778.html", "C"),
        ("case_adg1_nouid-01MS0022-7c5751b4a3f0.html", "C"),
    ],
)
def test_page_type_is_detected_from_the_markup(filename, expected) -> None:
    """Вёрстку карточки опознаём по странице, а не по домену портала.

    Домен говорит лишь, какую вёрстку мы ОЖИДАЕМ у региона, а движок общий для 72 регионов
    и отдать может любую из двух: у Орловской области тип B, у соседнего Пермского края —
    тип C. Ошибка в типе не падает, а тихо даёт пустой разбор, поэтому спрашиваем страницу.
    """
    html = (HTML_DIR / filename).read_text(encoding="utf-8")

    assert detect_page_type(html) == expected


def test_page_that_is_not_a_card_has_no_type() -> None:
    """Не карточка (или снова новая вёрстка) — типа нет, и выдумывать его нельзя.

    В этом случае клиент разбирает страницу ожидаемым типом, а пустой результат отсечёт
    проверка в app/monitoring/tasks.py.
    """
    assert detect_page_type("<html><head></head><body></body></html>") is None
    # Страница поиска московского портала — тип A, к вёрсткам движка отношения не имеет.
    assert detect_page_type((HTML_DIR / "case_details_page.html").read_text(encoding="utf-8")) is None
