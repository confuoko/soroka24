"""Два входа в систему: УИД и ссылка на карточку дела.

Зачем два. У порталов вроде mos-sud.ru есть поиск по УИД, а у msudrf.ru (6063 суда из
72 регионов) его нет — там дело открывается только прямой ссылкой, и УИД становится
известен лишь после похода на страницу. Проверяем, что вход выбирается правильно и что
задача заводится тем ключом, который на этот момент действительно есть.
"""
import pytest

from app.courts import (
    MoscowMirCourtClient,
    MsudrfCourtClient,
    UnsupportedCourt,
    define_court_by_uid,
    define_court_by_url,
    find_uid,
    is_supported_url,
)
from app.models.database import SearchStatus, SearchTask
from app.repositories import SearchTaskRepository
from app.validators import looks_like_url, validate_url

CASE_URL = (
    "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs"
    "&case_id=429386415&delo_id=1540005"
)
UID = "50MS0095-01-2026-002990-16"
MOSCOW_UID = "77MS0466-01-2026-003751-93"


# ------------------------------------------------------ что прислали: УИД или ссылка
@pytest.mark.parametrize(
    "value, is_url",
    [
        (CASE_URL, True),
        ("http://1.bkr.msudrf.ru/modules.php?name=sud_delo", True),
        (MOSCOW_UID, False),
        (UID, False),
        ("  " + CASE_URL + "  ", True),
    ],
)
def test_url_is_told_apart_from_uid(value, is_url) -> None:
    """Различаем по схеме адреса: у УИД её нет и быть не может."""
    assert looks_like_url(value) is is_url


def test_garbage_is_not_a_valid_url() -> None:
    """«Похоже на ссылку» и «годная ссылка» — разные проверки."""
    assert looks_like_url("https://") is True
    assert validate_url("https://") is False


# ------------------------------------------------------------------- резолвер судов
def test_moscow_region_url_resolves_to_msudrf_client() -> None:
    """Любой участок Московской области обслуживает один клиент — движок у них общий."""
    for url in (CASE_URL, "http://148.mo.msudrf.ru/x", "https://235.mo.msudrf.ru/y"):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)


def test_altai_krai_url_resolves_to_msudrf_client() -> None:
    """Алтайский край — тот же движок и тот же клиент, только другой домен.

    Поддомены там именные, а не по номеру участка (centr1, biysk1), поэтому проверяем
    именно домен: разбирать имя участка из хоста нечем и не нужно.
    """
    for url in (
        "https://centr1.alt.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=1",
        "http://biysk1.alt.msudrf.ru/x",
    ):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)


def test_altai_republic_and_krai_are_separate_domains() -> None:
    """Республика Алтай (*.ralt) и Алтайский край (*.alt) — РАЗНЫЕ регионы движка.

    Подключены оба, поэтому перепутать их теперь не «обидно», а опасно: у края 143 суда с
    кодом 22MS, у республики 14 с кодом 02MS, и дело привязалось бы к чужому суду. Держится
    различие на одной точке в define_court_by_url: без неё endswith("alt.msudrf.ru")
    накрывает и республику.

    Границу проверяем на ВЫДУМАННОМ домене: реальных вторых уровней, кончающихся на
    "alt.msudrf.ru", на движке ровно два — alt и ralt, оба подключены, и отрицательного
    примера из живых регионов взять негде. Если бы сравнение шло без точки, *.qalt тоже
    прошёл бы за Алтайский край.
    """
    for url in (
        "https://galtms1.ralt.msudrf.ru/case",
        "http://ulagan.ralt.msudrf.ru/x",
    ):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)

    assert is_supported_url("https://1.qalt.msudrf.ru/case") is False

    with pytest.raises(UnsupportedCourt):
        define_court_by_url("https://1.qalt.msudrf.ru/case")


def test_amur_oblast_url_resolves_to_msudrf_client() -> None:
    """Амурская область — третий подключённый регион движка, поддомены тоже именные."""
    for url in (
        "https://arhr.amr.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=1",
        "http://bel1.amr.msudrf.ru/x",
    ):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)


def test_arkhangelsk_url_resolves_to_msudrf_client() -> None:
    """Архангельская область — и вместе с ней Ненецкий АО: портал у них общий.

    Округ входит в область административно, поэтому его три суда сидят на том же домене
    с теми же кодами 29MS. Проверяем оба случая: отдельной строки в COURT_BY_DOMAIN у
    округа нет и быть не должно.
    """
    for url in (
        "https://1vel.arh.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=1",
        "http://1nao.arh.msudrf.ru/x",  # Ненецкий АО, 29MS0070
    ):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)


def test_astrakhan_url_resolves_to_msudrf_client() -> None:
    """Астраханская область — пятый домен движка."""
    for url in (
        "https://kir1.ast.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=1",
        "http://chrn1.ast.msudrf.ru/x",
    ):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)


