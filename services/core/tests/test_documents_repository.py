"""Тесты сверки документов дела со страницей.

Особенность этой сущности: портал отдаёт по несколько ПОЛНОСТЬЮ одинаковых строк за одну
дату (у дела 77MS0002-01-2026-001597-10 — 21 «Приложение» за 17.07.2026), и различить их в
разметке нечем. Поэтому в identity входит номер повторения строки на странице, а хранить
надо все. Ветки updated у документа нет: изменяемых полей не осталось.
"""
from datetime import date

from app.models.database import Case
from app.repositories.documents import DocumentRepository, document_uid

CASE_UID = "77MS0002-01-2026-000006-66"

# Как у 77MS0002-01-2026-001597-10: пачка одинаковых приложений плюс отдельные документы.
PAGE_ROWS = (
    [{"document_date": date(2026, 7, 17), "document_type": "Приложение"} for _ in range(21)]
    + [
        {"document_date": date(2026, 7, 17), "document_type": "Квитанция об оплате госпошлины"},
        {"document_date": date(2026, 7, 20), "document_type": "Судебный приказ"},
    ]
)


def _case(session, court) -> Case:
    case = Case(uid=CASE_UID, court=court)
    session.add(case)
    session.flush()
    return case


def test_identical_rows_all_saved(session, court) -> None:
    """21 одинаковая строка → 21 запись с разными uid, flush без IntegrityError."""
    case = _case(session, court)

    new, removed = DocumentRepository(session).sync_documents(case, PAGE_ROWS)
    session.flush()

    assert len(new) == 23
    assert removed == []
    assert len(case.documents) == 23
    # Уникальность держит ix_document_uid — значит все uid обязаны различаться.
    assert len({d.uid for d in case.documents}) == 23

    attachments = [d for d in case.documents if d.document_type == "Приложение"]
    assert len(attachments) == 21


def test_second_sync_of_same_page_changes_nothing(session, court) -> None:
    """Повторный парсинг той же страницы не даёт диффа.

    Главная проверка: нестабильный номер повторения давал бы «новый документ» на каждом
    обходе — 21 ложное уведомление по одному делу.
    """
    case = _case(session, court)
    repo = DocumentRepository(session)
    repo.sync_documents(case, PAGE_ROWS)
    session.flush()

    new, removed = repo.sync_documents(case, PAGE_ROWS)

    assert (new, removed) == ([], [])
    assert len(case.documents) == 23


def test_new_attachment_added_at_the_end(session, court) -> None:
    """Добавилось ещё одно такое же приложение → ровно один новый документ."""
    case = _case(session, court)
    repo = DocumentRepository(session)
    repo.sync_documents(case, PAGE_ROWS)
    session.flush()

    grown = PAGE_ROWS + [
        {"document_date": date(2026, 7, 17), "document_type": "Приложение"}
    ]

    new, removed = repo.sync_documents(case, grown)
    session.flush()

    assert len(new) == 1
    assert removed == []
    assert new[0].uid == document_uid(
        CASE_UID, date(2026, 7, 17), "Приложение", occurrence=21
    )
    assert len(case.documents) == 24


def test_document_gone_from_page_is_removed(session, court) -> None:
    """Документ пропал со страницы → удаляем: страница — источник истины."""
    case = _case(session, court)
    repo = DocumentRepository(session)
    repo.sync_documents(case, PAGE_ROWS)
    session.flush()

    new, removed = repo.sync_documents(case, PAGE_ROWS[:-1])
    session.flush()

    assert new == []
    assert len(removed) == 1
    assert removed[0].document_type == "Судебный приказ"
    assert len(case.documents) == 22


def test_other_types_do_not_shift_the_counter(session, court) -> None:
    """Документ другого вида, вставленный выше, не сдвигает uid приложений.

    Счётчик повторений ведётся внутри группы (дата, вид), а не по всей таблице — иначе
    появление одной строки выше «сдвинуло» бы uid всех соседей и повторный парсинг
    посчитал бы их удалёнными.
    """
    case = _case(session, court)
    repo = DocumentRepository(session)
    repo.sync_documents(case, PAGE_ROWS)
    session.flush()
    uids_before = {d.uid for d in case.documents}

    reordered = [
        {"document_date": date(2026, 7, 17), "document_type": "Извещение"}
    ] + PAGE_ROWS

    new, removed = repo.sync_documents(case, reordered)
    session.flush()

    assert len(new) == 1
    assert new[0].document_type == "Извещение"
    assert removed == []
    # Все прежние документы сохранили свои uid.
    assert uids_before <= {d.uid for d in case.documents}
