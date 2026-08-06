"""Тесты диффа скалярных полей карточки дела.

Раньше upsert молча перезаписывал поля, поэтому смена «Текущего состояния» или
появление решения первой инстанции нигде не были видны — ни в Case.diff_history, ни в
логах. Теперь метод возвращает список изменений.

Карточка ищется по паре «УИД + суд»: сам по себе УИД не уникален, потому что не
меняется при переходе дела по инстанциям.
"""
from datetime import date

from app.models.database import Case
from app.repositories.cases import CaseFieldChange, CaseRepository

CASE_UID = "77MS0002-01-2026-000007-77"


FIRST_PARSE = {
    "code": "02-0634/2/2026",
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
    case, changes = CaseRepository(session).upsert_by_uid_and_court(CASE_UID, court, FIRST_PARSE)

    assert changes == []
    assert case.code == "02-0634/2/2026"
    assert case.receipt_date == date(2026, 6, 8)


def test_changed_field_is_reported(session, court) -> None:
    """Сменился статус и появилось решение → оба поля в диффе, остальные молчат."""
    repo = CaseRepository(session)
    repo.upsert_by_uid_and_court(CASE_UID, court, FIRST_PARSE)
    session.flush()

    second = dict(
        FIRST_PARSE,
        status="Завершено, 30.07.2026",
        first_instance_date=date(2026, 7, 30),
        first_instance_decision="Удовлетворено, 30.07.2026",
    )

    case, changes = repo.upsert_by_uid_and_court(CASE_UID, court, second)

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
    repo.upsert_by_uid_and_court(CASE_UID, court, FIRST_PARSE)
    session.flush()

    _, changes = repo.upsert_by_uid_and_court(CASE_UID, court, dict(FIRST_PARSE))

    assert changes == []


def test_value_disappearing_from_page_is_reported(session, court) -> None:
    """Метка пропала со страницы → поле обнуляется, и это тоже изменение.

    Парсер отдаёт ВСЕ ключи карточки (отсутствующая метка приходит как None), поэтому
    стухшее значение не остаётся в БД. Это же свойство разводит «Дату поступления» и
    «Дату регистрации» при переходе на раздельные поля.
    """
    repo = CaseRepository(session)
    repo.upsert_by_uid_and_court(CASE_UID, court, FIRST_PARSE)
    session.flush()

    moved = dict(FIRST_PARSE, receipt_date=None, registration_date=date(2026, 6, 8))

    case, changes = repo.upsert_by_uid_and_court(CASE_UID, court, moved)

    by_field = {c.field: c for c in changes}
    assert by_field["receipt_date"].new is None
    assert by_field["registration_date"].new == date(2026, 6, 8)
    assert case.receipt_date is None


def test_missing_key_leaves_field_untouched(session, court) -> None:
    """Ключа нет в data вообще → поле не трогаем и в дифф не пишем.

    Так ведёт себя парсер другого типа страницы, который часть меток не отдаёт.
    """
    repo = CaseRepository(session)
    repo.upsert_by_uid_and_court(CASE_UID, court, FIRST_PARSE)
    session.flush()

    case, changes = repo.upsert_by_uid_and_court(CASE_UID, court, {"status": "Завершено, 30.07.2026"})

    assert [c.field for c in changes] == ["status"]
    assert case.code == "02-0634/2/2026"  # не обнулился