def test_belgorod_url_resolves_to_msudrf_client() -> None:
    """Белгородская область — шестой домен движка."""
    for url in (
        "https://alex1.blg.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=1",
        "http://belgr2.blg.msudrf.ru/x",
    ):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)


def test_volgograd_and_vologda_urls_resolve_to_msudrf_client() -> None:
    """Волгоградская и Вологодская области — соседние домены, различаются одной буквой.

    Проверяем обе разом: vol/vld легко перепутать при добавлении, а рядом живёт ещё и
    Владимирская область на wld.msudrf.ru, которую мы не подключали.
    """
    for url in ("https://1.vol.msudrf.ru/x", "https://146.vol.msudrf.ru/x"):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)
    for url in ("https://1.vld.msudrf.ru/x", "https://68.vld.msudrf.ru/x"):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)

    assert is_supported_url("https://1.wld.msudrf.ru/x") is False


def test_voronezh_url_resolves_to_msudrf_client() -> None:
    """Воронежская область — девятый домен движка, поддомены именные."""
    for url in ("https://zhelezn1.vrn.msudrf.ru/x", "http://zhelezn4.vrn.msudrf.ru/y"):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)


def test_other_regions_on_the_same_engine_are_not_served_yet() -> None:
    """Тот же движок в чужом регионе пока не обслуживаем.

    Движок общий для 72 регионов, но разметку мы смотрели только на части из них (список —
    в COURT_BY_DOMAIN), поэтому обещать остальные 2618 судов, ни разу их не открыв, нельзя.
    Подключается регион одной строкой в COURT_BY_DOMAIN — когда его разметку проверят.
    """
    # Ростовская область — самый большой из неподключённых регионов движка (230 судов),
    # Адыгея — один из самых маленьких (24).
    for url in ("http://1.ros.msudrf.ru/x", "https://maikop1.adg.msudrf.ru/y"):
        assert is_supported_url(url) is False
        with pytest.raises(UnsupportedCourt):
            define_court_by_url(url)


def test_domain_boundary_is_respected_for_the_region() -> None:
    """«evil-mo.msudrf.ru» — не Московская область: сравниваем по границе имени.

    Без проверки границы дефис перед «mo» проскочил бы, и браузер (через прокси и с
    игнором сертификата) пошёл бы на чужой хост.
    """
    assert is_supported_url("https://evil-mo.msudrf.ru/case") is False


def test_lookalike_domain_is_rejected() -> None:
    """msudrf.ru.evil.com — чужой хост. Сравниваем по границе имени, а не подстрокой.

    Иначе по такой ссылке браузер (да ещё через прокси и с игнором сертификата)
    пошёл бы куда угодно.
    """
    assert is_supported_url("https://msudrf.ru.evil.com/case") is False

    with pytest.raises(UnsupportedCourt):
        define_court_by_url("https://msudrf.ru.evil.com/case")


def test_unknown_portal_is_rejected() -> None:
    """Портал, который мы не умеем открывать, отсекаем до создания задачи."""
    assert is_supported_url("https://mirsud24.ru/case/1") is False


def test_spb_portal_is_supported_though_parsing_is_not_written() -> None:
    """Петербург подключён НАМЕРЕННО, хотя разбор его страниц ещё не написан.

    Так дело можно завести через API, дойти до портала и получить снимок страницы в S3
    (снимок снимается до разбора) — ради накопления образцов. Падает такая задача уже
    на разборе. Для Брянска решение обратное: тип C доменов не получил, чтобы не тратить
    прокси и капчу впустую, — а на mirsud.spb.ru капчи нет и поход бесплатный.
    """
    from app.courts.spb_mir_court import SpbMirCourtClient

    url = "https://mirsud.spb.ru/cases/detail/98/?id=2-2983%2F2026-98"

    assert is_supported_url(url) is True
    assert isinstance(define_court_by_url(url), SpbMirCourtClient)


def test_uid_resolves_only_for_portals_with_search() -> None:
    """По УИД ищем только там, где у портала есть поиск по нему.

    Для 50MS резолвер по УИД отказывает намеренно: на msudrf.ru поиска нет, такое дело
    можно завести только ссылкой.
    """
    assert isinstance(define_court_by_uid(MOSCOW_UID), MoscowMirCourtClient)

    with pytest.raises(UnsupportedCourt):
        define_court_by_uid(UID)


# ------------------------------------------------------------- поиск УИД на странице
def test_uid_is_found_in_page_text() -> None:
    """Формат УИД общероссийский, поэтому поиск один на все порталы."""
    assert find_uid(f"<td>Уникальный идентификатор дела</td><td>{UID}</td>") == UID
    assert find_uid("<html>ничего похожего</html>") is None


