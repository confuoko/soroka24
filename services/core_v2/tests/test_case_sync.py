"""CaseSync: единая операция приведения БД к состоянию страницы суда.

Перенос services/characterization/test_sync_and_outbox.py, снятого со старого core до
рефакторинга. Из него убрана outbox-часть: сериализация изменений в события
(changes_to_events) переносится отдельной фазой, а тесты на baseline и атомарность
переедут вместе с ней.

Здесь фиксируется:

* первый импорт — что считается baseline;
* повторная сверка без изменений — нулевой diff;
* new / updated / removed по дочерним сущностям;
* реконсиляция судей и сторон;
* два разных смысла отсутствующего ключа и ключа со значением None;
* порядок строк документов как часть identity.

Тестам нужен настоящий PostgreSQL.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.parsers.parsed_case import (
    UNSET,
    ParsedCase,
    ParsedDocument,
    ParsedEvent,
    ParsedPlace,
    ParsedSession,
    ParsedSide,
)
from app.services import sync_case

pytestmark = pytest.mark.db

CODE = "02-0123/2026"
UID = "77MS0002-01-2026-001579-64"
URL = "https://mos-sud.ru/2/services/cases/details/char-test-1"


def page(**overrides) -> ParsedCase:
    """Разбор страницы: минимальная карточка со всеми видами дочерних строк.

    Выставлены ровно три скалярных поля. Остальные девять остаются UNSET — так ведёт себя
    настоящий парсер, у портала которого таких меток нет, и колонки по ним в БД не
    трогаются. Тесты ниже этим и пользуются: page(category=None) отличается от того,
    чтобы вообще не передавать category.

    Адреса страницы здесь нет: он не содержимое карточки, а знание того, кто ходил на
    портал, и уходит в sync_case отдельным аргументом.
    """
    parsed = ParsedCase(
        status="Рассмотрение",
        category="Гражданские дела",
        receipt_date=dt.date(2026, 8, 1),
        judge_names=["Иванов И.И."],
        sides=[ParsedSide(role="Истец", full_name="Петров П.П.")],
        events=[
            ParsedEvent(
                event_date=dt.datetime(2026, 8, 10, 10, 0),
                state_description="Регистрация",
                document_str=None,
                published_at=None,
            ),
        ],
        place_history=[
            ParsedPlace(
                place_date=dt.date(2026, 8, 10),
                place_description="Судебный участок",
                comment=None,
            ),
        ],
        court_sessions=[
            ParsedSession(
                session_date=dt.datetime(2026, 8, 20, 15, 30),
                place="зал 1",
                stage="Первая инстанция",
                result=None,
                basis=None,
            ),
        ],
        documents=[
            ParsedDocument(document_date=dt.date(2026, 8, 21), document_type="Решение"),
        ],
    )
    for name, value in overrides.items():
        setattr(parsed, name, value)
    return parsed


def sync(session, court, parsed: ParsedCase):
    return sync_case(session, UID, parsed, court, CODE, source_url=URL)


# ------------------------------------------------------------------ первый импорт
def test_first_import_reports_everything_as_new(session, court) -> None:
    changes = sync(session, court, page())

    assert changes.is_new is True
    assert len(changes.new_events) == 1
    assert len(changes.new_places) == 1
    assert len(changes.new_sessions) == 1
    assert len(changes.new_documents) == 1
    assert len(changes.added_judges) == 1
    assert len(changes.added_sides) == 1
    # У новой карточки отдельных изменений полей не бывает: сравнивать не с чем.
    assert changes.field_changes == []


def test_first_import_is_marked_as_baseline(session, court) -> None:
    """Первый импорт помечается признаком is_new — на нём событий не выпускают.

    Здесь проверяется только сам признак: подавление событий по нему живёт в outbox,
    который переносится отдельной фазой. Смысл признака в том, что на первом обходе вся
    карточка формально «новая» (десятки строк истории, заседания, документы), и выпускать
    по ним события об ИЗМЕНЕНИЯХ было бы неправдой.
    """
    changes = sync(session, court, page())
    assert changes.is_new is True


def test_case_fields_are_saved_on_first_import(session, court) -> None:
    changes = sync(session, court, page())
    case = changes.case
    assert case.status == "Рассмотрение"
    assert case.category == "Гражданские дела"
    assert case.receipt_date == dt.date(2026, 8, 1)
    assert case.card_key == f"{UID}|{court.code}|{CODE}"


def test_calendar_date_stays_a_date(session, court) -> None:
    """Дата поступления обязана остаться date, а не стать полночью (ТЗ PRIORITY 25)."""
    case = sync(session, court, page()).case
    assert isinstance(case.receipt_date, dt.date)
    assert not isinstance(case.receipt_date, dt.datetime)


def test_local_datetime_is_stored_as_utc(session, court) -> None:
    """Парсер отдал наивное локальное время — в базе лежит UTC-aware (риск R3)."""
    changes = sync(session, court, page())
    event = changes.new_events[0]
    assert event.event_date.tzinfo is not None
    assert event.event_date.utcoffset() == dt.timedelta(0)
    # 10:00 по Москве — это 07:00 UTC.
    assert event.event_date.hour == 7


# ------------------------------------------------------ повторная сверка без изменений
def test_second_sync_without_changes_reports_nothing(session, court) -> None:
    """Ключевое свойство: обход неизменившейся страницы даёт нулевой diff."""
    sync(session, court, page())
    session.flush()

    again = sync(session, court, page())

    assert again.is_new is False
    assert again.has_changes() is False
    assert again.field_changes == []
    assert again.new_events == [] and again.updated_events == []
    assert again.removed_events == []
    assert again.new_documents == [] and again.removed_documents == []
    assert again.added_judges == [] and again.removed_judges == []
    assert again.added_sides == [] and again.removed_sides == []


def test_repeated_sync_keeps_child_uids(session, court) -> None:
    """uid дочерних строк детерминированы — повторный обход их не переписывает."""
    first = sync(session, court, page())
    first_uid = first.new_events[0].uid
    session.flush()

    sync(session, court, page())
    session.flush()

    stored = first.case.events
    assert len(stored) == 1
    assert stored[0].uid == first_uid


# ------------------------------------------------------------------------ new / updated
def test_new_event_is_detected(session, court) -> None:
    sync(session, court, page())
    session.flush()

    grown = page()
    grown.events = grown.events + [
        ParsedEvent(event_date=dt.datetime(2026, 8, 15, 11, 0), state_description="Судебное заседание", document_str=None, published_at=None),
    ]
    changes = sync(session, court, grown)

    assert len(changes.new_events) == 1
    assert changes.new_events[0].state_description == "Судебное заседание"
    assert changes.removed_events == []



def test_event_time_filled_in_later_is_an_update_not_a_new_row(session, court) -> None:
    """Дозаполнение времени = UPDATE: в identity события входит только дата (риск R3)."""
    sync(session, court, page())
    session.flush()

    later = page()
    later.events[0].event_date = dt.datetime(2026, 8, 10, 16, 45)
    changes = sync(session, court, later)

    assert changes.new_events == []
    assert changes.removed_events == []
    assert len(changes.updated_events) == 1


def test_removed_event_is_detected(session, court) -> None:
    sync(session, court, page())
    session.flush()

    shrunk = page()
    shrunk.events = []
    changes = sync(session, court, shrunk)

    assert len(changes.removed_events) == 1
    assert changes.new_events == []



def test_case_field_change_is_reported_with_old_and_new(session, court) -> None:
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(status="Рассмотрено"))

    assert len(changes.field_changes) == 1
    change = changes.field_changes[0]
    assert (change.field, change.old, change.new) == (
        "status",
        "Рассмотрение",
        "Рассмотрено",
    )


def test_unset_field_leaves_the_column_alone(session, court) -> None:
    """Риск R1, ядро вопроса: UNSET ≠ None.

    Первый обход записал категорию. На втором поле пришло со значением UNSET — так ведёт
    себя парсер, у портала которого такой метки не бывает вовсе. Колонка обязана остаться
    прежней. Если бы вместо UNSET пришёл None, категория обнулилась бы — и это было бы
    правильно, потому что None означает «метка со страницы исчезла».
    """
    first = sync(session, court, page())
    session.flush()
    assert first.case.category == "Гражданские дела"

    without_key = page()
    # Именно UNSET, а не None: «у этого портала такой метки не бывает».
    without_key.category = UNSET
    changes = sync(session, court, without_key)

    assert changes.case.category == "Гражданские дела"
    assert [c.field for c in changes.field_changes] == []


def test_none_nulls_the_column(session, court) -> None:
    """Обратная половина R1: значение None — метка со страницы исчезла."""
    sync(session, court, page())
    session.flush()

    changes = sync(session, court, page(category=None))

    assert changes.case.category is None
    assert [c.field for c in changes.field_changes] == ["category"]


# ------------------------------------------------------------- судьи и стороны
def test_judge_and_side_reconciliation(session, court) -> None:
    sync(session, court, page())
    session.flush()

    changed = page(
        judge_names=["Сидоров С.С."],
        sides=[ParsedSide(role="Ответчик", full_name="Петров П.П.")],
    )
    changes = sync(session, court, changed)

    assert [j.full_name for j in changes.added_judges] == ["Сидоров С.С."]
    assert [j.full_name for j in changes.removed_judges] == ["Иванов И.И."]
    # Ключ стороны — (ФИО, роль): та же фамилия с другой ролью это ДРУГАЯ сторона.
    assert [s.role for s in changes.added_sides] == ["Ответчик"]
    assert [s.role for s in changes.removed_sides] == ["Истец"]


# ------------------------------------------------- порядок документов (риск R2)
def test_identical_document_rows_get_distinct_uids_by_position(session, court) -> None:
    """21 одинаковая строка «Приложение» — 21 разная строка в базе.

    Различает их только номер повторения, а он считается по позиции на странице.
    Поэтому порядок строк, который отдал парсер, менять нельзя.
    """
    many = page()
    many.documents = [
        ParsedDocument(document_date=dt.date(2026, 8, 21), document_type="Приложение")
        for _ in range(21)
    ]
    changes = sync(session, court, many)

    assert len(changes.new_documents) == 21
    assert len({d.uid for d in changes.new_documents}) == 21


def test_duplicate_event_rows_collapse_into_one(session, court) -> None:
    """Дубль со страницы гасится: повторная вставка того же uid уронила бы транзакцию."""
    doubled = page()
    doubled.events = doubled.events * 2
    changes = sync(session, court, doubled)

    assert len(changes.new_events) == 1
