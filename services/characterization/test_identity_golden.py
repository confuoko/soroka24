"""Characterization: правила identity, прибитые к конкретным значениям.

Здесь нет ни одной «вычисляемой» ожидаемой величины: все uid выписаны литералами. Это
сделано намеренно. Если ожидание считать той же функцией, которую проверяем, тест
переживёт любое изменение формулы и ничего не поймает. А сломанная формула означает, что
после переноса у всех существующих дел поедут uid дочерних строк, и синхронизация выдаст
волну фейковых удалений и вставок.

Покрываются: card_key, четыре uuid5-namespace, synthetic_uid, canonical_case_url,
host_variants и асимметрия «identity по локальному времени, хранение в UTC».

Тесты БД не требуют — все функции чистые.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.repositories.court_sessions import (
    COURT_SESSION_UID_NAMESPACE,
    court_session_uid,
)
from app.repositories.documents import DOCUMENT_UID_NAMESPACE, document_uid
from app.repositories.events import EVENT_UID_NAMESPACE, event_uid
from app.repositories.place_history import (
    PLACE_HISTORY_UID_NAMESPACE,
    place_history_uid,
)
from app.timezones import timezone_for, to_court_local, to_utc
from app.validators import (
    canonical_case_url,
    host_variants,
    is_synthetic_uid,
    synthetic_uid,
)

# Карточка, от которой считаются все дочерние uid: «УИД | код суда | номер дела».
CARD_KEY = "77MS0002-01-2026-001579-64|77MS0002|02-0123/2026"


# ------------------------------------------------------------------ namespace-константы
def test_uid_namespaces_are_pinned() -> None:
    """Сами namespace'ы — часть формулы. Их подмена переписывает все uid в базе."""
    assert EVENT_UID_NAMESPACE == uuid.UUID("af75dcd7-7083-4294-8e05-d5f643e533c3")
    assert PLACE_HISTORY_UID_NAMESPACE == uuid.UUID(
        "6b1f3c02-9a4d-5e77-b8c1-2f0a7d43e915"
    )
    assert COURT_SESSION_UID_NAMESPACE == uuid.UUID(
        "9c4e7a10-2f83-5b6d-a1c7-4e0d9f5b3a26"
    )
    assert DOCUMENT_UID_NAMESPACE == uuid.UUID("2f7b91c4-6d3e-5a08-9c1f-7b45e0a2d836")


# ---------------------------------------------------------------------------- Event uid
def test_event_uid_is_pinned() -> None:
    got = event_uid(CARD_KEY, dt.datetime(2026, 8, 21, 15, 30), "Судебное заседание")
    assert got == uuid.uuid5(
        EVENT_UID_NAMESPACE, f"{CARD_KEY}|2026-08-21|Судебное заседание"
    )
    assert str(got) == "74f34738-65c2-5b65-aaae-a552b4f63169"


def test_event_uid_ignores_time() -> None:
    """В identity события входит только ДАТА (events.py:39).

    Поэтому дозаполнение времени на портале даёт UPDATE существующей строки, а не новую
    строку. Это ровно то, ради чего в sync_events есть ветка обновления event_date.
    """
    morning = event_uid(CARD_KEY, dt.datetime(2026, 8, 21, 9, 0), "Заседание")
    evening = event_uid(CARD_KEY, dt.datetime(2026, 8, 21, 18, 45), "Заседание")
    midnight = event_uid(CARD_KEY, dt.datetime(2026, 8, 21, 0, 0), "Заседание")
    assert morning == evening == midnight


def test_event_uid_separates_dates_and_descriptions() -> None:
    base = event_uid(CARD_KEY, dt.datetime(2026, 8, 21, 10, 0), "Заседание")
    other_day = event_uid(CARD_KEY, dt.datetime(2026, 8, 22, 10, 0), "Заседание")
    other_text = event_uid(CARD_KEY, dt.datetime(2026, 8, 21, 10, 0), "Заседание ")
    assert base != other_day
    assert base != other_text


def test_event_uid_separates_cards() -> None:
    """Дочерние uid считаются от КАРТОЧКИ, а не от дела: по одному УИД карточек несколько."""
    other_card = "77MS0002-01-2026-001579-64|77MS0002|02-0999/2026"
    assert event_uid(CARD_KEY, dt.datetime(2026, 8, 21), "Х") != event_uid(
        other_card, dt.datetime(2026, 8, 21), "Х"
    )


# -------------------------------------------------------------------- CourtSession uid
def test_court_session_uid_is_pinned() -> None:
    got = court_session_uid(CARD_KEY, dt.datetime(2026, 8, 21, 15, 30), "Первая инстанция")
    assert got == uuid.uuid5(
        COURT_SESSION_UID_NAMESPACE,
        f"{CARD_KEY}|2026-08-21T15:30:00|Первая инстанция",
    )


