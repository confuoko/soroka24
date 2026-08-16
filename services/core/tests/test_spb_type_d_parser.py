"""Тесты парсера карточки мировых судов Санкт-Петербурга на сохранённых html_examples.

Парсер — чистая функция HTML -> данные, поэтому тестам не нужны ни БД, ни Chromium.

Фикстуры подобраны по одной на каждый вид производства: набор строк карточки у них
различается (у негражданских нет «Сущности спора» и «Даты принятия к производству»),
поэтому «работает на гражданском» тут ничего не доказывает.
  * case_mirsud_78MS0098-01-2026-003970-97.html — гражданское приказное, 2 стороны, 5 событий
  * case_mirsud_78MS0124-01-2026-003108-44.html — гражданское, где дата принятия
    к производству ОТЛИЧАЕТСЯ от даты поступления (ради этого случая и заведено поле)
  * case_mirsud_78MS0009-01-2026-003023-89.html — КоАП: сторона «Привлекаемое лицо»
  * case_mirsud_78MS0208-01-2026-002313-14.html — уголовное: одно событие
  * case_mirsud_78MS0056-01-2026-002523-68.html — материал «9у-»: номер с кириллицей
"""
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from app.parsers.spb_type_d import SpbTypeDParser

HTML_DIR = Path(__file__).resolve().parents[1] / "html_examples"

CIVIL = "case_mirsud_78MS0098-01-2026-003970-97.html"
CIVIL_LATE_ACCEPT = "case_mirsud_78MS0124-01-2026-003108-44.html"
KOAP = "case_mirsud_78MS0009-01-2026-003023-89.html"
CRIMINAL = "case_mirsud_78MS0208-01-2026-002313-14.html"
MATERIAL = "case_mirsud_78MS0056-01-2026-002523-68.html"


def _html(filename: str) -> str:
    return (HTML_DIR / filename).read_text(encoding="utf-8")


def _parse(filename: str) -> dict:
    return SpbTypeDParser().parse(_html(filename))


# ------------------------------------------------------------------ скалярные поля
def test_civil_card_fields_parsed() -> None:
    """Гражданское дело: разбираем все строки карточки, которых нет — None."""
    card = _parse(CIVIL)

    assert card["receipt_date"] == date(2026, 8, 14)
    assert card["accepted_date"] == date(2026, 8, 14)
    assert card["status"] == "Судебный приказ"
    assert card["category"].startswith("Споры, возникающие из ЖИЛИЩНОГО законодательства")
    # Метки «Дата регистрации» на портале нет ни у одного вида производства.
    assert card.get("registration_date") is None


def test_acceptance_date_can_differ_from_receipt_date() -> None:
    """Заявление приняли не в день поступления — ради этого поле и отдельное.

    Если сложить обе даты в одно поле, разницу «поступило 10-го, принято 13-го» уже
    не восстановить, а она означает, что заявление три дня лежало без движения.
    """
    card = _parse(CIVIL_LATE_ACCEPT)

    assert card["receipt_date"] == date(2026, 8, 10)
    assert card["accepted_date"] == date(2026, 8, 13)


def test_koap_card_has_no_civil_only_labels() -> None:
    """У КоАП нет ни «Сущности спора», ни «Даты принятия к производству».

    Портал просто не рисует эти строки. Отсутствие метки — норма, а не поломка:
    поля обязаны прийти None, а разбор — не упасть.
    """
    card = _parse(KOAP)

    assert card["receipt_date"] == date(2026, 8, 21)
    assert card["accepted_date"] is None
    assert card["category"] is None
    assert card["status"] == "Рассмотрение"


def test_criminal_and_material_parse_too() -> None:
    """Уголовное дело и материал разбираются тем же кодом."""
    criminal = _parse(CRIMINAL)
    material = _parse(MATERIAL)

    assert criminal["receipt_date"] == date(2026, 8, 13)
    assert criminal["accepted_date"] is None
    assert material["receipt_date"] == date(2026, 8, 13)
    assert material["accepted_date"] is None


def test_receipt_date_is_present_for_every_case_kind() -> None:
    """«Дата поступления» здесь есть у ВСЕХ видов производства.

    На порталах msudrf.ru и mos-sud.ru она только у гражданских, а у КоАП вместо неё
    «Дата регистрации». Ошибиться легко, поэтому проверяем все фикстуры разом.
    """
    for filename in (CIVIL, CIVIL_LATE_ACCEPT, KOAP, CRIMINAL, MATERIAL):
        assert _parse(filename)["receipt_date"] is not None, filename


