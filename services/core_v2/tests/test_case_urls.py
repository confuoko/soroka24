"""Карточка дела = тройка «УИД + суд + номер дела», адресов к ней несколько.

УИД сквозной: он не меняется, когда дело идёт по инстанциям, поэтому один и тот же УИД
встречается на странице участка мирового судьи и на странице районного суда — это разные
карточки. В одном суде по одному УИД тоже бывает несколько карточек: приказное
производство и последовавшее исковое различаются номером дела.

А на одну карточку, наоборот, ведёт много внешне разных адресов.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Case, CaseUrl, Court, CourtLevel
from app.repositories.cases import CaseRepository
from app.validators import canonical_case_url

# Нужен настоящий PostgreSQL: JSONB у payload событий и FOR UPDATE ... SKIP
# LOCKED у пула прокси. Пропустить весь такой набор: pytest -m "not db".
pytestmark = pytest.mark.db

UID = "50MS0095-01-2026-002990-16"
CODE = "2-1585/2026"
CASE_URL = (
    "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs"
    "&case_id=429386415&delo_id=1540005"
)


@pytest.fixture
def other_court(session) -> Court:
    """Второй суд — чтобы проверить, что тот же УИД в нём уживается с первым."""
    row = Court(
        code="ZZ0000TEST",
        name="Тестовый районный суд",
        level=CourtLevel.GENERAL,
        region="Московская область",
        timezone="Europe/Moscow",
    )
    session.add(row)
    session.flush()
    return row


def _case(session, court, uid: str = UID, code: str = CODE) -> Case:
    case = Case(uid=uid, court=court, code=code)
    session.add(case)
    session.flush()
    return case


# ------------------------------------------------------------------- нормализация
@pytest.mark.parametrize(
    "raw",
    [
        CASE_URL,
        # схема, регистр хоста, хвостовой слеш
        "http://95.MO.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=429386415&delo_id=1540005",
        # другой порядок параметров
        "https://95.mo.msudrf.ru/modules.php?delo_id=1540005&case_id=429386415&op=cs&name=sud_delo",
        # лишние параметры и якорь
        CASE_URL + "&utm_source=mail#top",
        # пробелы по краям
        "  " + CASE_URL + "  ",
    ],
)
def test_same_card_gives_same_canonical_url(raw) -> None:
    """Все эти строки ведут на одну карточку и должны схлопываться в одну.

    Иначе каждая выглядела бы новой карточкой: лишняя задача с походом через прокси
    и капчу, а в базе — дубль.
    """
    assert canonical_case_url(raw) == canonical_case_url(CASE_URL)


def test_different_case_id_is_a_different_card() -> None:
    """А вот case_id — значащий параметр, его схлопывать нельзя."""
    other = CASE_URL.replace("429386415", "432884853")

    assert canonical_case_url(other) != canonical_case_url(CASE_URL)


# У Петербурга номер дела стоит в параметре id, а путь задаёт ТОЛЬКО участок.
SPB_URL = "https://mirsud.spb.ru/cases/detail/98/?id=2-2976%2F2026-98"


def test_spb_case_number_survives_canonicalization() -> None:
    """Параметр id у Петербурга значащий: без него дела участка схлопнутся в одно.

    Ловушка не теоретическая: id не входил в белый список, и два дела участка № 98
    давали один канонический адрес «/cases/detail/98». Поскольку url в case_url
    уникален глобально, второе дело после этого не сохранялось вовсе — задача падала
    с «адрес уже закреплён за карточкой».
    """
    other = SPB_URL.replace("2-2976", "2-2995")

    assert canonical_case_url(SPB_URL) != canonical_case_url(other)
    assert "2-2976" in canonical_case_url(SPB_URL)


def test_spb_url_still_drops_the_noise() -> None:
    """При этом мусор и хвостовой слеш у Петербурга убираются как везде."""
    assert canonical_case_url(SPB_URL + "&utm_source=mail#top") == canonical_case_url(
        SPB_URL
    )


# ----------------------------------------------------------- адреса одной карточки
def test_url_is_stored_in_canonical_form(session, court) -> None:
    """В таблицу попадает канонический вид, как бы адрес ни прислали."""
    case = _case(session, court)

    added = CaseRepository(session).add_url(
        case,
        "http://95.MO.msudrf.ru/modules.php/?op=cs&name=sud_delo"
        "&case_id=429386415&delo_id=1540005&utm_source=mail",
    )

    assert added.url == canonical_case_url(CASE_URL)


def test_add_url_is_idempotent(session, court) -> None:
    """Повторный вызов не плодит строк — иначе список адресов рос бы на каждый обход."""
    repo = CaseRepository(session)
    case = _case(session, court)

    first = repo.add_url(case, CASE_URL)
    second = repo.add_url(case, CASE_URL.replace("https://", "http://"))

    assert first.id == second.id
    assert len(repo.get_by_url(CASE_URL).urls) == 1


def test_one_url_cannot_belong_to_two_cards(session, court, other_court) -> None:
    """Один адрес ведёт ровно в одну карточку — иначе по нему нельзя опознать дело."""
    repo = CaseRepository(session)
    repo.add_url(_case(session, court), CASE_URL)

    with pytest.raises(ValueError, match="уже закреплён"):
        repo.add_url(_case(session, other_court), CASE_URL)


def test_case_is_found_by_any_of_its_urls(session, court) -> None:
    """Старый адрес после переезда участка должен продолжать находить карточку."""
    repo = CaseRepository(session)
    case = _case(session, court)
    repo.add_url(case, CASE_URL)
    repo.add_url(case, "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=1&delo_id=2")

    assert repo.get_by_url(CASE_URL).id == case.id
    assert repo.get_by_url("http://95.mo.msudrf.ru/modules.php?op=cs&name=sud_delo&case_id=1&delo_id=2").id == case.id


# ------------------------------------------------- какой ссылкой ходить в следующий раз
def test_primary_url_prefers_the_one_that_worked(session, court) -> None:
    """Ходим по той ссылке, по которой последний раз получилось.

    Рабочий адрес важнее просто известного: после переезда участка старый отвечать
    перестаёт, и упираться в него на каждом обходе смысла нет.
    """
    repo = CaseRepository(session)
    case = _case(session, court)
    old = repo.add_url(case, CASE_URL)
    fresh = repo.add_url(case, "https://96.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=7&delo_id=8")
    fresh.last_success_at = datetime.now(timezone.utc)
    session.flush()

    assert repo.primary_url(case) == fresh.url
    assert old.last_success_at is None


def test_primary_url_falls_back_to_the_freshest(session, court) -> None:
    """Не получалось ещё ни по одной — берём самую свежую.

    Из нерабочих больше шансов у той, которую прислали последней: старый адрес уже
    показал, что не отвечает.
    """
    repo = CaseRepository(session)
    case = _case(session, court)
    old = repo.add_url(case, CASE_URL)
    old.created_at = datetime.now(timezone.utc) - timedelta(days=1)
    fresh = repo.add_url(
        case, "https://96.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=7&delo_id=8"
    )
    session.flush()

    assert repo.primary_url(case) == fresh.url


def test_case_without_urls_has_no_primary(session, court) -> None:
    """У дел, заведённых по УИД, ссылок может не быть вовсе."""
    assert CaseRepository(session).primary_url(_case(session, court)) is None


# ------------------------------------------------------ идентичность карточки
def test_same_uid_in_two_courts_are_two_cards(session, court, other_court) -> None:
    """Дело перешло в другую инстанцию → вторая карточка с тем же УИД.

    Ровно ради этого УИД перестал быть уникальным сам по себе.
    """
    first = _case(session, court)
    second = _case(session, other_court)

    assert first.id != second.id
    assert {c.id for c in CaseRepository(session).list_by_uid(UID)} == {first.id, second.id}


def test_same_uid_and_court_with_other_code_are_two_cards(session, court) -> None:
    """В одном суде по одному УИД два производства → две карточки.

    Приказное производство отменили и завели исковое: УИД сквозной, суд тот же, номер
    дела новый. Ровно ради этого номер попал в ключ.
    """
    first = _case(session, court, code="2-1585/2026")
    second = _case(session, court, code="2-1777/2026")

    assert first.id != second.id
    assert {c.id for c in CaseRepository(session).list_by_uid_and_court(UID, court.id)} == {
        first.id,
        second.id,
    }


def test_same_uid_court_and_code_is_rejected(session, court) -> None:
    """А дважды завести карточку с той же тройкой нельзя — это и есть ключ."""
    _case(session, court)

    # Внутри savepoint: нарушение ограничения рвёт транзакцию, а внешнюю (её откатывает
    # фикстура после теста) ронять нельзя.
    with pytest.raises(IntegrityError), session.begin_nested():
        _case(session, court)


def test_card_is_found_by_uid_court_and_code(session, court, other_court) -> None:
    """Поиск идёт по тройке: по одному УИД карточек может быть несколько."""
    repo = CaseRepository(session)
    mine = _case(session, court)
    _case(session, other_court)
    _case(session, court, code="2-1777/2026")

    assert repo.get_by_uid_court_code(UID, court.id, CODE).id == mine.id
    # Тот же УИД и суд, но чужой номер — это другая карточка, а не эта.
    assert repo.get_by_uid_court_code(UID, court.id, "2-0000/2026") is None
    assert repo.get_by_uid_court_code(UID, 10**9, CODE) is None


def test_url_dies_with_its_card(session, court) -> None:
    """Удалили карточку — её адреса уходят следом (иначе останутся сироты)."""
    repo = CaseRepository(session)
    case = _case(session, court)
    repo.add_url(case, CASE_URL)

    session.delete(case)
    session.flush()

    assert session.query(CaseUrl).filter_by(url=canonical_case_url(CASE_URL)).count() == 0
