"""Тесты парсера ВТОРОЙ вёрстки карточки движка msudrf.ru (тип C) на сохранённых страницах.

Парсер — чистая функция HTML -> данные, поэтому тестам не нужны ни БД, ни Chromium.

Фикстуры подобраны так, чтобы поймать всё, чем эта вёрстка отличается от типа B:
  * case_maikop1_nouid-01MS0001-049286050778.html — Адыгея, приказное: таблица «Движения
    дела» из ЧЕТЫРЁХ колонок (без «Времени события»), стороны в ТРАНСПОНИРОВАННОЙ таблице,
    вкладка подписана «СТОРОНЫ ПО ДЕЛУ (ТРЕТЬИ ЛИЦА)»;
  * case_96_nouid-59MS0096-75a9a5a05233.html — Пермский край, КоАП: колонок ПЯТЬ, у сторон
    другие подписи строк («Вид участника производства»), а секция называется «СВЕДЕНИЯ О
    ПРИВЛЕКАЕМОМ ЛИЦЕ»;
  * case_96_nouid-59MS0096-8ea6caa0770d.html — Пермский край, уголовное: в карточке всего
    одна метка, вкладки сторон нет вовсе.

УИД ни на одной из этих страниц нет — карточкам достаётся самодельный ключ от ссылки
(synthetic_uid в app/validators.py), поэтому так названы и файлы фикстур.
"""
from datetime import date
from pathlib import Path

from app.parsers.msudrf_type_c import MsudrfTypeCParser

HTML_DIR = Path(__file__).resolve().parents[1] / "html_examples"

ADYGEA_ORDER = "case_maikop1_nouid-01MS0001-049286050778.html"
PERM_KOAP = "case_96_nouid-59MS0096-75a9a5a05233.html"
PERM_CRIMINAL = "case_96_nouid-59MS0096-8ea6caa0770d.html"


def _parse(filename: str) -> dict:
    html = (HTML_DIR / filename).read_text(encoding="utf-8")
    return MsudrfTypeCParser().parse(html)


# ------------------------------------------------------------------ скалярные поля
def test_card_labels_in_bold_are_parsed() -> None:
    """Метки карточки свёрстаны <b>, а не <h2> — набор меток при этом тот же, что в типе B."""
    card = _parse(ADYGEA_ORDER)

    assert card["category"].startswith("Споры, связанные с имущественными правами")
    assert card["first_instance_date"] == date(2026, 7, 24)
    assert card["first_instance_decision"] == "Судебный приказ"
    assert card["judge_names"] == ["Нефляшев Асхад Юрьевич"]
    # Меток, которых на странице нет, не выдумываем.
    assert card["receipt_date"] is None
    assert card["decision_effective_date"] is None


def test_section_heading_is_not_a_label() -> None:
    """«ОСНОВНЫЕ СВЕДЕНИЯ» — секционный заголовок в ячейке с colspan, а не метка поля.

    Он свёрстан тем же <h2>, что метки типа B, и стоит первой строкой каждой таблицы. Если
    принять его за метку, разбор поехал бы на одну строку.
    """
    card = _parse(PERM_CRIMINAL)

    # В карточке этого дела ровно одна метка — судья, и она найдена.
    assert card["judge_names"] == ["Карташев Алексей Юрьевич"]
    assert card["category"] is None


# ------------------------------------------------------------------ движение дела
def test_four_column_movement_table() -> None:
    """Адыгея: колонок четыре, «Времени события» нет — колонки ищем по шапке.

    Шапка здесь лежит в теле таблицы (строка из <b>), потому что <thead> у этой вёрстки нет.
    """
    card = _parse(ADYGEA_ORDER)

    assert [(e["event_date"], e["state_description"]) for e in card["events"]] == [
        (date(2026, 7, 24), "Регистрация судебного приказа"),
        (date(2026, 7, 30), "Окончание производства"),
    ]
    # Состояние дела — последняя строка таблицы, даже если даты у неё нет.
    assert card["status"] == "Сдача в архив"
    # Колонки «Дата размещения» у этой вёрстки нет ни в одном регионе.
    assert all(event["published_at"] is None for event in card["events"])


def test_five_column_movement_table_keeps_result() -> None:
    """Пермский край: колонок пять, результат события приезжает в описание состояния."""
    card = _parse(PERM_KOAP)

    assert len(card["events"]) == 1
    event = card["events"][0]
    assert event["event_date"] == date(2026, 8, 18)
    assert event["state_description"] == "Рассмотрение дела"
    assert card["status"] == "Рассмотрение дела"


def test_row_without_event_date_is_not_an_event_but_is_a_state() -> None:
    """Строка без даты события в события не идёт, а состоянием дела стать может.

    Дата входит в identity события (uid считается от неё), поэтому без даты строку сохранить
    нельзя. Но у только что заведённых дел даты нет ни в одной строке, и иначе о деле не
    осталось бы вообще ничего.
    """
    card = _parse(PERM_CRIMINAL)

    # На странице две строки: «Первичное ознакомление» без даты и «Предварительное
    # слушание» с датой.
    assert [e["state_description"] for e in card["events"]] == ["Предварительное слушание"]
    assert card["status"] == "Предварительное слушание"


# ------------------------------------------------------------------------- стороны
def test_transposed_sides_table_is_paired_by_column() -> None:
    """Стороны лежат ТРАНСПОНИРОВАННО: роли в одной строке, ФИО в следующей.

    Это главное отличие вёрстки: в типе B строка таблицы — это одна сторона, а здесь строка
    — это поле, а сторона — колонка. Склеивать роль с ФИО нужно по номеру колонки.
    """
    sides = _parse(ADYGEA_ORDER)["sides"]

    assert sides == [
        {"role": "Взыскатель", "full_name": 'ООО ПКО "Финэква"'},
        {"role": "Должник", "full_name": "Трапизонян Екатерина Дмитриевна"},
    ]


def test_koap_sides_have_their_own_row_labels() -> None:
    """У КоАП строки сторон подписаны иначе: «Вид участника производства».

    Прочие строки той же таблицы («Главная статья (КоАП, ТК ...)», «Наименование вида
    правонарушения») не читаем: полей под них в модели дела нет.
    """
    sides = _parse(PERM_KOAP)["sides"]

    assert sides == [
        {
            "role": "Лицо, в отношении которого ведется производство по делу",
            "full_name": "Рубцов Илья Курстанбекович",
        }
    ]


def test_missing_sides_tab_is_not_an_error() -> None:
    """Вкладки сторон может не быть вовсе — это норма, а не поломка разметки."""
    assert _parse(PERM_CRIMINAL)["sides"] == []


# ----------------------------------------------------------------------- живучесть
def test_empty_document_parses_to_empty_result() -> None:
    """Браузер иногда отдаёт документ до рендера — это пустой разбор, а не исключение."""
    result = MsudrfTypeCParser().parse("<html><head></head><body></body></html>")

    assert result["status"] is None
    assert result["judge_names"] == []
    assert result["sides"] == []
    assert result["events"] == []
    assert result["category"] is None


def test_absent_sections_are_always_empty_lists() -> None:
    """Местонахождений, заседаний и документов у движка нет — ключи всё равно есть.

    update_case читает их через data.get(..., []), но пустой список честнее отсутствия
    ключа: он означает «на странице пусто», и лишние строки в БД будут удалены.
    """
    result = _parse(ADYGEA_ORDER)

    assert result["place_history"] == []
    assert result["court_sessions"] == []
    assert result["documents"] == []