# ------------------------------------------------------------------------- судья
def test_judge_is_taken_from_the_card() -> None:
    """Судья лежит в той же таблице, но это связь, а не скалярное поле дела."""
    assert _parse(CIVIL)["judge_names"] == ["Салахова Елена Сергеевна"]
    # Портал местами ставит двойные пробелы в ФИО — их схлопываем.
    assert _parse(MATERIAL)["judge_names"] == ["Лопатина Ирина Александровна"]


# ------------------------------------------------------------------------ стороны
def test_civil_sides_keep_portal_roles() -> None:
    """Роль пишем ровно как на портале, к общему словарю не приводим."""
    assert _parse(CIVIL)["sides"] == [
        {"role": "Истец", "full_name": 'ООО "Дом. Северная Столица"'},
        {"role": "Ответчик", "full_name": "Ефимова Юлия Владиславовна"},
    ]


def test_roles_differ_by_case_kind() -> None:
    """У КоАП «Привлекаемое лицо», у уголовного «Подсудимый» — сводить их нельзя."""
    assert _parse(KOAP)["sides"] == [
        {"role": "Привлекаемое лицо", "full_name": 'ООО "Строй Сервис"'}
    ]
    assert _parse(CRIMINAL)["sides"] == [
        {"role": "Подсудимый", "full_name": "Карческий Виктор Вячеславович"}
    ]


# ------------------------------------------------------------------- движение дела
def test_events_keep_the_portal_joined_description() -> None:
    """Событие и результат портал склеивает сам — берём строку как есть.

    В типе B их приходится сшивать из двух колонок; здесь сшивать нечего, и попытка
    «улучшить» строку разъехалась бы с identity уже сохранённых событий.
    """
    events = _parse(CIVIL)["events"]

    assert len(events) == 5
    assert events[0] == {
        "event_date": date(2026, 8, 14),
        "state_description": "Регистрация поступившего заявления о выдаче судебного приказа",
        "document_str": None,
        "published_at": date(2026, 8, 10),
    }
    assert events[1]["state_description"] == (
        "Решение вопроса о принятии заявления / Заявление принято"
    )


def test_event_time_column_is_ignored() -> None:
    """Колонка «Время события» есть на портале, но в событие не попадает.

    У Event только event_date (Date); время в identity не входит. Проверяем, что оно
    не уехало по ошибке в дату публикации — колонки соседние.
    """
    for event in _parse(CIVIL)["events"]:
        assert isinstance(event["published_at"], date)
    assert _parse(CIVIL)["events"][0]["published_at"] == date(2026, 8, 10)


def test_events_without_a_date_are_skipped() -> None:
    """Без даты событие не участвует в детекте изменений — из него не посчитать uid."""
    soup = BeautifulSoup(_html(CRIMINAL), "lxml")
    row = soup.select("div.case-print table.case-print__table")[-1].find_all("tr")[-1]
    row.find_all("td")[1].string = ""

    events = SpbTypeDParser().parse(str(soup))["events"]

    assert events == []


# --------------------------------------------------------------- выбор таблиц
def test_tables_are_found_by_header_not_by_order() -> None:
    """Таблицы ищем по шапке: их порядок — наблюдение, а не гарантия портала.

    Меняем стороны и движение местами: разбор обязан остаться прежним.
    """
    soup = BeautifulSoup(_html(CIVIL), "lxml")
    tables = soup.select("div.case-print table.case-print__table")
    sides_table, events_table = tables[1], tables[2]
    events_table.insert_before(sides_table.extract())

    swapped = SpbTypeDParser().parse(str(soup))

    assert swapped["sides"] == _parse(CIVIL)["sides"]
    assert swapped["events"] == _parse(CIVIL)["events"]


def test_access_key_form_is_not_mistaken_for_data() -> None:
    """Рядом лежит table.personal-data — форма ключа доступа, а не данные дела."""
    soup = BeautifulSoup(_html(CIVIL), "lxml")

    assert soup.select_one("table.personal-data") is not None
    for side in _parse(CIVIL)["sides"]:
        assert "ключ доступа" not in side["full_name"].lower()


# ------------------------------------------------------------- пустой документ
def test_empty_document_gives_empty_result() -> None:
    """Браузер отдал документ до отрисовки карточки — разбор пуст, но не падает.

    Карточку рисует фоновая задача портала уже после загрузки страницы, так что такой
    документ реально встречается.
    """
    data = SpbTypeDParser().parse("<html><head></head><body></body></html>")

    assert data["judge_names"] == []
    assert data["sides"] == []
    assert data["events"] == []
    assert data["receipt_date"] is None
    assert data["accepted_date"] is None
    assert data["status"] is None


def test_blocks_absent_on_the_portal_are_empty_lists() -> None:
    """Истории местонахождения, заседаний и документов портал не публикует вовсе."""
    data = _parse(CIVIL)

    assert data["place_history"] == []
    assert data["court_sessions"] == []
    assert data["documents"] == []
