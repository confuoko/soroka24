"""Тесты диффа скалярных полей карточки дела.

Раньше upsert молча перезаписывал поля, поэтому смена «Текущего состояния» или
появление решения первой инстанции нигде не были видны. Теперь метод возвращает список
изменений — из него строятся события мониторинга (app/monitoring/outbox.py).

Карточка ищется по тройке «УИД + суд + номер дела»: УИД не уникален (не меняется при
переходе дела по инстанциям), и пара с судом тоже (в одном суде по УИД бывает несколько
производств).
"""
from datetime import date

from app.models.database import Case
from app.repositories.cases import CaseFieldChange, CaseRepository

CASE_UID = "77MS0002-01-2026-000007-77"
# Номер дела — часть ключа карточки, поэтому идёт отдельным аргументом, а не в data:
# его источник — таблица результатов поиска, а не разобранная страница.
CASE_CODE = "02-0634/2/2026"


FIRST_PARSE = {
    "status": "Зарегистрировано, 08.06.2026",
    "receipt_date": date(2026, 6, 8),
    "registration_date": None,
    "first_instance_date": None,
    "first_instance_decision": None,
    "decision_effective_date": None,
    "superior_case_number": None,
}


def test_new_case_has_no_field_changes(session, court) -> None:
    """У нового дела дифф полей пустой: появление дела — само по себе событие."""
    case, changes, _ = CaseRepository(session).upsert_by_uid_court_code(
        CASE_UID, court, CASE_CODE, FIRST_PARSE
    )

    assert changes == []
    assert case.code == CASE_CODE
    assert case.receipt_date == date(2026, 6, 8)


def test_changed_field_is_reported(session, court) -> None:
    """Сменился статус и появилось решение → оба поля в диффе, остальные молчат."""
    repo = CaseRepository(session)
    repo.upsert_by_uid_court_code(CASE_UID, court, CASE_CODE, FIRST_PARSE)
    session.flush()

    second = dict(
        FIRST_PARSE,
        status="Завершено, 30.07.2026",
        first_instance_date=date(2026, 7, 30),
        first_instance_decision="Удовлетворено, 30.07.2026",
    )

    case, changes, _ = repo.upsert_by_uid_court_code(CASE_UID, court, CASE_CODE, second)

    by_field = {c.field: c for c in changes}
    assert set(by_field) == {"status", "first_instance_date", "first_instance_decision"}
    assert by_field["status"] == CaseFieldChange(
        field="status", old="Зарегистрировано, 08.06.2026", new="Завершено, 30.07.2026"
    )
    assert by_field["first_instance_decision"].old is None
    assert case.first_instance_decision == "Удовлетворено, 30.07.2026"


def test_repeated_parse_gives_no_changes(session, court) -> None:
    """Страница не изменилась → дифф пустой. Иначе пользователь получал бы шум."""
    repo = CaseRepository(session)
    repo.upsert_by_uid_court_code(CASE_UID, court, CASE_CODE, FIRST_PARSE)
    session.flush()

    _, changes, _ = repo.upsert_by_uid_court_code(CASE_UID, court, CASE_CODE, dict(FIRST_PARSE))

    assert changes == []


def test_value_disappearing_from_page_is_reported(session, court) -> None:
    """Метка пропала со страницы → поле обнуляется, и это тоже изменение.

    Парсер отдаёт ВСЕ ключи карточки (отсутствующая метка приходит как None), поэтому
    стухшее значение не остаётся в БД. Это же свойство разводит «Дату поступления» и
    «Дату регистрации» при переходе на раздельные поля.
    """
    repo = CaseRepository(session)
    repo.upsert_by_uid_court_code(CASE_UID, court, CASE_CODE, FIRST_PARSE)
    session.flush()

    moved = dict(FIRST_PARSE, receipt_date=None, registration_date=date(2026, 6, 8))

    case, changes, _ = repo.upsert_by_uid_court_code(CASE_UID, court, CASE_CODE, moved)

    by_field = {c.field: c for c in changes}
    assert by_field["receipt_date"].new is None
    assert by_field["registration_date"].new == date(2026, 6, 8)
    assert case.receipt_date is None


def test_acceptance_date_is_stored_and_diffed(session, court) -> None:
    """«Дата принятия к производству» — самостоятельное поле, живёт рядом с поступлением.

    Обе даты стоят на одной карточке Петербурга и могут расходиться, поэтому важно,
    что accepted_date обновляется и попадает в дифф: сдвиг принятия к производству
    пользователь должен увидеть, а не получить молча перезаписанное значение.
    """
    repo = CaseRepository(session)
    repo.upsert_by_uid_court_code(
        CASE_UID, court, CASE_CODE, dict(FIRST_PARSE, accepted_date=date(2026, 6, 8))
    )
    session.flush()

    case, changes, _ = repo.upsert_by_uid_court_code(
        CASE_UID, court, CASE_CODE, dict(FIRST_PARSE, accepted_date=date(2026, 6, 11))
    )

    by_field = {c.field: c for c in changes}
    assert by_field["accepted_date"].old == date(2026, 6, 8)
    assert by_field["accepted_date"].new == date(2026, 6, 11)
    # Дата поступления при этом своя и не поехала.
    assert case.receipt_date == date(2026, 6, 8)
    assert case.accepted_date == date(2026, 6, 11)


def test_missing_key_leaves_field_untouched(session, court) -> None:
    """Ключа нет в data вообще → поле не трогаем и в дифф не пишем.

    Так ведёт себя парсер другого типа страницы, который часть меток не отдаёт.
    """
    repo = CaseRepository(session)
    repo.upsert_by_uid_court_code(CASE_UID, court, CASE_CODE, FIRST_PARSE)
    session.flush()

    case, changes, _ = repo.upsert_by_uid_court_code(
        CASE_UID, court, CASE_CODE, {"status": "Завершено, 30.07.2026"}
    )

    assert [c.field for c in changes] == ["status"]
    assert case.receipt_date == date(2026, 6, 8)  # не обнулилась


def test_same_uid_and_court_with_other_code_is_another_card(session, court) -> None:
    """Тот же УИД в том же суде, но с другим номером → ВТОРАЯ карточка, а не правка первой.

    Так выглядит приказное производство, которое отменили и завели исковое: УИД сквозной,
    суд тот же, а номер дела новый. Раньше вторая карточка затирала первую.
    """
    repo = CaseRepository(session)
    first, _, _ = repo.upsert_by_uid_court_code(CASE_UID, court, CASE_CODE, FIRST_PARSE)
    session.flush()

    second, changes, _ = repo.upsert_by_uid_court_code(
        CASE_UID, court, "02-0777/2/2026", FIRST_PARSE
    )
    session.flush()

    assert second.id != first.id
    assert changes == []  # это новая карточка, а не изменение старой
    assert {c.code for c in repo.list_by_uid_and_court(CASE_UID, court.id)} == {
        CASE_CODE,
        "02-0777/2/2026",
    }
