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
def test_url_resolves_to_msudrf_client() -> None:
    """Любой поддомен msudrf.ru обслуживает один клиент — движок у них общий."""
    for url in (CASE_URL, "http://1.bkr.msudrf.ru/x", "https://maikop1.adg.msudrf.ru/y"):
        assert isinstance(define_court_by_url(url), MsudrfCourtClient)


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


def test_example_link_is_a_real_case_url() -> None:
    """Пример должен быть настоящим адресом карточки, а не выдумкой.

    Его показывают пользователю как образец, поэтому он обязан разбираться нашим же
    резолвером — иначе мы советуем прислать то, что сами не примем.
    """
    from app.courts.msudrf_court import CASE_URL_EXAMPLE

    assert validate_url(CASE_URL_EXAMPLE)
    assert is_supported_url(CASE_URL_EXAMPLE)
    assert looks_like_url(CASE_URL_EXAMPLE)