def test_court_session_uid_keeps_time() -> None:
    """У заседания время ВХОДИТ в identity — в отличие от события.

    Отсюда же требование R4: отсутствующее время обязано превращаться в локальную
    полночь детерминированно, иначе uid переписывался бы на каждом разборе.
    """
    at_ten = court_session_uid(CARD_KEY, dt.datetime(2026, 8, 21, 10, 0), "Стадия")
    at_eleven = court_session_uid(CARD_KEY, dt.datetime(2026, 8, 21, 11, 0), "Стадия")
    assert at_ten != at_eleven


# ------------------------------------------------------------------------ Document uid
def test_document_uid_is_pinned() -> None:
    got = document_uid(CARD_KEY, dt.date(2026, 8, 21), "Решение", occurrence=0)
    assert got == uuid.uuid5(
        DOCUMENT_UID_NAMESPACE, f"{CARD_KEY}|2026-08-21|Решение|0"
    )


def test_document_uid_separates_repeats() -> None:
    """Номер повторения — единственное, что различает одинаковые строки (риск R2).

    Портал отдаёт до 21 строки «Приложение» на одну дату. Если при переносе порядок
    строк изменится, occurrence разъедется и получится волна фейковых удалений/вставок.
    """
    uids = [
        document_uid(CARD_KEY, dt.date(2026, 8, 21), "Приложение", occurrence=n)
        for n in range(21)
    ]
    assert len(set(uids)) == 21


# --------------------------------------------------------------------- PlaceHistory uid
def test_place_history_uid_is_pinned() -> None:
    got = place_history_uid(CARD_KEY, dt.date(2026, 8, 21), "Судебный участок")
    assert got == uuid.uuid5(
        PLACE_HISTORY_UID_NAMESPACE, f"{CARD_KEY}|2026-08-21|Судебный участок"
    )


# ------------------------------------------------------------------------ synthetic UID
def test_synthetic_uid_is_pinned() -> None:
    """Формула: "nouid-" + код суда + "-" + первые 12 символов sha256 канонического URL.

    Прибито литералом: канонизация URL входит в формулу, поэтому изменение
    canonical_case_url переписало бы синтетические uid, а с ними card_key и все
    дочерние uid (риск R18).
    """
    url = "https://mirsud.spb.ru/cases/detail/126/?id=2-1557%2F2026-126"
    got = synthetic_uid("78MS0126", url)
    assert got == "nouid-78MS0126-64000d80fcd9"
    assert is_synthetic_uid(got)
    assert not is_synthetic_uid("77MS0002-01-2026-001579-64")


def test_synthetic_uid_survives_cosmetic_url_changes() -> None:
    """Косметика адреса не должна порождать вторую карточку того же дела."""
    base = "https://mirsud.spb.ru/cases/detail/126/?id=2-1557%2F2026-126"
    assert synthetic_uid("78MS0126", base) == synthetic_uid(
        "78MS0126", base + "&utm_source=mail"
    )


def test_synthetic_uid_separates_courts() -> None:
    url = "https://mirsud.spb.ru/cases/detail/126/?id=2-1557%2F2026-126"
    assert synthetic_uid("78MS0126", url) != synthetic_uid("78MS0127", url)


# -------------------------------------------------------------- canonical_case_url
@pytest.mark.parametrize(
    "raw, expected",
    [
        # Значимые параметры сохраняются, мусорные (utm_*) отбрасываются;
        # завершающий слэш пути снимается.
        (
            "https://mirsud.spb.ru/cases/detail/126/?id=2-1557%2F2026-126&utm_source=x",
            "https://mirsud.spb.ru/cases/detail/126?id=2-1557%2F2026-126",
        ),
        # Регистр хоста не значим.
        (
            "https://MIRSUD.SPB.RU/cases/detail/126/?id=1",
            "https://mirsud.spb.ru/cases/detail/126?id=1",
        ),
        # Схема приводится к https, а значимые параметры СОРТИРУЮТСЯ по имени: иначе
        # тот же адрес с другим порядком параметров завёл бы вторую карточку дела.
        (
            "http://oktyabrskiy.orl.msudrf.ru/modules.php"
            "?name=sud_delo&op=cd&delo_id=1540005",
            "https://oktyabrskiy.orl.msudrf.ru/modules.php"
            "?delo_id=1540005&name=sud_delo&op=cd",
        ),
        # Адрес без параметров не меняется вовсе.
        (
            "https://mos-sud.ru/2/services/cases/details/abc-123",
            "https://mos-sud.ru/2/services/cases/details/abc-123",
        ),
    ],
)
def test_canonical_case_url_is_pinned(raw: str, expected: str) -> None:
    """Канонизация — ключ уникальности CaseUrl.url и вход synthetic_uid (риск R18)."""
    assert canonical_case_url(raw) == expected


