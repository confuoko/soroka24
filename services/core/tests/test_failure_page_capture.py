"""Тесты сохранения страницы, на которой упал парсинг.

Ни сети, ни Chromium, ни записи в S3: браузер подменён заглушкой, save_snapshot —
регистратором вызовов. Проверяем три вещи:
  * клиент суда прикладывает к ошибке снимок страницы (снять его можно только там);
  * при отказе страница уходит в подпапку failed/, при успехе — в обычную папку дела;
  * ключ страницы отказа никогда не переиспользуется для карточки.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.courts import CaseNotFound, FetchFailed, PageSnapshot
from app.courts import moscow_mir_court
from app.models.database import SearchStatus, SearchTask, session_scope
from app.monitoring import tasks
from app.repositories import SearchTaskRepository
from app.storage.html_snapshots import is_failure_key

CASE_UID = "77MS0002-01-2026-000004-44"
CAPTCHA_HTML = "<html><body>Подтвердите, что вы не робот</body></html>"
CARD_HTML = "<html><body>карточка дела</body></html>"


# --------------------------------------------------------------- заглушка браузера
class _StubPage:
    def __init__(self, url: str, link_count: int) -> None:
        self.url = url
        self._link_count = link_count

    def locator(self, selector: str):
        return SimpleNamespace(count=lambda: self._link_count, first=object())


class _StubSession:
    """Мини-подмена ChromiumSession: только то, что использует клиент суда."""

    def __init__(
        self,
        fail_on: str | None = None,
        status: int = 200,
        link_count: int = 1,
        results_status: int | None = None,
        card_status: int | None = 200,
    ) -> None:
        self.fail_on = fail_on
        self.status = status
        # Статусы отдельных шагов: страница поиска -> выдача -> карточка дела.
        self.results_status = results_status
        self.card_status = card_status
        self.page = _StubPage("https://mos-sud.ru/search", link_count)

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
        moscow_mir_court.MoscowMirCourtClient().fetch_case_html_by_uid(CASE_UID)

    page = caught.value.page
    assert page.html == CAPTCHA_HTML
    assert page.url == "https://mos-sud.ru/search"
    assert page.status == 200
    assert isinstance(caught.value.reason, TimeoutError)


def test_case_not_found_carries_page_snapshot(stub_browser) -> None:
    """«Ничего не нашлось» — тоже повод сохранить страницу: возможно, поехал селектор."""
    stub_browser(link_count=0)

    with pytest.raises(CaseNotFound) as caught:
        moscow_mir_court.MoscowMirCourtClient().fetch_case_html_by_uid(CASE_UID)

    assert caught.value.page.html == CAPTCHA_HTML


def test_successful_fetch_returns_card_and_raises_nothing(stub_browser) -> None:
    """Когда всё хорошо — обычный HTML карточки, никаких снимков отказа."""
    stub_browser()

    assert moscow_mir_court.MoscowMirCourtClient().fetch_case_html_by_uid(CASE_UID) == CARD_HTML


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
        moscow_mir_court.MoscowMirCourtClient().fetch_case_html_by_uid(CASE_UID)

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
        moscow_mir_court.MoscowMirCourtClient().fetch_case_html_by_uid(CASE_UID)

    assert caught.value.page.status == 500


def test_card_page_error_does_not_reach_parser(stub_browser) -> None:
    """500 на самой карточке → FetchFailed, а не «успешно забрали HTML».

    Раньше страница ошибки уезжала в парсер, тот падал, и задача помечалась
    проваленной окончательно — ретрая не было вовсе, хотя причина временная.
    """
    stub_browser(card_status=503)

    with pytest.raises(FetchFailed) as caught:
        moscow_mir_court.MoscowMirCourtClient().fetch_case_html_by_uid(CASE_UID)

    assert caught.value.page.status == 503
    assert caught.value.page.html == CARD_HTML


# ------------------------------------------------------ куда кладём страницу отказа
@pytest.fixture
def recorded_uploads(monkeypatch):
    """Перехватить save_snapshot: в S3 в тестах не пишем, только фиксируем вызовы."""
    calls = []

    def _fake_save(uid, html, fetched_at, failed=False):
        key = f"html_snapshots/{uid}/{'failed/' if failed else ''}{uid}_stub.html.gz"
        calls.append({"uid": uid, "html": html, "failed": failed, "key": key})
        return {"html_bucket": "soroka", "html_key": key, "html_sha256": "sha", "html_size": len(html)}

    monkeypatch.setattr(tasks, "save_snapshot", _fake_save)
    return calls


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


def test_failure_page_goes_to_failed_subfolder(task_id, recorded_uploads, monkeypatch) -> None:
    """Страница отказа уходит в html_snapshots/<уид>/failed/, а page_status — в задачу."""
    failure = FetchFailed(
        CASE_UID,
        TimeoutError("Page.fill: Timeout 30000ms exceeded."),
        page=PageSnapshot(html=CAPTCHA_HTML, url="https://mos-sud.ru/search", status=403),
    )
    monkeypatch.setattr(
        tasks,
        "define_court_by_uid",
        lambda uid, proxy=None: SimpleNamespace(
            fetch_case_html_by_uid=lambda _: (_ for _ in ()).throw(failure)
        ),
    )

    tasks._sync_case(_StubTask(retries=_StubTask.max_retries), task_id)

    assert len(recorded_uploads) == 1
    upload = recorded_uploads[0]
    assert upload["failed"] is True
    assert upload["html"] == CAPTCHA_HTML
    assert is_failure_key(upload["key"])

    row = _row(task_id)
    assert row.status is SearchStatus.FAILED
    # Колонка page_status до этого не заполнялась ничем и была NULL у всех задач.
    assert row.page_status == 403


def test_nothing_saved_when_error_has_no_page(task_id, recorded_uploads, monkeypatch) -> None:
    """Упали до открытия страницы — сохранять нечего, хранилище не засоряем."""

    def _boom(uid: str, proxy=None):
        raise TimeoutError("сеть недоступна")

    monkeypatch.setattr(tasks, "define_court_by_uid", _boom)

    tasks._sync_case(_StubTask(retries=_StubTask.max_retries), task_id)

    assert recorded_uploads == []
    assert _row(task_id).page_status is None


# ------------------------------- ключ отказа не должен переиспользоваться карточкой
@pytest.fixture
def previous_entry(monkeypatch):
    """Подменить «предыдущую запись истории парсинга» дела на заданную."""

    def _install(entry: dict):
        monkeypatch.setattr(
            tasks,
            "CaseRepository",
            lambda session: SimpleNamespace(get_by_uid_and_court=lambda uid, court_id: object()),
        )
        monkeypatch.setattr(tasks, "last_entry", lambda case: entry)

    return _install


def test_snapshot_key_reused_after_successful_parse(previous_entry, recorded_uploads) -> None:
    """Разметка не изменилась с прошлого УСПЕШНОГО раза → повторно не заливаем."""
    sha = tasks.snapshot_sha256(CARD_HTML)
    previous_entry(
        {
            "html_key": f"html_snapshots/{CASE_UID}/{CASE_UID}_2026-08-04T15-00-00Z.html.gz",
            "html_bucket": "soroka",
            "html_sha256": sha,
            "html_size": len(CARD_HTML),
        }
    )

    snapshot, unchanged = tasks._take_snapshot(
        CASE_UID, CARD_HTML, datetime(2026, 8, 4, 16, 0, 0), court_id=1
    )

    assert unchanged is True
    assert recorded_uploads == []


def test_failure_key_is_never_reused_for_a_card(previous_entry, recorded_uploads) -> None:
    """Последней записью был отказ — его ключ карточке подставлять нельзя.

    Иначе история дела ссылалась бы на капчу как на разобранную карточку.
    """
    sha = tasks.snapshot_sha256(CARD_HTML)
    previous_entry(
        {
            "html_key": f"html_snapshots/{CASE_UID}/failed/{CASE_UID}_2026-08-04T15-00-00Z.html.gz",
            "html_bucket": "soroka",
            "html_sha256": sha,  # тот же sha — но ключ из failed/, переиспользовать нельзя
            "html_size": len(CARD_HTML),
        }
    )

    snapshot, unchanged = tasks._take_snapshot(
        CASE_UID, CARD_HTML, datetime(2026, 8, 4, 16, 0, 0), court_id=1
    )

    assert unchanged is False
    assert len(recorded_uploads) == 1
    assert recorded_uploads[0]["failed"] is False
    assert not is_failure_key(snapshot["html_key"])
