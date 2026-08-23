"""Тесты снимка страницы, на которой упал парсинг.

Ни сети, ни Chromium: браузер подменён заглушкой. Проверяем две вещи:
  * клиент суда прикладывает к ошибке снимок страницы (снять его можно только там);
  * HTTP-статус из этого снимка доезжает до SearchTask.page_status — по нему потом
    видно, отказал портал (403) или упало раньше.

Сама страница отказа больше никуда не сохраняется: архив разметки в S3 остался только
для отладки парсеров успешных карточек и по умолчанию выключен.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import discovery
from app.courts import CaseNotFound, FetchFailed, PageSnapshot
from app.courts import moscow as moscow_mir_court
from app.models import SearchStatus, SearchTask
from app.database import session_scope
from app import tasks
from app.repositories import SearchTaskRepository

CASE_UID = "77MS0002-01-2026-000004-44"
CAPTCHA_HTML = "<html><body>Подтвердите, что вы не робот</body></html>"
CARD_HTML = "<html><body>карточка дела</body></html>"


# --------------------------------------------------------------- заглушка браузера
class _StubLink:
    """Ссылка на карточку в таблице результатов: номер дела и участок из href."""

    def __init__(self, code: str, participok_no: int) -> None:
        self._code = code
        self._href = f"/{participok_no}/cases/admin/details/stub?uid=x&formType=fullForm"

    def inner_text(self) -> str:
        return self._code

    def get_attribute(self, name: str) -> str:
        return self._href if name == "href" else ""


class _StubPage:
    def __init__(self, url: str, links: list[_StubLink]) -> None:
        self.url = url
        self._links = links

    def locator(self, selector: str):
        return SimpleNamespace(
            count=lambda: len(self._links),
            nth=lambda index: self._links[index],
        )


class _StubSession:
    """Мини-подмена ChromiumSession: только то, что использует клиент суда."""

    def __init__(
        self,
        fail_on: str | None = None,
        status: int = 200,
        link_count: int = 1,
        results_status: int | None = None,
        card_status: int | None = 200,
        links: list[_StubLink] | None = None,
    ) -> None:
        self.fail_on = fail_on
        self.status = status
        # Статусы отдельных шагов: страница поиска -> выдача -> карточка дела.
        self.results_status = results_status
        self.card_status = card_status
        # По умолчанию — link_count одинаковых строк таблицы; links задаёт их явно,
        # когда в тесте важны конкретные номера дел и участков.
        if links is None:
            links = [_StubLink(f"05-{i:04d}/2/2026", 2) for i in range(link_count)]
        self.page = _StubPage("https://mos-sud.ru/search", links)

    def __enter__(self) -> "_StubSession":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def goto(self, url: str, timeout: int | None = None):
        return SimpleNamespace(status=self.status)

    def fill(self, selector: str, value: str) -> None:
        if self.fail_on == "fill":
            raise TimeoutError('Page.fill: Timeout 30000ms exceeded.')

    def submit_and_wait(self, selector: str, timeout: int | None = None) -> int | None:
        return self.results_status

    def open_in_new_tab(self, locator, timeout: int | None = None) -> tuple[str, int | None]:
        return CARD_HTML, self.card_status

    def content(self) -> str:
        return CAPTCHA_HTML


@pytest.fixture
def stub_browser(monkeypatch):
    """Подменить ChromiumSession в клиенте суда. Возвращает настройщик заглушки."""

    def _install(**kwargs) -> _StubSession:
        stub = _StubSession(**kwargs)
        # proxy клиент суда передаёт всегда (в тесте — None): подпись должна его принять.
        monkeypatch.setattr(
            moscow_mir_court, "ChromiumSession", lambda headless=True, proxy=None: stub
        )
        return stub

    return _install


# ------------------------------------------------- клиент суда прикладывает снимок
def test_fetch_failure_carries_page_snapshot(stub_browser) -> None:
    """Таймаут на поле ввода → FetchFailed со снимком страницы, url и HTTP-статусом.

    Ровно тот отказ, что был у задач 6 и 7: goto прошёл, а input[name="uid"] не появился.
    Раньше наружу уходил голый Page.fill: Timeout, и понять, что отдал портал, было нельзя.

    Статус здесь 200: портал ответил как ни в чём не бывало, а разметка оказалась не та.
    Явные коды ошибок (403/429/5xx) отсекаются раньше — см. тесты ниже.
    """
    stub_browser(fail_on="fill", status=200)

    with pytest.raises(FetchFailed) as caught:
        moscow_mir_court.MoscowClient().fetch_cases_by_uid(CASE_UID)

    page = caught.value.page
    assert page.html == CAPTCHA_HTML
    assert page.url == "https://mos-sud.ru/search"
    assert page.status == 200
    assert isinstance(caught.value.reason, TimeoutError)


def test_case_not_found_carries_page_snapshot(stub_browser) -> None:
    """«Ничего не нашлось» — тоже повод сохранить страницу: возможно, поехал селектор."""
    stub_browser(link_count=0)

    with pytest.raises(CaseNotFound) as caught:
        moscow_mir_court.MoscowClient().fetch_cases_by_uid(CASE_UID)

    assert caught.value.page.html == CAPTCHA_HTML


def test_successful_fetch_returns_card_and_raises_nothing(stub_browser) -> None:
    """Когда всё хорошо — обычный HTML карточки, никаких снимков отказа."""
    stub_browser()

    cards = moscow_mir_court.MoscowClient().fetch_cases_by_uid(CASE_UID)

    assert [c.html for c in cards] == [CARD_HTML]
    assert cards[0].case_code == "05-0000/2/2026"
    assert cards[0].participok_no == 2


# ------------------------------------------- ошибка портала должна быть ретраибельной
@pytest.mark.parametrize("status", [500, 502, 503, 403, 429])
def test_search_page_error_fails_fast(stub_browser, status) -> None:
    """Портал ответил ошибкой → падаем сразу, не дожидаясь таймаута на поле формы.

    Заглушка настроена так, что fill вообще упал бы по таймауту; раз до него не дошло,
    значит статус проверен раньше. Для Playwright 500 — успешная навигация, поэтому
    без явной проверки здесь сгорало бы 30 секунд на каждой попытке.
    """
    stub_browser(fail_on="fill", status=status)

    with pytest.raises(FetchFailed) as caught:
        moscow_mir_court.MoscowClient().fetch_cases_by_uid(CASE_UID)

    assert caught.value.page.status == status
    assert not isinstance(caught.value.reason, TimeoutError)
    assert str(status) in str(caught.value.reason)


def test_results_page_error_is_not_mistaken_for_case_not_found(stub_browser) -> None:
    """500 на выдаче — это отказ портала, а не «дело не найдено».

    Ссылок на странице ошибки тоже ноль, поэтому без проверки статуса временный сбой
    выглядел бы как CaseNotFound — окончательный отказ, который никто не повторит.
    """
    stub_browser(status=200, results_status=500, link_count=0)

    with pytest.raises(FetchFailed) as caught:
        moscow_mir_court.MoscowClient().fetch_cases_by_uid(CASE_UID)

    assert caught.value.page.status == 500


def test_card_page_error_does_not_reach_parser(stub_browser) -> None:
    """500 на самой карточке → FetchFailed, а не «успешно забрали HTML».

    Раньше страница ошибки уезжала в парсер, тот падал, и задача помечалась
    проваленной окончательно — ретрая не было вовсе, хотя причина временная.
    """
    stub_browser(card_status=503)

    with pytest.raises(FetchFailed) as caught:
        moscow_mir_court.MoscowClient().fetch_cases_by_uid(CASE_UID)

    assert caught.value.page.status == 503
    assert caught.value.page.html == CARD_HTML


# ----------------------------------------------- статус страницы отказа доезжает в БД
@pytest.fixture
def task_id():
    """Реальная строка search_task; после теста удаляется."""
    with session_scope() as session:
        created_id = SearchTaskRepository(session).create(CASE_UID).id

    yield created_id

    with session_scope() as session:
        row = session.get(SearchTask, created_id)
        if row is not None:
            session.delete(row)


def _row(task_id: int) -> SearchTask:
    with session_scope() as session:
        return session.get(SearchTask, task_id)


class _StubTask:
    max_retries = 3

    def __init__(self, retries: int) -> None:
        self.request = SimpleNamespace(retries=retries)

    def retry(self, exc=None, countdown=None):
        raise AssertionError("ретрай в этом тесте не ожидается")


def test_failure_page_status_reaches_the_task(task_id, monkeypatch) -> None:
    """HTTP-статус страницы, на которой отказали, уходит в SearchTask.page_status."""
    failure = FetchFailed(
        CASE_UID,
        TimeoutError("Page.fill: Timeout 30000ms exceeded."),
        page=PageSnapshot(html=CAPTCHA_HTML, url="https://mos-sud.ru/search", status=403),
    )
    monkeypatch.setattr(discovery, "define_court_by_uid",
        lambda uid, proxy=None, **kwargs: SimpleNamespace(
            fetch_cases_by_uid=lambda _: (_ for _ in ()).throw(failure)
        ),
    )

    tasks._run(_StubTask(retries=_StubTask.max_retries), task_id)

    row = _row(task_id)
    assert row.status is SearchStatus.FAILED
    # Колонка page_status до этого не заполнялась ничем и была NULL у всех задач.
    assert row.page_status == 403


def test_page_status_is_empty_when_error_has_no_page(task_id, monkeypatch) -> None:
    """Упали до открытия страницы — снимка нет, статусу взяться неоткуда."""

    def _boom(uid: str, proxy=None, **kwargs):
        raise TimeoutError("сеть недоступна")

    monkeypatch.setattr(discovery, "define_court_by_uid", _boom)

    tasks._run(_StubTask(retries=_StubTask.max_retries), task_id)

    assert _row(task_id).page_status is None
