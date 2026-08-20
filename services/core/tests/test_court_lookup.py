"""Определение суда карточки: по номеру участка (поиск по УИД) и по хосту (ссылка).

Раньше суд выводился из УИД — `get_by_code(uid[:8])`. Это неверно: у 36 московских судов
номер участка не совпадает с числом в коде суда, и «собрать» код арифметикой нельзя —
получится ЧУЖОЙ существующий суд. Теперь суд берётся из того же источника, что и само
дело: из строки таблицы результатов либо из хоста ссылки.

Номер участка и хост нигде не хранятся отдельными колонками: и то, и другое выводится из
name/base_url прямо в момент поиска. Справочник в тестах не используем — заводим свои суды
с несуществующим префиксом ZZMS, чтобы не зависеть от того, залит ли courts.json, и не
задеть настоящие записи.
"""
from datetime import datetime
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app.courts.moscow_mir_court import DETAIL_LINK, participok_from_href
from app.models.database import Court, CourtLevel
from app.repositories.courts import CourtRepository, host_of, participok_no
from app.storage.html_snapshots import card_folder, snapshot_key

HTML_EXAMPLES = Path(__file__).resolve().parents[1] / "html_examples"


@pytest.fixture
def courts(session) -> dict[str, Court]:
    """Пара судов, воспроизводящая московскую ловушку «номер участка ≠ число в коде».

    В настоящем справочнике это 77MS0466 (участок № 463) и 77MS0463 (участок № 460):
    наивное «77MS» + номер участка дало бы для участка 463 совсем другой суд.
    """
    rows = {
        "ZZMS0466": ("Судебный участок № 463 поселения Троицк", "https://463.zz.test"),
        "ZZMS0463": ("Судебный участок № 460 Дмитровского района", "https://460.zz.test"),
    }
    created = {}
    for code, (name, base_url) in rows.items():
        row = Court(
            code=code,
            name=name,
            level=CourtLevel.MIRSUD,
            region="Тестовый регион",
            base_url=base_url,
        )
        session.add(row)
        created[code] = row
    session.flush()
    return created


# ------------------------------------------------------- вывод полей из справочника
@pytest.mark.parametrize(
    "name, expected",
    [
        ("Судебный участок № 235 Зарайского судебного района", 235),
        ("Судебный участок №1 района Матушкино", 1),
        ("Городской суд без участка", None),
    ],
)
def test_participok_number_is_taken_from_the_name(name, expected) -> None:
    """Номер участка живёт в названии суда — оттуда его и берём при заливке справочника."""
    assert participok_no(name) == expected


def test_host_is_lowercased_and_stripped_of_scheme() -> None:
    """Хост сравнивается со ссылкой пользователя, поэтому нормализуем регистр."""
    assert host_of("http://95.MO.msudrf.ru/modules.php?x=1") == "95.mo.msudrf.ru"
    assert host_of(None) is None


# ------------------------------------------------------------- поиск суда по участку
def test_court_is_found_by_participok_not_by_code_arithmetic(session, courts) -> None:
    """Участок № 463 → суд с кодом ...0466, а не «...0463».

    Это главная регрессия: код с тем же числом принадлежит другому участку, и наивная
    арифметика молча привязала бы дело к чужому суду.
    """
    repo = CourtRepository(session)

    found = repo.get_by_participok("ZZMS", 463)

    assert found.code == "ZZMS0466"
    assert found is not courts["ZZMS0463"]


def test_participok_search_is_limited_to_the_region(session, courts) -> None:
    """Один и тот же номер участка в двух регионах — два разных суда.

    По справочнику в целом номер участка не уникален: участок № 1 есть в каждом судебном
    районе десятков регионов. Поэтому поиск без региона выбирал бы случайный суд.
    """
    other_region = Court(
        code="ZZNS0001",
        name="Судебный участок № 463 другого региона",
        level=CourtLevel.MIRSUD,
        region="Другой тестовый регион",
    )
    session.add(other_region)
    session.flush()

    repo = CourtRepository(session)

    assert repo.get_by_participok("ZZMS", 463).code == "ZZMS0466"
    assert repo.get_by_participok("ZZNS", 463).code == "ZZNS0001"


def test_real_reference_resolves_the_moscow_trap(session) -> None:
    """На настоящем справочнике участок № 463 → 77MS0466, а не 77MS0463.

    Тот самый случай из html_examples/case_details_page.html: УИД
    77MS0466-01-2026-003751-93 принадлежит участку № 463, а код 77MS0463 — это участок
    № 460 Дмитровского района, совсем другой суд.
    """
    repo = CourtRepository(session)
    if repo.get_by_code("77MS0466") is None:
        pytest.skip("справочник судов не залит (data/courts.json)")

    assert repo.get_by_participok("77MS", 463).code == "77MS0466"
    # Наивное «77MS» + номер участка привело бы вот сюда — в чужой суд.
    assert participok_no(repo.get_by_code("77MS0463").name) == 460


