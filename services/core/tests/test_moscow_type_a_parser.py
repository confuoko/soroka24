"""Тесты парсера карточки мировых судов Москвы на сохранённых html_examples.

Парсер — чистая функция HTML -> данные, поэтому тестам не нужны ни БД, ни Chromium.
"""
from datetime import date
from pathlib import Path

import pytest

from app.parsers.moscow_type_a import MoscowTypeAParser

HTML_DIR = Path(__file__).resolve().parents[1] / "html_examples"


def _parse(filename: str) -> dict:
    html = (HTML_DIR / filename).read_text(encoding="utf-8")
    return MoscowTypeAParser().parse(html)


@pytest.mark.parametrize(
    ("filename", "expected_date"),
    [
        ("case_details_page.html", date(2026, 7, 10)),
        ("case_details_page_2.html", date(2026, 5, 12)),
    ],
)
def test_place_history_parsed(filename: str, expected_date: date) -> None:
    """Разбираем «Историю местонахождения»: одна строка, комментарий пустой -> None.

    Ровно одна строка — важная проверка: внизу страницы лежат скрытые клоны тех же
    таблиц в div#modalTable, и если искать таблицу по классу, а не по <h3>,
    результат удвоится.
    """
    result = _parse(filename)

    assert result["place_history"] == [
        {
            "place_date": expected_date,
            "place_description": "В канцелярии",
            "comment": None,
        }
    ]


def test_place_history_absent_section() -> None:
    """Нет заголовка «История местонахождения» — пустой список, а не падение."""
    html = "<html><body><h3>История состояний</h3><table></table></body></html>"

    assert MoscowTypeAParser().parse(html)["place_history"] == []


def test_state_history_not_confused_with_place_history() -> None:
    """События берутся из своей таблицы и не смешиваются с местонахождениями."""
    events = _parse("case_details_page_2.html")["events"]

    assert events, "события должны разобраться"
    descriptions = {e["state_description"] for e in events}
    assert "В канцелярии" not in descriptions
