"""Путь до карточки дела на портале Санкт-Петербурга (mirsud.spb.ru, тип D).

Ни сети, ни Chromium: браузер подменён заглушкой. Проверяем то, ради чего клиент и
написан отдельно от двух существующих:

  * карточка дорисовывается фоновой задачей портала — сразу после goto страница пустая,
    и отдавать её нельзя;
  * не дождались отрисовки — это ВРЕМЕННЫЙ отказ, задача должна поретраиться;
  * отказ портала (403) виден сразу, до полутора минут ожидания разметки;
  * сертификат здесь НЕ ослабляется, в отличие от msudrf.ru;
  * суд определяется по номеру участка из пути ссылки, потому что хост общий на регион.
"""
from types import SimpleNamespace

import pytest

from app.courts import CaseNotFound, FetchFailed
from app.courts import spb as spb_mir_court
from app.courts.base import find_uid
from app.courts.spb import extract_spb_case_code
from app.courts.spb import SpbClient, participok_from_url

CASE_URL = "https://mirsud.spb.ru/cases/detail/98/?id=2-2983%2F2026-98"
UID = "78MS0098-01-2026-003978-73"

# Страница сразу после goto: разметка есть, данных ещё нет — фоновая задача не отработала.
PENDING_HTML = (
    "<html><head><title>Судебное дело №2-2983/2026-98 | Мировые судьи</title></head>"
    "<body><table class='case-print__table'></table></body></html>"
)
# Она же после отрисовки: появились подписи полей и УИД.
RENDERED_HTML = (
    "<html><head><title>Судебное дело №2-2983/2026-98 | Мировые судьи</title></head>"
    "<body><table class='case-print__table'>"
    "<tr><td><b class='table-title'>№ участка</b></td><td>98</td></tr>"
    f"<tr><td><b class='table-title'>УИД</b></td><td>{UID}</td></tr>"
    "</table></body></html>"
)


class _StubPage:
    """Страница, которая наполняется данными только после ожидания селектора."""

    def __init__(self, session: "_StubSession") -> None:
        self._session = session
        # capture_page() читает адрес страницы, снимая отказ, — без него снимок не выйдет.
        self.url = CASE_URL

    def wait_for_selector(self, selector: str, timeout: int | None = None):
        self._session.waited.append((selector, timeout))
        if self._session.render_error is not None:
            raise self._session.render_error
        # Портал дорисовал карточку — только теперь на странице есть данные.
        self._session.current = self._session.rendered
        return SimpleNamespace()


class _StubSession:
    def __init__(
        self,
        pending: str = PENDING_HTML,
        rendered: str = RENDERED_HTML,
        status: int = 200,
        render_error: Exception | None = None,
    ) -> None:
        self.current = pending
        self.rendered = rendered
        self.status = status
        self.render_error = render_error
        self.waited: list = []
        self.ignore_https_errors = None
        self.page = _StubPage(self)

    def __enter__(self) -> "_StubSession":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def goto(self, url: str, timeout: int | None = None):
        return SimpleNamespace(status=self.status)

    def content(self) -> str:
        return self.current


@pytest.fixture
def portal(monkeypatch):
    """Подменить браузер заглушкой. Возвращает её, чтобы потом опросить."""

    def _install(**kwargs) -> _StubSession:
        session = _StubSession(**kwargs)

        def _factory(headless=True, proxy=None, ignore_https_errors=False):
            session.ignore_https_errors = ignore_https_errors
            return session

        monkeypatch.setattr(spb_mir_court, "ChromiumSession", _factory)
        return session

    return _install


def _fetch() -> str:
    """Сходить за карточкой и отдать её РАЗМЕТКУ (клиент теперь возвращает FetchedCard)."""
    return SpbClient().fetch_card_by_url(CASE_URL).html


# --------------------------------------------------------- ожидание отрисовки
def test_waits_for_the_card_to_render(portal) -> None:
    """Портал рисует карточку фоновой задачей — сразу после goto данных ещё нет.

    Если отдать страницу не дожидаясь, в S3 уедет пустая разметка, а УИД не найдётся.
    """
    session = portal()

    html = _fetch()

    assert html == RENDERED_HTML
    assert UID in html
    assert session.waited == [(spb_mir_court.RENDERED_MARK, spb_mir_court.RENDER_TIMEOUT)]


def test_render_timeout_is_retryable(portal) -> None:
    """Не дождались отрисовки → FetchFailed (временный отказ), а не «дело не найдено».

    Важно именно это: задача поретраится и на следующем заходе возьмёт другой прокси.
    Если бы отказ был окончательным, единичная задержка портала хоронила бы дело.
    """
    portal(render_error=RuntimeError("Timeout 90000ms exceeded"))

    with pytest.raises(FetchFailed) as caught:
        _fetch()

    # К отказу приложен снимок страницы — по нему потом видно, что портал успел отдать.
    assert caught.value.page is not None


def test_portal_error_skips_waiting(portal) -> None:
    """403 виден сразу: ждать полторы минуты разметки на странице отказа незачем."""
    session = portal(status=403)

    with pytest.raises(FetchFailed):
        _fetch()

    assert session.waited == []


def test_certificate_check_is_not_relaxed(portal) -> None:
    """У mirsud.spb.ru сертификат в порядке — в отличие от поддоменов msudrf.ru."""
    session = portal()

    _fetch()

    assert session.ignore_https_errors is False


# ------------------------------------------------------------- данные со страницы
def test_uid_is_extracted_from_rendered_card(portal) -> None:
    """УИД берётся со страницы: по нему сверяется суд, определённый по ссылке."""
    portal()

    assert find_uid(_fetch()) == UID
    assert UID[:8] == "78MS0098"


def test_case_code_is_extracted_from_heading() -> None:
    """Номер дела стоит в заголовке; хвост « | Мировые судьи…» в него попасть не должен."""
    assert extract_spb_case_code(RENDERED_HTML) == "2-2983/2026-98"


def test_missing_case_code_is_case_not_found() -> None:
    """Без номера дела карточку не сохранить — он в её ключе и в имени папки в S3."""
    with pytest.raises(CaseNotFound):
        extract_spb_case_code("<html><body>ничего</body></html>")


# --------------------------------------------------- номер участка из ссылки
def test_participok_comes_from_the_url_path() -> None:
    """Хост общий на 211 судов, поэтому суд ищется по номеру участка из пути."""
    assert participok_from_url(CASE_URL) == 98
    assert participok_from_url("https://mirsud.spb.ru/cases/detail/9/?id=5-1628%2F2026-9") == 9


def test_participok_is_none_for_other_paths() -> None:
    """Не карточка дела — номера нет; молча подставлять что-то нельзя."""
    assert participok_from_url("https://mirsud.spb.ru/court-sites/98") is None
    assert participok_from_url("https://95.mo.msudrf.ru/modules.php?case_id=1") is None
    assert participok_from_url("") is None


def test_url_number_is_participok_not_court_code() -> None:
    """В пути стоит номер УЧАСТКА, а не число из кода суда — и они расходятся.

    У участка № 126 код 78MS0124, а 78MS0126 — это совсем другой суд (участок № 128).
    Тест стережёт документацию: соблазн вывести код арифметикой из ссылки велик.
    """
    assert participok_from_url("https://mirsud.spb.ru/cases/detail/126/?id=2-1557%2F2026-126") == 126
