"""Пробы порталов: как проба различает «дошли», «капча», «забанили» и «сломалось».

Ни сети, ни Chromium: браузер подменён заглушкой. Проверяем ровно то, ради чего
проба написана, — вердикт по ответу портала. Сами походы через настоящий прокси
живут в scripts/check_proxy.py --sites и в pytest не тащатся.
"""
from types import SimpleNamespace

import pytest

from app.courts import site_probe
from app.courts.site_probe import (
    BLOCKED,
    CAPTCHA,
    ERROR,
    OK,
    SITE_PROBES,
    one_line,
    probe_site,
    short_error,
)

CAPTCHA_HTML = (
    "<html><body><h2>Для продолжения необходимо пройти дополнительную проверку</h2>"
    '<form id="kcaptchaForm"><img src="/captcha.php"></form></body></html>'
)
CARD_HTML = "<html><body><h2>ДЕЛО № 2-1244/2026</h2></body></html>"
EMPTY_HTML = "<html><body>Информация по делу отсутствует</body></html>"


class _StubLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class _StubPage:
    def __init__(self, links: int) -> None:
        self._links = links

    def locator(self, selector: str) -> _StubLocator:
        return _StubLocator(self._links)


class _StubSession:
    """Мини-подмена ChromiumSession: только то, что дёргает проба."""

    def __init__(
        self,
        html: str = CARD_HTML,
        status: int | None = 200,
        results_status: int | None = None,
        links: int = 0,
        goto_error: Exception | None = None,
    ) -> None:
        self._html = html
        self._status = status
        self._results_status = results_status
        self._goto_error = goto_error
        self.page = _StubPage(links)
        self.ignore_https_errors = None
        self.filled: list[str] = []

    def __enter__(self) -> "_StubSession":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def goto(self, url: str, timeout: int | None = None):
        if self._goto_error is not None:
            raise self._goto_error
        return SimpleNamespace(status=self._status)

    def fill(self, selector: str, value: str) -> None:
        self.filled.append(value)

    def submit_and_wait(self, selector: str, timeout: int | None = None) -> int | None:
        return self._results_status

    def content(self) -> str:
        return self._html


@pytest.fixture
def portal(monkeypatch):
    """Подменить браузер заглушкой. Возвращает саму заглушку, чтобы её опросить."""

    def _install(**kwargs) -> _StubSession:
        session = _StubSession(**kwargs)

        def _factory(headless=True, proxy=None, ignore_https_errors=False):
            session.ignore_https_errors = ignore_https_errors
            return session

        monkeypatch.setattr(site_probe, "ChromiumSession", _factory)
        return session

    return _install


def _probe(name: str):
    return probe_site(SITE_PROBES[name])


# ------------------------------------------------------------------------ msudrf
def test_msudrf_card_is_ok(portal) -> None:
    """Портал отдал карточку — прокси годится, в пояснении виден номер дела."""
    portal(html=CARD_HTML)

    result = _probe("msudrf")

    assert result.verdict == OK
    assert result.ok
    assert "2-1244/2026" in result.detail


def test_msudrf_captcha_counts_as_success(portal) -> None:
    """Капча — успех: до забаненного IP портал её не доводит, он отвечает 403.

    Разгадывать её здесь нельзя — это деньги, а на вопрос «доходит ли прокси»
    сам факт показанной проверки уже отвечает.
    """
    portal(html=CAPTCHA_HTML)

    result = _probe("msudrf")

    assert result.verdict == CAPTCHA
    assert result.ok


def test_msudrf_403_is_blocked(portal) -> None:
    """403 — нас отсекли по IP. Именно этим один прокси отличается от другого."""
    portal(html=CARD_HTML, status=403)

    result = _probe("msudrf")

    assert result.verdict == BLOCKED
    assert "403" in result.detail
    assert not result.ok


def test_msudrf_500_is_blocked(portal) -> None:
    """5xx тоже не успех: карточку мы не получили, вердикт должен быть отрицательным."""
    portal(html=CARD_HTML, status=500)

    assert _probe("msudrf").verdict == BLOCKED


def test_msudrf_foreign_page_is_error(portal) -> None:
    """Страница открылась, но это не карточка — успехом считать нельзя."""
    portal(html=EMPTY_HTML)

    assert _probe("msudrf").verdict == ERROR


def test_msudrf_relaxes_certificate_check(portal) -> None:
    """У поддоменов движка сертификат не совпадает с именем — иначе не откроется вовсе."""
    session = portal(html=CARD_HTML)

    _probe("msudrf")

    assert session.ignore_https_errors is True


# ----------------------------------------------------------------------- mos-sud
def test_mos_sud_search_results_are_ok(portal) -> None:
    """Проба реально ищет дело по УИД, а не просто открывает форму поиска.

    Форму портал отдаёт всем, отсекает он уже на самом запросе — иначе проба
    зеленела бы на прокси, который до выдачи не доходит.
    """
    session = portal(links=2)

    result = _probe("mos-sud")

    assert result.verdict == OK
    assert session.filled == [site_probe.MOS_SUD_UID]
    assert "2" in result.detail


def test_mos_sud_403_on_results_is_blocked(portal) -> None:
    """Заход прошёл, а выдача ответила 403 — статус выдачи и есть ответ на вопрос."""
    portal(status=200, results_status=403, links=0)

    result = _probe("mos-sud")

    assert result.verdict == BLOCKED
    assert "403" in result.detail


def test_mos_sud_empty_results_are_not_ok(portal) -> None:
    """Пустая выдача — не успех: либо дело сняли, либо поехала разметка."""
    portal(links=0)

    assert _probe("mos-sud").verdict == ERROR


def test_mos_sud_keeps_certificate_check(portal) -> None:
    """У mos-sud.ru сертификат в порядке — ослаблять проверку незачем."""
    session = portal(links=1)

    _probe("mos-sud")

    assert session.ignore_https_errors is False


# -------------------------------------------------------------------- сетевые отказы
def test_network_failure_is_error_not_crash(portal) -> None:
    """Прокси не отозвался — проба обязана вернуть вердикт, а не уронить весь прогон.

    Иначе один мёртвый адрес обрывал бы проверку всего пула.
    """
    portal(goto_error=RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED"))

    result = _probe("msudrf")

    assert result.verdict == ERROR
    assert "ERR_TUNNEL_CONNECTION_FAILED" in result.detail


def test_error_text_is_trimmed_to_one_line() -> None:
    """Playwright пишет в исключение весь call log — в таблицу лезет только первая строка.

    Без этого многострочный отказ рвёт итоговую таблицу на куски.
    """
    exc = RuntimeError("Timeout 60000ms exceeded.\nCall log:\n  - navigating to ...")

    text = short_error(exc)

    assert text.startswith("RuntimeError: Timeout 60000ms exceeded.")
    assert "\n" not in text
    assert len(text) < 130


def test_long_error_is_cut_to_limit() -> None:
    """Длинную первую строку тоже режем — колонка таблицы не резиновая."""
    text = one_line("x" * 200, limit=40)

    assert len(text) == 40
    assert text.endswith("...")
