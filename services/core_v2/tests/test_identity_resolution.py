"""Разрешение identity: чем опознаём карточку дела.

Сценарии взяты из ТЗ §26 «Identity»: настоящий УИД, самодельный УИД, существующая
карточка с самодельным УИД, появление настоящего УИД позже, номер дела, канонизация
адреса.

Самый важный тест здесь — test_real_uid_appearing_later_does_not_rekey_the_card. Он про
то, из-за чего вся эта функция и написана именно в таком порядке: если ключ карточки
поменять, uid событий и документов перестанут совпадать с сохранёнными, и обход выдаст
«всё удалено, всё создано заново».
"""
from __future__ import annotations

import pytest

from app.courts import CaseNotFound, FetchedCard
from app.models import Case
from app.repositories import CaseRepository
from app.services.identity import resolve_case_code, resolve_case_uid
from app.validators import canonical_case_url, is_synthetic_uid

pytestmark = pytest.mark.db

URL = "https://oktyabrskiy.orl.msudrf.ru/modules.php?name=sud_delo&op=cd&delo_id=1540005"
REAL_UID = "57MS0025-01-2026-001234-56"
OTHER_UID = "57MS0025-01-2026-009999-99"
CODE = "2-1244/2026"

CARD_WITH_UID = f"<html><body><h2>ДЕЛО № {CODE}</h2><p>{REAL_UID}</p></body></html>"
CARD_WITHOUT_UID = f"<html><body><h2>ДЕЛО № {CODE}</h2></body></html>"


def _case(session, court, uid: str, url: str = URL) -> Case:
    """Завести карточку с этим УИД и привязать к ней адрес."""
    case = Case(uid=uid, court=court, code=CODE)
    session.add(case)
    session.flush()
    CaseRepository(session).add_url(case, url)
    session.flush()
    return case


# ------------------------------------------------------------------- УИД со страницы
def test_real_uid_from_the_page_is_used(session, court) -> None:
    """Карточки в базе нет, на странице есть настоящий УИД — берём его."""
    assert resolve_case_uid(session, CARD_WITH_UID, URL, court.code) == REAL_UID


def test_missing_uid_becomes_a_synthetic_key(session, court) -> None:
    """УИД на странице нет вовсе — считаем ключ от адреса.

    Так устроены архивные дела движка msudrf.ru и целые регионы вроде Магаданской
    области. Без ключа карточку не сохранить, поэтому его приходится придумывать.
    """
    uid = resolve_case_uid(session, CARD_WITHOUT_UID, URL, court.code)

    assert is_synthetic_uid(uid)
    assert uid.startswith(f"nouid-{court.code}-")


def test_synthetic_key_is_stable_across_calls(session, court) -> None:
    """Один и тот же адрес всегда даёт один и тот же самодельный ключ.

    Иначе каждый обход заводил бы новую карточку.
    """
    first = resolve_case_uid(session, CARD_WITHOUT_UID, URL, court.code)
    second = resolve_case_uid(session, CARD_WITHOUT_UID, URL, court.code)
    assert first == second


def test_synthetic_key_ignores_cosmetic_url_changes(session, court) -> None:
    """Косметика адреса не порождает вторую карточку того же дела."""
    plain = resolve_case_uid(session, CARD_WITHOUT_UID, URL, court.code)
    with_utm = resolve_case_uid(
        session, CARD_WITHOUT_UID, URL + "&utm_source=mail", court.code
    )
    assert plain == with_utm


# ------------------------------------------- сохранённый УИД важнее найденного
def test_saved_uid_wins_over_the_page(session, court) -> None:
    """Карточка по этой ссылке уже есть — берём её УИД, а не тот, что на странице."""
    _case(session, court, OTHER_UID)

    assert resolve_case_uid(session, CARD_WITH_UID, URL, court.code) == OTHER_UID


def test_real_uid_appearing_later_does_not_rekey_the_card(session, court, caplog) -> None:
    """САМЫЙ ВАЖНЫЙ СЛУЧАЙ. Карточку завели без УИД, портал его дозаполнил.

    Ключ обязан остаться самодельным. Если подменить его настоящим УИД, поменяется
    Case.card_key, а от него считаются uid событий, документов, заседаний и
    местонахождений — все сохранённые строки перестали бы узнаваться, и следующий обход
    отчитался бы «всё удалено, всё создано заново».

    Расхождение при этом обязано попасть в лог: молча жить с ним нельзя.
    """
    synthetic = resolve_case_uid(session, CARD_WITHOUT_UID, URL, court.code)
    _case(session, court, synthetic)

    with caplog.at_level("WARNING"):
        resolved = resolve_case_uid(session, CARD_WITH_UID, URL, court.code)

    assert resolved == synthetic
    assert is_synthetic_uid(resolved)
    assert REAL_UID in caplog.text


def test_saved_uid_is_found_through_url_canonicalisation(session, court) -> None:
    """Адрес ищется в канонической форме: http против https карточку не раздваивает."""
    _case(session, court, OTHER_UID, url=URL)
    http_variant = URL.replace("https://", "http://") + "&utm_source=x"
    assert canonical_case_url(http_variant) == canonical_case_url(URL)

    assert resolve_case_uid(session, CARD_WITH_UID, http_variant, court.code) == OTHER_UID


def test_unknown_url_does_not_pick_up_a_foreign_card(session, court) -> None:
    """Чужой адрес не должен приводить к чужому УИД."""
    _case(session, court, OTHER_UID, url=URL)
    other_url = URL.replace("delo_id=1540005", "delo_id=9999999")

    assert resolve_case_uid(session, CARD_WITH_UID, other_url, court.code) == REAL_UID


# ------------------------------------------------------------------- номер дела
def test_case_code_known_from_navigation_is_not_reparsed() -> None:
    """Номер из таблицы результатов берём как есть — со страницы его не перечитываем.

    Так устроена Москва: номер стоит в строке таблицы, то есть известен до открытия
    карточки, а на самой карточке подписан по-разному в зависимости от вида
    производства.
    """
    fetched = FetchedCard(html="<html>ничего похожего на заголовок</html>", case_code="02-0123/2026")

    assert resolve_case_code("mos-sud", fetched) == "02-0123/2026"


def test_msudrf_case_code_comes_from_the_heading() -> None:
    """У движка номер только в заголовке карточки."""
    fetched = FetchedCard(html=CARD_WITH_UID)

    assert resolve_case_code("msudrf", fetched) == CODE


def test_spb_case_code_comes_from_the_heading() -> None:
    fetched = FetchedCard(
        html="<html><body>Судебное дело №2-2983/2026-98</body></html>"
    )

    assert resolve_case_code("spb", fetched) == "2-2983/2026-98"


def test_missing_case_code_is_case_not_found() -> None:
    """Без номера дело не сохранить — он в ключе карточки. Повторять поход бессмысленно."""
    with pytest.raises(CaseNotFound):
        resolve_case_code("msudrf", FetchedCard(html="<html><body></body></html>"))


def test_portal_without_a_way_to_learn_the_code_is_reported() -> None:
    """Портал не сказал номер и достать его со страницы мы не умеем — честный отказ."""
    with pytest.raises(CaseNotFound):
        resolve_case_code("mos-sud", FetchedCard(html=CARD_WITH_UID))