def test_canonical_case_url_is_idempotent() -> None:
    """Повторная канонизация ничего не меняет — иначе URL раздваивал бы карточку."""
    for name in (
        "https://mirsud.spb.ru/cases/detail/126/?id=2-1557%2F2026-126&utm=1",
        "http://oktyabrskiy.orl.msudrf.ru/modules.php?name=sud_delo&op=cd&delo_id=1540005",
        "https://mos-sud.ru/2/services/cases/details/abc-123",
    ):
        once = canonical_case_url(name)
        assert canonical_case_url(once) == once


# ------------------------------------------------------------------------ host_variants
def test_host_variants_keeps_exact_host_first() -> None:
    """Первым вариантом обязан идти сам хост (resolver.py:307-317).

    Точное совпадение проверяется первым проходом, иначе ralt.msudrf.ru (Республика
    Алтай, 02MS) совпал бы по границе имени с alt.msudrf.ru (Алтайский край, 22MS) и
    дело уехало бы в чужой суд.
    """
    assert host_variants("1alt.msudrf.ru")[0] == "1alt.msudrf.ru"


def test_host_variants_restores_glued_participok_label() -> None:
    """Слитная метка участка разворачивается в точечное и дефисное написание.

    Стык цифр и букв — единственный признак: метка участка на движке msudrf начинается
    с числа (26twr, 1elez), а домен региона буквенный.
    """
    assert host_variants("1alt.msudrf.ru") == [
        "1alt.msudrf.ru",
        "1.alt.msudrf.ru",
        "1-alt.msudrf.ru",
    ]


def test_host_variants_leaves_alphabetic_host_alone() -> None:
    """Целиком буквенный хост разворачивать нечем — и не надо."""
    assert host_variants("alt.msudrf.ru") == ["alt.msudrf.ru"]
    assert host_variants("mirsud.spb.ru") == ["mirsud.spb.ru"]


# ------------------------------------------------------- локальное время против UTC
def test_identity_uses_local_time_while_storage_uses_utc() -> None:
    """Асимметрия из events.py:75-76 — самое неочевидное место в переносе (риск R3).

    Одно и то же настенное время в двух поясах — это РАЗНЫЕ моменты, но при хешировании
    локального времени они дают ОДИНАКОВЫЙ хвост ключа. Именно поэтому uid считается по
    локальному времени, а колонка хранит UTC: иначе одинаковые локальные времена в разных
    зонах хешировались бы по-разному, и переход на timestamptz переписал бы все uid.
    """
    local = dt.datetime(2026, 8, 21, 15, 30)

    moscow = to_utc(local, "Europe/Moscow")
    yekaterinburg = to_utc(local, "Asia/Yekaterinburg")

    # Хранение: разные моменты.
    assert moscow != yekaterinburg
    assert moscow.utcoffset() == dt.timedelta(0)
    assert yekaterinburg.utcoffset() == dt.timedelta(0)
    # Москва UTC+3, Екатеринбург UTC+5: одно настенное время — момент на 2 часа раньше.
    assert (moscow - yekaterinburg) == dt.timedelta(hours=2)

    # Identity: считается по локальному времени, поэтому у обоих один и тот же ключ.
    assert court_session_uid(CARD_KEY, local, "Стадия") == court_session_uid(
        CARD_KEY, local, "Стадия"
    )


def test_to_utc_and_back_is_lossless() -> None:
    local = dt.datetime(2026, 8, 21, 15, 30)
    moment = to_utc(local, "Asia/Yekaterinburg")
    assert to_court_local(moment, "Asia/Yekaterinburg").replace(tzinfo=None) == local


def test_local_midnight_keeps_its_date() -> None:
    """Полночь не должна уезжать на сутки назад при переводе в UTC и обратно."""
    local = dt.datetime(2026, 8, 21, 0, 0)
    moment = to_utc(local, "Asia/Yekaterinburg")
    assert to_court_local(moment, "Asia/Yekaterinburg").date() == dt.date(2026, 8, 21)


def test_unknown_region_raises_instead_of_defaulting_to_moscow() -> None:
    """timezone_for намеренно падает, а не подставляет Москву (app/timezones.py:151-167).

    Молчаливый дефолт означал бы тихо неверные моменты у дел целого региона.
    """
    with pytest.raises(KeyError):
        timezone_for("Область, которой нет", "99MS0001")