# ------------------------------------------------------------------ задача по ссылке
def test_task_can_be_created_without_uid(session) -> None:
    """Задачу по ссылке заводим без УИД: на этот момент его ещё неоткуда взять."""
    task = SearchTaskRepository(session).create(source_url=CASE_URL)

    assert task.uid is None
    assert task.source_url == CASE_URL
    assert task.status is SearchStatus.PENDING


def test_task_needs_at_least_one_key(session) -> None:
    """Задача без УИД и без ссылки бессмысленна — по ней нечего открывать."""
    with pytest.raises(ValueError):
        SearchTaskRepository(session).create()


def test_uid_is_written_back_after_fetch(session) -> None:
    """Найденный на странице УИД дописывается в задачу — он нужен для привязки суда."""
    repo = SearchTaskRepository(session)
    task = repo.create(source_url=CASE_URL)

    repo.set_uid(task, UID)
    session.flush()

    assert session.get(SearchTask, task.id).uid == UID


def test_active_task_is_found_by_url(session) -> None:
    """Дедупликация по ссылке: второй такой же запрос не должен плодить задачи."""
    repo = SearchTaskRepository(session)
    task = repo.create(source_url=CASE_URL)

    assert repo.get_active_by_url(CASE_URL).id == task.id
    assert repo.get_active_by_url("https://95.mo.msudrf.ru/other") is None


def test_finished_task_does_not_block_new_one(session) -> None:
    """Завершённая задача дедупликацию не держит — дело можно перепарсить."""
    repo = SearchTaskRepository(session)
    task = repo.create(source_url=CASE_URL)
    repo.mark_success(task, case_id=1)
    session.flush()

    assert repo.get_active_by_url(CASE_URL) is None


# ------------------------------------------- подсказка, когда по УИД искать нельзя
def _stub_courts(monkeypatch, court):
    """Подменить справочник судов: тест не должен зависеть от содержимого таблицы."""
    from types import SimpleNamespace

    from app.api import routes

    monkeypatch.setattr(
        routes,
        "CourtRepository",
        lambda session: SimpleNamespace(get_by_code=lambda code: court),
    )
    return routes


def test_known_court_asks_for_a_link(monkeypatch) -> None:
    """Суд определился, но по УИД не ищется → говорим какой это суд и что прислать.

    Без этого ответ выглядел бы как «суд не поддержан», хотя дело прекрасно
    достаётся ссылкой — пользователь просто не знает, что нужна именно она.
    """
    from types import SimpleNamespace

    routes = _stub_courts(monkeypatch, SimpleNamespace(name="Судебный участок № 95"))

    answer = routes._explain_uid_not_searchable(UID)

    assert answer.status == "link_required"
    assert "Судебный участок № 95" in answer.message
    assert "msudrf.ru/modules.php" in answer.message


def test_unknown_court_says_so(monkeypatch) -> None:
    """Кода нет в справочнике — тут ссылка не поможет, и предлагать её незачем."""
    routes = _stub_courts(monkeypatch, None)

    answer = routes._explain_uid_not_searchable("99XX9999-01-2026-000001-11")

    assert answer.status == "unsupported_court"
    assert "99XX9999" in answer.message


# --------------------------------------------- по одному УИД карточек может быть много
def _stub_uid_branch(monkeypatch, cards, active_task=None):
    """Подменить БД для ветки по УИД. Возвращает (routes, список созданных задач)."""
    from types import SimpleNamespace

    from app.api import routes

    created = []
    monkeypatch.setattr(
        routes,
        "CaseRepository",
        lambda session: SimpleNamespace(list_by_uid=lambda uid: cards),
    )
    monkeypatch.setattr(
        routes,
        "SearchTaskRepository",
        lambda session: SimpleNamespace(
            get_active_by_uid=lambda uid: active_task,
            create=lambda **kw: created.append(kw) or SimpleNamespace(id=777),
        ),
    )
    # В очередь в тестах не ставим: Celery здесь не нужен.
    monkeypatch.setattr(routes.sync_case, "apply_async", lambda *a, **kw: None)
    return routes, created


def test_search_is_started_even_when_cases_are_already_found(monkeypatch) -> None:
    """Дело уже в БД → всё равно ставим поиск, а найденное отдаём вместе с task_id.

    УИД сквозной, поэтому найденные карточки могли прийти ссылками со страниц других
    инстанций — московской среди них может не быть вовсе. А если есть, рядом могло
    появиться ещё одно производство: портал показывает их одной таблицей.
    """
    from datetime import datetime
    from types import SimpleNamespace

    from fastapi import Response

    cards = [
        SimpleNamespace(id=11, updated_at=datetime(2026, 8, 1)),
        SimpleNamespace(id=12, updated_at=datetime(2026, 8, 5)),
    ]
    routes, created = _stub_uid_branch(monkeypatch, cards)

    answer = routes._sync_by_uid(MOSCOW_UID, force=False, response=Response())

    assert answer.status == "processing"
    assert answer.task_id == 777
    assert created == [{"uid": MOSCOW_UID}]  # поиск заведён, несмотря на находки
    # Отдаём ВСЕ найденные карточки; case_id — самая свежая, ради совместимости.
    assert answer.case_ids == [11, 12]
    assert answer.case_id == 12