def test_unknown_participok_gives_nothing(session, courts) -> None:
    """Участка нет в справочнике → None; дальше задача такое дело не парсит."""
    assert CourtRepository(session).get_by_participok("ZZMS", 999) is None


def test_ambiguous_participok_picks_nobody(session, courts) -> None:
    """Два суда на один номер участка → не выбираем никакой.

    Взять первый попавшийся значит привязать дело к чужому суду; лучше честно не знать.
    """
    twin = Court(
        code="ZZMS0999",
        name="Судебный участок № 463 другого района",
        level=CourtLevel.MIRSUD,
        region="Тестовый регион",
    )
    session.add(twin)
    session.flush()

    assert CourtRepository(session).get_by_participok("ZZMS", 463) is None


# ---------------------------------------------------------------- поиск суда по хосту
def test_court_is_found_by_host(session, courts) -> None:
    """Дело пришло ссылкой → суд определяется по её хосту, регистр не важен."""
    repo = CourtRepository(session)

    assert repo.get_by_host("463.zz.test").code == "ZZMS0466"
    assert repo.get_by_host("463.ZZ.TEST").code == "ZZMS0466"
    assert repo.get_by_host("нет-такого.zz.test") is None
    assert repo.get_by_host("") is None


def test_host_is_found_in_any_spelling_of_the_participok_label(session, courts) -> None:
    """Метку участка отделяют от домена региона точкой, дефисом или ничем — суд один и тот же.

    В справочнике так и есть: 69MS0045 Тверской области записан склеенно
    (26twr.msudrf.ru), а ссылки на него присылают в точечной форме — портал отвечает по
    обоим именам. Пока сверка была строго точной, такой суд не находился ни по одной
    ссылке, и справочник приходилось бы править под каждый подобный случай.
    """
    glued = Court(
        code="ZZMS0026",
        name="Судебный участок № 26 Тестовой области",
        level=CourtLevel.MIRSUD,
        region="Тестовый регион",
        base_url="http://26zz.test.ru",
    )
    session.add(glued)
    session.flush()
    repo = CourtRepository(session)

    # Справочник знает склеенное написание, а ссылка приходит в любом из трёх.
    assert repo.get_by_host("26zz.test.ru").code == "ZZMS0026"
    assert repo.get_by_host("26.zz.test.ru").code == "ZZMS0026"
    assert repo.get_by_host("26-zz.test.ru").code == "ZZMS0026"
    # Чужой участок того же региона от этого не находится.
    assert repo.get_by_host("27.zz.test.ru") is None


def test_shared_host_picks_nobody(session, courts) -> None:
    """Общий портал (один хост на сотни судов) суд не определяет.

    Так устроен mos-sud.ru: 471 московский суд на одном хосте. Молча взять первый нельзя.
    """
    shared = Court(
        code="ZZMS0500",
        name="Судебный участок № 500",
        level=CourtLevel.MIRSUD,
        region="Тестовый регион",
        base_url="https://463.zz.test",
    )
    session.add(shared)
    session.flush()

    assert CourtRepository(session).get_by_host("463.zz.test") is None


# --------------------------------------------- поиск суда по ссылке (хост или участок)
def test_get_by_url_falls_back_to_host(session, courts) -> None:
    """Обычный портал: у участка свой поддомен, ничего нового не происходит."""
    repo = CourtRepository(session)

    assert repo.get_by_url("https://463.zz.test/modules.php?case_id=1").code == "ZZMS0466"
    assert repo.get_by_url("https://нет-такого.zz.test/case") is None


def test_get_by_url_uses_participok_on_a_shared_host(session) -> None:
    """Петербург: 211 судов на одном хосте, суд берётся из номера участка в пути.

    Проверяем на настоящем справочнике и именно на расходящейся паре: участок № 126 —
    это код 78MS0124, а 78MS0126 — участок № 128, другой суд. Арифметика по числу из
    ссылки молча привязала бы дело не туда.
    """
    repo = CourtRepository(session)
    if repo.get_by_code("78MS0124") is None:
        pytest.skip("справочник судов не залит (data/courts.json)")

    # По хосту суд не определяется — и это не ошибка, а причина существования запасного пути.
    assert repo.get_by_host("mirsud.spb.ru") is None

    found = repo.get_by_url("https://mirsud.spb.ru/cases/detail/126/?id=2-1557%2F2026-126")
    assert found.code == "78MS0124"
    assert participok_no(repo.get_by_code("78MS0126").name) == 128


def test_get_by_url_needs_a_case_path_on_a_shared_host(session) -> None:
    """На общем хосте без номера участка в пути суд по-прежнему не определяется."""
    repo = CourtRepository(session)
    if repo.get_by_code("78MS0124") is None:
        pytest.skip("справочник судов не залит (data/courts.json)")

    assert repo.get_by_url("https://mirsud.spb.ru/court-sites/126") is None
    # Участка с таким номером в Петербурге нет — придумывать суд нельзя.
    assert repo.get_by_url("https://mirsud.spb.ru/cases/detail/9999/?id=2-1%2F2026-9999") is None


