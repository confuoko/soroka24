"""Characterization: полный вывод каждого парсера на каждой сохранённой странице.

Это не тесты «правильности» — это фиксация того, как парсеры ведут себя СЕЙЧАС. Любое
расхождение с golden означает, что поведение изменилось. После переноса в core_v2 те же
golden-файлы становятся контрактом для новых парсеров (ТЗ PRIORITY 17: «после переноса
сравнить старую и новую реализацию»).

Golden пересобирается только скриптом generate_golden.py и только осознанно.
"""
from __future__ import annotations

import json

import pytest
from conftest import GOLDEN_DIR, read_html
from snapshot import encode, parse_or_error

from app.parsers.msudrf_shared import detect_page_type
from app.parsers.registry import PARSER_BY_PAGE_TYPE

PAGE_TYPES = sorted(PARSER_BY_PAGE_TYPE)


def load_golden(name: str) -> dict:
    path = GOLDEN_DIR / name
    if not path.exists():
        pytest.fail(
            f"нет golden-файла {path}. Собрать: "
            "python services/characterization/generate_golden.py"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def golden_cases(page_type: str) -> list[tuple[str, str]]:
    """Пары (страница, тип парсера) из golden — источник параметризации."""
    return sorted(load_golden(f"parser_{page_type}.json"))


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_golden_covers_every_saved_page(page_type: str) -> None:
    """Golden не должен незаметно потерять страницы."""
    golden = load_golden(f"parser_{page_type}.json")
    assert len(golden) == 80, (
        f"в golden парсера {page_type} {len(golden)} страниц вместо 80 — "
        "добавили или потеряли фикстуру, golden надо пересобрать осознанно"
    )


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_parser_output_matches_golden(page_type: str) -> None:
    """Вывод парсера совпадает с зафиксированным — поле в поле, включая порядок списков.

    Сравниваем разом все 80 страниц, а не по одной на тест: при расхождении важно
    видеть, сколько страниц поехало — одна (правка селектора) или все (сломался общий
    хелпер).
    """
    golden = load_golden(f"parser_{page_type}.json")
    parser_class = PARSER_BY_PAGE_TYPE[page_type]

    mismatched: list[str] = []
    for name in sorted(golden):
        actual = encode(parse_or_error(parser_class(), read_html(name)))
        if actual != golden[name]:
            mismatched.append(name)

    assert not mismatched, (
        f"парсер {page_type} даёт другой результат на {len(mismatched)} страницах: "
        f"{mismatched[:10]}"
    )


def test_no_parser_raises_on_any_page() -> None:
    """Парсер обязан отдавать пустой результат, а не исключение (риск R11).

    Проверяется по golden: ключа __error__ там быть не должно ни у одной страницы ни у
    одного парсера. Сейчас это выполняется на всех 320 сочетаниях.
    """
    raised: list[str] = []
    for page_type in PAGE_TYPES:
        for name, result in load_golden(f"parser_{page_type}.json").items():
            if "__error__" in result:
                raised.append(f"{page_type}/{name}: {result['__error__']}")

    assert not raised, "парсеры бросают исключения: " + "; ".join(raised[:10])


def test_detect_page_type_matches_golden() -> None:
    """Определение вёрстки msudrf — дословно как сейчас.

    Им выбирается парсер для msudrf, поэтому его поведение обязано пережить перенос
    без изменений (ТЗ PRIORITY 13).
    """
    golden = load_golden("detect_page_type.json")
    mismatched = {
        name: (expected, detect_page_type(read_html(name)))
        for name, expected in sorted(golden.items())
        if detect_page_type(read_html(name)) != expected
    }
    assert not mismatched, f"detect_page_type изменился: {mismatched}"


def test_detect_page_type_distribution_is_pinned() -> None:
    """Сколько страниц каким типом опознаётся — тоже часть контракта.

    Отдельный тест, потому что распределение читается человеком: если после правки
    селектора 55 страниц типа B превратятся в 54, это увидит и тот, кто не сравнивает
    golden построчно.
    """
    golden = load_golden("detect_page_type.json")
    counts: dict[str, int] = {}
    for value in golden.values():
        counts[value or "не карточка"] = counts.get(value or "не карточка", 0) + 1

    assert counts == {"B": 55, "C": 13, "не карточка": 12}


# ------------------------------------------------------- ключи, которых нет (риск R1)
# Самое опасное место переноса: отсутствующий ключ и ключ со значением None — это два
# РАЗНЫХ указания для CaseRepository.upsert_by_uid_court_code. Здесь это закрепляется
# явно, чтобы typed ParsedCase в Phase 5 не подсунул None-дефолты вместо отсутствия.

# Поля, которых у парсера нет НИ НА ОДНОЙ сохранённой странице (ключ отсутствует).
# Строго говоря это «ни разу не наблюдалось на 80 фикстурах», а не «невозможно в
# принципе»: у типа A непустых страниц всего 2, у D — 7. Но для контракта переноса
# важно именно это: typed ParsedCase не имеет права начать отдавать ключ, которого
# старый парсер не отдавал, потому что тогда колонка обнулится.
FIELDS_NEVER_PRESENT = {
    "A": ("accepted_date",),
    "B": (
        "application_number",
        "incoming_number",
        "superior_case_number",
        "code",
        "registration_date",
        "accepted_date",
    ),
    "C": (
        "application_number",
        "incoming_number",
        "superior_case_number",
        "code",
        "registration_date",
        "accepted_date",
    ),
    "D": (
        "application_number",
        "incoming_number",
        "superior_case_number",
        "code",
        "registration_date",
        # У СПб этих меток на карточке нет — уточнение к таблице раздела 11 аудита,
        # найденное при сборе golden.
        "first_instance_date",
        "first_instance_decision",
        "decision_effective_date",
    ),
}


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_fields_the_parser_never_reports_stay_absent(page_type: str) -> None:
    """Такого ключа не должно появиться ни на одной странице — иначе колонка обнулится."""
    golden = load_golden(f"parser_{page_type}.json")
    leaked: list[str] = []
    for field in FIELDS_NEVER_PRESENT[page_type]:
        pages = [name for name, result in golden.items() if field in result]
        if pages:
            leaked.append(f"{field} на {len(pages)} страницах, напр. {pages[0]}")

    assert not leaked, (
        f"у парсера {page_type} появились ключи, которых у него быть не должно: "
        + "; ".join(leaked)
    )


def test_type_a_never_reports_published_at_on_events() -> None:
    """У типа A ключа published_at в событиях нет вовсе (moscow_type_a.py:236-242).

    Отдельным тестом, потому что это вложенный ключ: у B и D он есть, у C всегда None,
    а у A отсутствует. typed ParsedEvent нормализует его к None — это допустимо только
    потому, что published_at не входит в _UPDATABLE_FIELDS.
    """
    golden = load_golden("parser_A.json")
    leaked = [
        name
        for name, result in golden.items()
        for event in result.get("events", [])
        if "published_at" in event
    ]
    assert not leaked, f"published_at появился в событиях типа A: {leaked}"


def test_type_c_always_reports_published_at_as_none() -> None:
    """У типа C ключ есть, но значение всегда None (msudrf_type_c.py:199)."""
    golden = load_golden("parser_C.json")
    wrong = [
        (name, event["published_at"])
        for name, result in golden.items()
        for event in result.get("events", [])
        if event.get("published_at") is not None
    ]
    assert not wrong, f"у типа C published_at перестал быть None: {wrong[:5]}"