def test_nothing_found_still_returns_a_task(monkeypatch) -> None:
    """Дела в БД нет → обычный ответ с задачей и пустыми ссылками на карточки."""
    from fastapi import Response

    routes, created = _stub_uid_branch(monkeypatch, cards=[])

    answer = routes._sync_by_uid(MOSCOW_UID, force=False, response=Response())

    assert answer.status == "processing"
    assert answer.case_ids is None
    assert answer.case_id is None
    assert created == [{"uid": MOSCOW_UID}]


def test_active_task_is_not_duplicated_but_still_reports_cases(monkeypatch) -> None:
    """По этому УИД уже идёт задача → отдаём её, второй не заводим, карточки показываем."""
    from datetime import datetime
    from types import SimpleNamespace

    from fastapi import Response

    cards = [SimpleNamespace(id=11, updated_at=datetime(2026, 8, 1))]
    routes, created = _stub_uid_branch(
        monkeypatch, cards, active_task=SimpleNamespace(id=555)
    )

    answer = routes._sync_by_uid(MOSCOW_UID, force=False, response=Response())

    assert answer.task_id == 555
    assert created == []
    assert answer.case_ids == [11]


def _no_tasks(monkeypatch, routes) -> None:
    """Задача в этом тесте заводиться не должна — поймаем, если всё же заведётся."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        routes,
        "SearchTaskRepository",
        lambda session: SimpleNamespace(
            create=lambda **kw: pytest.fail("задачу заводить не должны")
        ),
    )


def test_unknown_court_is_rejected_before_checking_the_portal(monkeypatch) -> None:
    """Суда с таким сайтом нет в справочнике → отказ сразу, задачу не заводим.

    Справочник проверяется ПЕРВЫМ: если суда нет, неважно, умеем ли мы работать с его
    порталом — карточку всё равно не к чему привязать. Поэтому даже для поддержанного
    msudrf.ru ответ здесь про справочник, а не про портал.
    """
    from types import SimpleNamespace

    from fastapi import Response

    from app.api import routes

    monkeypatch.setattr(
        routes,
        "CourtRepository",
        lambda session: SimpleNamespace(get_by_url=lambda url: None),
    )
    monkeypatch.setattr(
        routes, "is_supported_url", lambda url: pytest.fail("портал проверять рано")
    )
    _no_tasks(monkeypatch, routes)

    answer = routes._sync_by_url(CASE_URL, force=False, response=Response())

    assert answer.status == "unsupported_court"
    assert "нет в справочнике" in answer.message
    assert "95.mo.msudrf.ru" in answer.message


def test_known_court_on_unsupported_portal_is_named(monkeypatch) -> None:
    """Суд нашёлся, а клиента к его порталу нет → называем суд по имени.

    В справочнике 85 регионов со своими порталами, а клиент пока только к движку
    msudrf.ru. Без имени суда ответ выглядел бы как «мы вас не знаем», хотя суд-то
    известен — не поддержан именно его сайт.
    """
    from types import SimpleNamespace

    from fastapi import Response

    from app.api import routes

    monkeypatch.setattr(
        routes,
        "CourtRepository",
        lambda session: SimpleNamespace(
            get_by_url=lambda url: SimpleNamespace(name="Судебный участок № 154")
        ),
    )
    monkeypatch.setattr(routes, "is_supported_url", lambda url: False)
    _no_tasks(monkeypatch, routes)

    # Портал взят заведомо неподдержанный: mirsud.spb.ru здесь уже не годится — его
    # подключили ради сбора образцов страниц.
    answer = routes._sync_by_url(
        "https://mirsud24.ru/case/1", force=False, response=Response()
    )

    assert answer.status == "unsupported_court"
    assert "Судебный участок № 154" in answer.message
    assert "mirsud24.ru" in answer.message


def test_example_link_is_a_real_case_url() -> None:
    """Пример должен быть настоящим адресом карточки, а не выдумкой.

    Его показывают пользователю как образец, поэтому он обязан разбираться нашим же
    резолвером — иначе мы советуем прислать то, что сами не примем.
    """
    from app.courts.msudrf_court import CASE_URL_EXAMPLE

    assert validate_url(CASE_URL_EXAMPLE)
    assert is_supported_url(CASE_URL_EXAMPLE)
    assert looks_like_url(CASE_URL_EXAMPLE)
