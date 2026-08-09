"""Путь до карточки дела на порталах msudrf.ru (Московская область и ещё 71 регион).

Ни сети, ни Chromium, ни сервиса распознавания: браузер подменён заглушкой, решатель
капчи — счётчиком вызовов. Проверяем то, ради чего клиент и написан:
  * капча выпадает не всегда — если её нет, разгадывать ничего не надо;
  * после верно разгаданной капчи портал умеет показать вторую подряд;
  * попытки конечны, и когда они кончились — это ВРЕМЕННЫЙ отказ, не «дело не найдено»;
  * УИД берётся со страницы, потому что из ссылки суд не выводится.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.captcha import ATTEMPT_SOLVED, CaptchaAttempt
from app.courts import CaseNotFound, FetchFailed
from app.courts import msudrf_court
from app.courts.msudrf_court import MsudrfCourtClient

CASE_URL = (
    "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs"
    "&case_id=429386415&delo_id=1540005"
)
UID = "50MS0095-01-2026-002990-16"

CAPTCHA_HTML = (
    "<html><body><h2>Для продолжения необходимо пройти дополнительную проверку</h2>"
    '<form id="kcaptchaForm"><img src="/captcha.php"></form></body></html>'
)
CARD_HTML = f"<html><body>Уникальный идентификатор дела: {UID}</body></html>"
# Страница открылась, но карточки на ней нет (дело сняли с публикации).
EMPTY_HTML = "<html><body>Информация по делу отсутствует</body></html>"


# --------------------------------------------------------------- заглушка браузера
class _StubLocator:
    def __init__(self, screenshots: list) -> None:
        self._screenshots = screenshots

    def screenshot(self) -> bytes:
        png = f"png-{len(self._screenshots)}".encode()
        self._screenshots.append(png)
        return png


class _StubPage:
    def __init__(self, session: "_StubSession") -> None:
        self._session = session
        self.url = CASE_URL

    def locator(self, selector: str) -> _StubLocator:
        return _StubLocator(self._session.screenshots)

    def fill(self, selector: str, value: str) -> None:
        self._session.typed.append(value)


class _StubSession:
    """Мини-подмена ChromiumSession: только то, что использует клиент суда.

    pages — что портал отдаёт по очереди: первый элемент на goto, следующие после
    каждой отправки формы.
    """

    def __init__(self, pages: list[str], status: int = 200) -> None:
        self._pages = list(pages)
        self._current = self._pages.pop(0)
        self.status = status
        self.screenshots: list[bytes] = []
        self.typed: list[str] = []
        self.submits = 0
        self.ignore_https_errors = None
        self.page = _StubPage(self)

    def __enter__(self) -> "_StubSession":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def goto(self, url: str, timeout: int | None = None):
        return SimpleNamespace(status=self.status)

    def submit_and_wait(self, selector: str, timeout: int | None = None) -> int | None:
        self.submits += 1
        if self._pages:
            self._current = self._pages.pop(0)
        return None

    def content(self) -> str:
        return self._current


@pytest.fixture
def portal(monkeypatch):
    """Настроить портал и решатель капчи. Возвращает (сессия, вызовы решателя)."""

    def _install(pages: list[str], status: int = 200) -> tuple[_StubSession, list]:
        session = _StubSession(pages, status=status)
        solved = []

        def _solve(png: bytes, on_attempt=None) -> CaptchaAttempt:
            solved.append(png)
            attempt = CaptchaAttempt(
                task_id=1000 + len(solved),
                status=ATTEMPT_SOLVED,
                text=f"answer-{len(solved)}",
                cost=Decimal("0.03"),
            )
            # Настоящий решатель сообщает о расходе сам, до возврата ответа.
            if on_attempt is not None:
                on_attempt(attempt)
            return attempt

        def _session_factory(headless=True, proxy=None, ignore_https_errors=False):
            session.ignore_https_errors = ignore_https_errors
            return session

        monkeypatch.setattr(msudrf_court, "ChromiumSession", _session_factory)
        monkeypatch.setattr(msudrf_court, "solve_image", _solve)
        # В S3 не ходим, но проверяем, что картинку туда отдавали.
        monkeypatch.setattr(
            msudrf_court,
            "save_captcha",
            lambda url, png, at: {"captcha_key": f"captcha/{len(png)}.png"},
        )
        return session, solved

    return _install


def _fetch(client=None) -> tuple[str, MsudrfCourtClient]:
    client = client or MsudrfCourtClient()
    return client.fetch_case_html_by_url(CASE_URL), client


# ------------------------------------------------------------------ капчи не было
def test_page_without_captcha_needs_no_solving(portal) -> None:
    """Капча выпадает не всегда: страница открылась сразу — решатель не дёргаем."""
    session, solved = portal([CARD_HTML])

    html, client = _fetch()

    assert html == CARD_HTML
    assert solved == []
    assert session.submits == 0
    assert client.captchas_solved == 0


def test_certificate_check_is_relaxed(portal) -> None:
    """У поддоменов участков сертификат не совпадает с именем — иначе не открыть."""
    session, _ = portal([CARD_HTML])

    _fetch()

    assert session.ignore_https_errors is True


# ------------------------------------------------------------------- одна капча
def test_single_captcha_is_solved(portal) -> None:
    """Показали проверку → разгадали, ввели ответ, отправили форму, получили карточку."""
    session, solved = portal([CAPTCHA_HTML, CARD_HTML])

    html, client = _fetch()

    assert html == CARD_HTML
    assert len(solved) == 1
    assert session.typed == ["answer-1"]
    assert session.submits == 1
    assert client.captchas_solved == 1


def test_captcha_image_comes_from_screenshot(portal) -> None:
    """Картинку снимаем со страницы скриншотом.

    Скачивать /captcha.php нельзя: повторный запрос сгенерирует НОВУЮ картинку, и
    разгаданный ответ к показанной на странице уже не подойдёт.
    """
    session, solved = portal([CAPTCHA_HTML, CARD_HTML])

    _fetch()

    assert solved == session.screenshots


# ------------------------------------------------------- вторая капча после первой
def test_second_captcha_in_a_row_is_handled(portal) -> None:
    """Портал умеет показать вторую проверку после верно разгаданной первой."""
    session, solved = portal([CAPTCHA_HTML, CAPTCHA_HTML, CARD_HTML])

    html, client = _fetch()

    assert html == CARD_HTML
    assert len(solved) == 2
    assert session.typed == ["answer-1", "answer-2"]
    assert client.captchas_solved == 2


# --------------------------------------------------------------- учёт расходов
def test_each_captcha_is_reported_for_accounting(portal) -> None:
    """Каждая капча уходит в учёт с тем, что знает только клиент суда.

    Номер проверки за поход и ключ картинки в S3 решателю неизвестны, а без них по
    записи не понять, за что заплатили и сколько раз портал показал проверку.
    """
    session, _ = portal([CAPTCHA_HTML, CAPTCHA_HTML, CARD_HTML])
    reported = []

    html, _ = _fetch(MsudrfCourtClient(on_captcha_attempt=reported.append))

    assert html == CARD_HTML
    assert [a.attempt_no for a in reported] == [1, 2]
    assert all(a.captcha_key for a in reported)
    assert [a.text for a in reported] == ["answer-1", "answer-2"]


def test_accounting_is_optional(portal) -> None:
    """Без подключённого учёта клиент работает как раньше: капча решается, ошибок нет."""
    portal([CAPTCHA_HTML, CARD_HTML])

    html, client = _fetch()

    assert html == CARD_HTML
    assert client.captchas_solved == 1


# ------------------------------------------------------------- попытки исчерпаны
def test_gives_up_after_configured_attempts(portal, monkeypatch) -> None:
    """Капча на всех попытках → FetchFailed (временный отказ), а не тихий возврат её HTML.

    Важно, что это именно FetchFailed: задача поретраится и возьмёт другой прокси.
    Если бы клиент вернул страницу капчи как карточку, парсер упал бы окончательно.
    """
    monkeypatch.setattr(msudrf_court, "CAPTCHA_ATTEMPTS", 3)
    session, solved = portal([CAPTCHA_HTML] * 5)

    with pytest.raises(FetchFailed) as caught:
        _fetch()

    assert len(solved) == 3
    assert caught.value.page.html == CAPTCHA_HTML


def test_report_incorrect_is_never_called(portal, monkeypatch) -> None:
    """Жалобу в сервис не шлём: повторная капча не значит, что ответ был неверный."""
    from app.captcha import rucaptcha

    monkeypatch.setattr(
        rucaptcha,
        "report_incorrect",
        lambda task_id: pytest.fail("report_incorrect вызывать не должны"),
    )
    portal([CAPTCHA_HTML] * 5)

    with pytest.raises(FetchFailed):
        _fetch()


# ----------------------------------------------------------------- отказы портала
def test_server_error_skips_captcha_entirely(portal) -> None:
    """500 на заходе → падаем сразу, решатель капчи не трогаем и денег не тратим."""
    _, solved = portal([CAPTCHA_HTML], status=500)

    with pytest.raises(FetchFailed):
        _fetch()

    assert solved == []


# --------------------------------------------------------------------- УИД со страницы
def test_uid_is_extracted_from_card() -> None:
    """УИД берём со страницы: по нему потом резолвится суд (первые 8 символов — код)."""
    assert MsudrfCourtClient().extract_uid(CARD_HTML) == UID
    assert UID[:8] == "50MS0095"


def test_missing_uid_is_case_not_found() -> None:
    """Страница есть, а карточки на ней нет — окончательный отказ, повторять нечего."""
    with pytest.raises(CaseNotFound):
        MsudrfCourtClient().extract_uid(EMPTY_HTML)


# --------------------------------------------------------------- номер дела со страницы
def test_case_code_is_extracted_from_heading() -> None:
    """Номер дела есть только в заголовке: в таблице карточки его нет вовсе."""
    html = "<html><body><h2>ДЕЛО № 2-1244/2026</h2></body></html>"

    assert MsudrfCourtClient().extract_case_code(html) == "2-1244/2026"


def test_material_case_code_starts_with_cyrillic_letter() -> None:
    """У материала номер начинается с кириллической «М» — цифрами номер не ограничен."""
    html = "<html><body><h2>ДЕЛО № М-2987/2026</h2></body></html>"

    assert MsudrfCourtClient().extract_case_code(html) == "М-2987/2026"


def test_missing_case_code_is_case_not_found() -> None:
    """Без номера дело не сохранить (он в ключе карточки) — повторять поход бессмысленно."""
    with pytest.raises(CaseNotFound):
        MsudrfCourtClient().extract_case_code(EMPTY_HTML)