# ------------------------------------------------- таблица результатов поиска Москвы
def test_results_table_has_one_row_per_case() -> None:
    """На реальной выдаче селектор находит РОВНО одну строку, а не две.

    Рядом с видимой таблицей на странице лежит её скрытая копия
    (<div id="modalTable" style="display:none">). Пока селектор не был ограничен
    .wrapper-search-tables, каждое дело находилось дважды — и качалось бы дважды.
    """
    html = (HTML_EXAMPLES / "after_search_page.html").read_text(encoding="utf-8")

    links = BeautifulSoup(html, "lxml").select(DETAIL_LINK)

    assert len(links) == 1
    assert links[0].get_text(strip=True) == "05-0444/1/2026"
    assert participok_from_href(links[0]["href"]) == 1


@pytest.mark.parametrize(
    "href, expected",
    [
        ("/1/cases/admin/details/13e9a520?uid=x&formType=fullForm", 1),
        ("/463/cases/claim-civil/details/abc", 463),
        ("https://mos-sud.ru/463/cases/claim-civil/details/abc", 463),
        ("/cases/admin/details/abc", None),  # номера участка в пути нет
        ("", None),
    ],
)
def test_participok_is_taken_from_the_link_path(href, expected) -> None:
    """Номер участка берём из первого сегмента пути, а не из текста ссылки.

    Текст («Дела об административных правонарушениях, судебный участок № 1») зависит от
    формулировки портала, а путь — нет.
    """
    assert participok_from_href(href) == expected


# --------------------------------------------------------------- ключ снапшота в S3
def test_card_folder_flattens_slashes_in_the_case_number() -> None:
    """Слэши в номере дела заменяются дефисами: в ключе объекта слэш — разделитель папок."""
    assert card_folder("77MS0002", "05-0444/1/2026") == "77MS0002-05-0444-1-2026"


def test_snapshot_key_separates_cards_of_one_uid() -> None:
    """У двух карточек одного УИД — разные папки, иначе разметка смешается в одну."""
    when = datetime(2026, 8, 4, 15, 0, 0)
    uid = "77MS0002-01-2026-000004-44"

    first = snapshot_key(uid, when, card=card_folder("77MS0002", "05-0444/2/2026"))
    second = snapshot_key(uid, when, card=card_folder("77MS0002", "02-0848/2/2026"))

    assert first != second
    assert first.startswith(f"html_snapshots/{uid}/77MS0002-05-0444-2-2026/")


# ------------------------------------------------------------- сборка справочника
def _build_courts_module():
    """Загрузить scripts/build_courts_json.py как модуль (в пакете он не лежит)."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "build_courts_json.py"
    spec = importlib.util.spec_from_file_location("build_courts_json", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reference_build_rejects_shared_msudrf_host() -> None:
    """Два суда msudrf.ru на одном поддомене → сборка справочника не проходит.

    Ровно так и было у 50MS0022 с 50MS0122: у второго в исходной странице потерялась
    цифра поддомена, и дела Люберецкого суда привязались бы к Воскресенскому.
    """
    module = _build_courts_module()

    problems = module.check_hosts_unique(
        [
            {"code": "50MS0022", "name": "Судебный участок № 22", "base_url": "http://22.mo.msudrf.ru"},
            {"code": "50MS0122", "name": "Судебный участок № 122", "base_url": "http://22.mo.msudrf.ru"},
        ]
    )

    assert len(problems) == 1
    assert "50MS0022" in problems[0] and "50MS0122" in problems[0]


def test_reference_build_repairs_broken_mo_host() -> None:
    """Побитый адрес суда МО восстанавливается из номера участка в названии."""
    module = _build_courts_module()

    assert (
        module._normalize_base_url(
            "50MS0122", "Судебный участок № 122 мирового судьи Люберецкого",
            "http://22.mo.msudrf.ru",
        )
        == "https://122.mo.msudrf.ru"
    )
    # Целый адрес не трогаем — иначе в диффе справочника оказались бы сотни записей.
    assert (
        module._normalize_base_url(
            "50MS0095", "Судебный участок № 95 мирового судьи Красногорского",
            "http://95.mo.msudrf.ru",
        )
        == "http://95.mo.msudrf.ru"
    )
    # Чужие регионы живут по своим правилам, их адреса не переписываем.
    assert (
        module._normalize_base_url(
            "78MS0151", "Судебный участок № 154", "https://mirsud.spb.ru/court-sites/154"
        )
        == "https://mirsud.spb.ru/court-sites/154"
    )


def test_shipped_reference_has_no_host_collisions() -> None:
    """Сам data/courts.json не должен содержать коллизий: по хосту определяется суд."""
    import json

    module = _build_courts_module()
    courts = json.loads(
        (Path(__file__).resolve().parents[1] / "data" / "courts.json").read_text(
            encoding="utf-8"
        )
    )

    assert module.check_hosts_unique(courts) == []
