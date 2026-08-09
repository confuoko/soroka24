"""Реестр парсеров: выбор стратегии по типу страницы.

Отдельный файл нужен из-за типа C: он заведён заглушкой (Брянская область, разметка
движка msudrf.ru отличается от типа B), и важно зафиксировать, что заглушка ведёт себя
предсказуемо — падает явно и никуда не подключена.
"""
import pytest

from app.courts import (
    MoscowMirCourtClient,
    MsudrfCourtClient,
    MsudrfTypeCCourtClient,
)
from app.courts.resolver import COURT_BY_DOMAIN, COURT_BY_PREFIX
from app.parsers.registry import get_parser


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


def test_type_c_is_a_stub_that_fails_loudly() -> None:
    """Тип C заведён, но не написан: parse() обязан падать, а не отдавать пустой разбор.

    Пустой словарь выглядел бы как «дело есть, но в нём ничего нет» — карточка
    сохранилась бы без единого поля, и обход счёл бы это нормой.
    """
    parser = get_parser("C")

    with pytest.raises(NotImplementedError):
        parser.parse("<html></html>")


def test_type_c_is_not_reachable_from_any_court() -> None:
    """Пока разбор не написан, в тип C не должен вести ни один клиент суда.

    Иначе дело завелось бы, задача сходила бы на портал через прокси и капчу — и всё
    это ради NotImplementedError на разборе.
    """
    clients = set(COURT_BY_PREFIX.values()) | set(COURT_BY_DOMAIN.values())

    assert "C" not in {client_cls.page_type for client_cls in clients}
