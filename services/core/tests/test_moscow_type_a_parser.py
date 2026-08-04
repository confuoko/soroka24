"""Тесты парсера карточки мировых судов Москвы на сохранённых html_examples.

Парсер — чистая функция HTML -> данные, поэтому тестам не нужны ни БД, ни Chromium.
"""
from datetime import date, datetime
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


def test_court_sessions_parsed() -> None:
    """Разбираем вкладку «Судебные заседания»: время сохраняется, пустые колонки → None.

    Ровно одно заседание — важная проверка: клон этой таблицы лежит в div#modalTable, и
    если искать её по классу, а не по id="sessions", результат удвоится.
    """
    result = _parse("case_details_page_2.html")

    assert result["court_sessions"] == [
        {
            "session_date": datetime(2026, 6, 1, 14, 5),
            "place": "442 - 124365 Москва, Зеленоград, корп. 2016",
            "stage": "Судебное заседание",
            "result": "Рассмотрение завершено",
            "basis": None,
        }
    ]


def test_court_sessions_absent_tab() -> None:
    """У приказных дел вкладки заседаний нет совсем — пустой список, а не падение."""
    assert _parse("case_details_page.html")["court_sessions"] == []


def test_documents_parsed() -> None:
    """Разбираем вкладку «Документы»: только дата и вид, третья колонка игнорируется.

    У этой страницы во второй строке в «Тексте документа» лежит ссылка «Скачать файл» —
    в результат она попасть не должна: ни текста, ни файла мы не храним.
    """
    result = _parse("case_details_page_2.html")

    assert result["documents"] == [
        {
            "document_date": date(2026, 5, 14),
            "document_type": "Определение о назначении дела об административном правонарушении к рассмотрению",
        },
        {
            "document_date": date(2026, 6, 1),
            "document_type": "Постановление о назначении административного наказания",
        },
        {"document_date": date(2026, 7, 12), "document_type": "Электронное уведомление"},
    ]


def test_executive_documents_not_taken_for_documents() -> None:
    """Соседняя таблица «Исполнительные документы» не попадает в документы.

    Она лежит в ТОМ ЖЕ контейнере #act-documents, но это другая сущность. Если брать
    строки контейнера подряд, её запись приезжает мусором с пустой датой.
    """
    documents = _parse("case_details_page_2.html")["documents"]

    assert all(d["document_date"] is not None for d in documents)
    assert not any("Штраф как вид наказания" in d["document_type"] for d in documents)


def test_duplicate_documents_all_returned() -> None:
    """Одинаковые строки не схлопываются парсером: их различает репозиторий по позиции."""
    documents = _parse("case_details_page.html")["documents"]

    attachments = [d for d in documents if d["document_type"] == "Приложение"]
    assert len(attachments) > 1
    assert all(d["document_date"] == date(2026, 7, 10) for d in attachments)
