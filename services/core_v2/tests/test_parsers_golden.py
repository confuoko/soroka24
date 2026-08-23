"""Парсеры отдают то же самое, что отдавал старый core. Побайтово.

Это главная проверка фазы переноса парсеров. Файлы в tests/golden/ — снимки вывода
СТАРЫХ парсеров по каждой из 80 сохранённых страниц, снятые до рефакторинга
(services/characterization/generate_golden.py). Здесь по тем же страницам прогоняются
НОВЫЕ парсеры, и результат обязан совпасть.

Парсеры содержат накопленное знание о реальных судебных сайтах: терпимость к латинским
двойникам в русских метках, поиск колонок по тексту шапки, порядок проверки меток,
скрытые мобильные клоны таблиц. Ни одно из этих правил не выводится из общих
соображений — каждое появилось из-за конкретной страницы конкретного суда. Golden-файлы
существуют, чтобы такое правило нельзя было потерять незаметно.

Каждый парсер прогоняется по КАЖДОЙ странице, а не только по «своим». Так зафиксировано и
поведение на чужой вёрстке: страница типа C, разобранная как B, кладёт текст заголовка
в статус и обходит охранник пустого разбора — такое поведение тоже часть контракта.

Единственное известное расхождение с golden описано в PUBLISHED_AT_ADDED_TO_TYPE_A.
Любое другое означает регрессию.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import pytest
from snapshot import encode

from app.parsers.moscow_type_a import MoscowTypeAParser
from app.parsers.msudrf_shared import detect_page_type
from app.parsers.msudrf_type_b import MsudrfTypeBParser
from app.parsers.msudrf_type_c import MsudrfTypeCParser
from app.parsers.parsed_case import ParsedCase
from app.parsers.spb_type_d import SpbTypeDParser

CORE_V2_ROOT = Path(__file__).resolve().parents[1]
HTML_DIR = CORE_V2_ROOT / "html_examples"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

PARSERS = {
    "A": MoscowTypeAParser,
    "B": MsudrfTypeBParser,
    "C": MsudrfTypeCParser,
    "D": SpbTypeDParser,
}
PAGE_TYPES = sorted(PARSERS)

# Не карточка дела и не в UTF-8: выгрузка списка судов с sudrf.ru в windows-1251 (4 МБ),
# её читает только скрипт сборки справочника. Парсерам карточек её давать незачем.
NOT_A_CASE_PAGE = frozenset({"mir_court_list_full.html"})


def html_pages() -> dict[str, str]:
    """Все сохранённые страницы дел: имя файла -> HTML."""
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(HTML_DIR.glob("*.html"))
        if path.name not in NOT_A_CASE_PAGE
    }


def load_golden(name: str) -> dict:
    path = GOLDEN_DIR / name
    if not path.exists():
        pytest.fail(f"нет golden-файла {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def as_legacy_dict(parsed: ParsedCase) -> dict[str, Any]:
    """Привести ParsedCase к форме старого dict — ТОЛЬКО для сверки с golden.

    Это не часть рабочего кода и не «слой преобразования»: в бою ParsedCase уходит в
    sync_case как есть. Здесь он разворачивается обратно в словарь единственно затем,
    чтобы сравнить с тем, что отдавал старый парсер.

    Поля со значением UNSET не попадают в словарь — ровно так же, как в старом core
    отсутствовал соответствующий ключ. Именно это и надо проверить.
    """
    result: dict[str, Any] = dict(parsed.card_fields())
    result["judge_names"] = list(parsed.judge_names)
    for name in ("sides", "events", "place_history", "court_sessions", "documents"):
        rows = getattr(parsed, name)
        result[name] = [asdict(row) if is_dataclass(row) else row for row in rows]
    return result


# Единственное намеренное расхождение с golden.
#
# OLD:    у типа A в событиях ключа published_at не было ВООБЩЕ — карточка мировых судов
#         Москвы колонки «Дата размещения» не имеет, и парсер этот ключ не заводил.
# NEW:    ParsedEvent.published_at существует всегда и у типа A равен None.
# REASON: типизированная строка события не может иметь разный набор полей в зависимости
#         от портала. На запись это не влияет: published_at не участвует ни в identity
#         события (event_uid берёт только дату), ни в сверке скалярных полей дела —
#         репозиторий читал его через .get(), то есть отсутствие ключа и None всегда
#         означали для него одно и то же.
PUBLISHED_AT_ADDED_TO_TYPE_A = True


def normalize_golden_events(page_type: str, golden_result: dict) -> dict:
    """Дописать в golden типа A published_at=None — см. PUBLISHED_AT_ADDED_TO_TYPE_A."""
    if page_type != "A":
        return golden_result
    events = golden_result.get("events")
    if not events:
        return golden_result
    patched = dict(golden_result)
    patched["events"] = [{"published_at": None, **event} for event in events]
    return patched


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_parser_output_matches_golden(page_type: str) -> None:
    """Вывод нового парсера совпадает с выводом старого — поле в поле.

    Сравниваем разом все 80 страниц, а не по одной на тест: при расхождении важно видеть,
    сколько страниц поехало. Одна — правка селектора; все — сломался общий хелпер.
    """
    golden = load_golden(f"parser_{page_type}.json")
    parser_class = PARSERS[page_type]
    pages = html_pages()

    mismatched: list[str] = []
    for name in sorted(golden):
        actual = encode(as_legacy_dict(parser_class().parse(pages[name])))
        expected = normalize_golden_events(page_type, golden[name])
        if actual != expected:
            mismatched.append(name)

    assert not mismatched, (
        f"парсер {page_type} расходится со старым на {len(mismatched)} страницах "
        f"из {len(golden)}: {mismatched[:10]}"
    )


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_golden_covers_every_saved_page(page_type: str) -> None:
    """Golden не должен незаметно потерять страницы."""
    assert len(load_golden(f"parser_{page_type}.json")) == 80


def test_all_saved_pages_are_readable() -> None:
    """Фикстуры на месте и читаются: 80 карточек плюс одна не-карточка."""
    assert len(html_pages()) == 80
    assert len(list(HTML_DIR.glob("*.html"))) == 81


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_parser_never_raises(page_type: str) -> None:
    """Ни одна страница не должна ронять парсер (даже чужая или недорендеренная).

    Требование из докстринга CaseParser. Проверяется на всех 80 страницах каждым из
    четырёх парсеров — то есть на 320 сочетаниях.
    """
    parser_class = PARSERS[page_type]
    for name, html in html_pages().items():
        try:
            parser_class().parse(html)
        except Exception as exc:  # noqa: BLE001 — именно это и проверяем
            pytest.fail(f"парсер {page_type} упал на {name}: {type(exc).__name__}: {exc}")


def test_empty_document_gives_empty_result() -> None:
    """Недорендеренная страница — пустой разбор, а не исключение.

    Сохранение пустого разбора затёрло бы события, судей и стороны существующей
    карточки, поэтому выше по стеку стоит охранник is_empty(). Но сначала парсер обязан
    вообще доработать до конца.
    """
    for page_type, parser_class in sorted(PARSERS.items()):
        parsed = parser_class().parse("<html><head></head><body></body></html>")
        assert parsed.is_empty(), page_type
        assert parsed.events == []
        assert parsed.sides == []
        assert parsed.judge_names == []


# ------------------------------------------------------------- определение вёрстки
def test_detect_page_type_matches_golden() -> None:
    """Определение вёрстки msudrf — дословно как в старом core.

    Им выбирается парсер для страниц движка, поэтому поведение обязано пережить перенос
    без изменений.
    """
    golden = load_golden("detect_page_type.json")
    pages = html_pages()
    mismatched = {
        name: (expected, detect_page_type(pages[name]))
        for name, expected in sorted(golden.items())
        if detect_page_type(pages[name]) != expected
    }
    assert not mismatched, f"detect_page_type изменился: {mismatched}"


def test_detect_page_type_distribution_is_pinned() -> None:
    """Сколько страниц каким типом опознаётся — тоже часть контракта."""
    counts: dict[str, int] = {}
    for value in load_golden("detect_page_type.json").values():
        counts[value or "не карточка"] = counts.get(value or "не карточка", 0) + 1
    assert counts == {"B": 55, "C": 13, "не карточка": 12}


# --------------------------------------------------- UNSET против None (риск R1)
# Поля, которых у парсера нет НИ НА ОДНОЙ сохранённой странице. Проверяем не по golden,
# а по живому выводу: именно здесь typed ParsedCase мог бы всё испортить, начав отдавать
# None вместо отсутствия — и тогда у половины дел молча обнулились бы колонки.
FIELDS_NEVER_PROVIDED = {
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
        "first_instance_date",
        "first_instance_decision",
        "decision_effective_date",
    ),
}


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_fields_the_parser_never_reports_stay_unset(page_type: str) -> None:
    """Такое поле обязано остаться UNSET, иначе колонка в БД обнулится."""
    parser_class = PARSERS[page_type]
    leaked: list[str] = []
    for name, html in html_pages().items():
        provided = parser_class().parse(html).card_fields()
        for field_name in FIELDS_NEVER_PROVIDED[page_type]:
            if field_name in provided:
                leaked.append(f"{field_name} на странице {name}")
    assert not leaked, (
        f"парсер {page_type} прислал поля, которых у его портала не бывает: {leaked[:5]}"
    )


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_card_fields_never_contains_unset(page_type: str) -> None:
    """card_fields отдаёт только реально присланное — UNSET внутрь просочиться не может."""
    from app.parsers.parsed_case import UNSET

    parser_class = PARSERS[page_type]
    for html in html_pages().values():
        assert UNSET not in parser_class().parse(html).card_fields().values()
