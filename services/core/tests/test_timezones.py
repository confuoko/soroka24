"""Часовые пояса судов и стабильность ключей событий и заседаний.

Портал пишет время своим местным и пояса не указывает, поэтому «10:00» в Магадане и в
Москве — разные моменты. В БД лежит UTC, а обратно показывается местное время суда.

Отдельная и куда более дорогая тема — uid. Он считается uuid5 от строки с датой, и любое
изменение этой строки переписало бы ключи всех уже сохранённых событий и заседаний: они
пересоздались бы, а в outbox_event уехала бы волна ложных «удалено/создано». Поэтому
ожидаемые uuid здесь прибиты литералами: тест обязан упасть, если формат ключа поедет.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.repositories.court_sessions import court_session_uid
from app.repositories.events import event_uid
from app.timezones import TZ_BY_REGION, timezone_for, to_court_local, to_utc

CARD_KEY = "77MS0002-01-2026-000002-22|77MS0002|02-0002/2/2026"


# ------------------------------------------------------------------ пояс по справочнику
def test_timezone_is_found_by_region() -> None:
    """Обычный случай: пояс берётся из региона суда."""
    assert timezone_for("Город Москва", "77MS0002") == "Europe/Moscow"
    assert timezone_for("Сахалинская область", "65MS0001") == "Asia/Sakhalin"


def test_court_override_wins_over_the_region() -> None:
    """Суд из «разъезжающегося» региона берёт свой пояс, а не региональный.

    Якутия и Сахалин не укладываются в один пояс: у Якутии их три. Проверяем сам механизм
    исключения, не завися от того, какие коды уже занесены в TZ_BY_COURT_CODE.
    """
    from app import timezones

    code = "14MS0099"
    assert timezone_for("Республика Саха (Якутия)", code) == "Asia/Yakutsk"

    timezones.TZ_BY_COURT_CODE[code] = "Asia/Srednekolymsk"
    try:
        assert timezone_for("Республика Саха (Якутия)", code) == "Asia/Srednekolymsk"
    finally:
        del timezones.TZ_BY_COURT_CODE[code]


def test_unknown_region_raises() -> None:
    """Незнакомый регион — ошибка, а не молчаливая подстановка Москвы.

    Подстановка по умолчанию дала бы неверное время без единого признака неисправности:
    у дальневосточного суда оно уехало бы на девять часов, и заметили бы это не скоро.
    """
    with pytest.raises(KeyError):
        timezone_for("Республика Нарния", "99MS0001")


def test_every_region_of_the_reference_book_is_covered() -> None:
    """В карте есть все регионы справочника — иначе часть судов не завести вовсе."""
    import json

    from app.config import COURTS_JSON_PATH

    courts = json.loads(COURTS_JSON_PATH.read_text(encoding="utf-8"))
    missing = sorted({c["region"] for c in courts if c["region"] not in TZ_BY_REGION})
    assert missing == []


# ------------------------------------------------------------------------ конвертация
def test_local_to_utc_and_back() -> None:
    """Местное время суда превращается в момент и возвращается тем же."""
    local = datetime(2026, 9, 14, 10, 0)

    moment = to_utc(local, "Asia/Vladivostok")

    # Владивосток — UTC+10, значит 10:00 у них это 00:00 того же дня по UTC.
    assert moment == datetime(2026, 9, 14, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert to_court_local(moment, "Asia/Vladivostok").replace(tzinfo=None) == local


def test_same_wall_clock_in_two_courts_is_a_different_moment() -> None:
    """«10:00» в Москве и во Владивостоке — разные моменты, и это ровно то, ради чего всё."""
    local = datetime(2026, 9, 14, 10, 0)

    assert to_utc(local, "Europe/Moscow") != to_utc(local, "Asia/Vladivostok")


def test_local_midnight_keeps_its_date_in_the_court_timezone() -> None:
    """У события без времени полночь остаётся своей датой при взгляде из пояса суда.

    В UTC такая полночь уезжает на соседние сутки (в Барнауле на 17:00 предыдущего дня),
    поэтому дату события нельзя читать в UTC — только в поясе суда.
    """
    midnight = datetime(2026, 7, 17, 0, 0)

    moment = to_utc(midnight, "Asia/Barnaul")

    assert moment.date() != midnight.date()  # в UTC это ещё вчера
    assert to_court_local(moment, "Asia/Barnaul").date() == midnight.date()


# ------------------------------------------------------------------- стабильность uid
def test_event_uid_ignores_time() -> None:
    """Время не входит в identity события: появление времени не создаёт новое событие.

    У порталов время появляется не сразу (колонка «Время события» заполняется позже), и
    если бы оно попадало в ключ, каждое такое дописывание выглядело бы как «событие
    исчезло, появилось другое».
    """
    midnight = event_uid(CARD_KEY, datetime(2026, 6, 8, 0, 0), "Завершено")
    with_time = event_uid(CARD_KEY, datetime(2026, 6, 8, 14, 30), "Завершено")

    assert midnight == with_time


def test_event_uid_is_pinned() -> None:
    """Ключ события не менялся при переходе на timestamptz — литерал тому порукой."""
    assert str(event_uid(CARD_KEY, datetime(2026, 6, 8, 0, 0), "Завершено")) == (
        "2a7e4e76-69ac-5083-ad8c-5f463ae7e553"
    )


def test_event_uid_still_separates_dates() -> None:
    """А вот РАЗНЫЕ дни — это разные события, иначе они схлопнулись бы в одно."""
    first = event_uid(CARD_KEY, datetime(2026, 6, 8, 0, 0), "Завершено")
    second = event_uid(CARD_KEY, datetime(2026, 6, 9, 0, 0), "Завершено")

    assert first != second


def test_court_session_uid_is_pinned() -> None:
    """Ключ заседания считается от МЕСТНЫХ настенных часов и тоже не сдвинулся.

    Считай мы его от хранимого момента в UTC, одно и то же «16:50» в разных судах давало
    бы разные ключи, а перевод базы на timestamptz переписал бы все существующие.
    """
    assert str(
        court_session_uid(CARD_KEY, datetime(2026, 7, 30, 16, 50), "Беседа")
    ) == "98b7cee7-7cc7-5b90-8f3e-5bca42650061"


def test_court_session_uid_keeps_time() -> None:
    """У заседаний время в identity ОСТАЁТСЯ: два заседания в один день — разные строки."""
    morning = court_session_uid(CARD_KEY, datetime(2026, 7, 30, 10, 0), "Беседа")
    evening = court_session_uid(CARD_KEY, datetime(2026, 7, 30, 16, 50), "Беседа")

    assert morning != evening
