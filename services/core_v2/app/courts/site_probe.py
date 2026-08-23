"""Пробы порталов: доходит ли конкретный прокси до конкретного сайта судов.

Зачем отдельно от check_proxy.py. Проверка «прокси жив» (поход на api.ipify.org) на
вопрос «годится ли он для дела» не отвечает: прокси бывают живые, но забаненные
порталом, причём по-разному — один ходит на mos-sud.ru и не ходит на msudrf.ru,
другой наоборот. Так что проверять надо каждый портал отдельно, тем же браузером и
тем же путём, каким туда ходит бой.

Капчу проба НЕ разгадывает: разгадка стоит денег, а для ответа «прокси доходит»
достаточно самого факта, что портал показал проверку — до заблокированного IP он её
не показывает, он отдаёт 403. Поэтому verdict CAPTCHA считается успехом: дальше
сработает штатный решатель (app/courts/msudrf_court.py).

Селекторы и маркеры сознательно импортируются у боевых клиентов, а не копируются:
иначе проба однажды начнёт проверять разметку, которой на портале уже нет, и будет
бодро зеленеть на сломанном прокси.

Живёт в app/courts/, а не в app/browser/: проба знает про порталы и их разметку,
то есть это слой судов, который поверх браузера, а не рядом с ним.
"""
import time
from dataclasses import dataclass
from typing import Callable

from app.browser import ChromiumSession, ProxySettings
from app.courts.base import is_retryable_status
from app.courts.moscow import (
    DETAIL_LINK,
    SEARCH_BUTTON,
    SEARCH_URL,
    UID_INPUT,
)
from app.courts.base import find_uid
from app.courts.msudrf import CAPTCHA_MARK, CASE_CODE_RE
from app.courts.spb import RENDER_TIMEOUT, RENDERED_MARK

# Вердикты пробы. OK и CAPTCHA — успех (прокси до портала доходит), остальное — нет.
OK = "ok"
CAPTCHA = "captcha"
BLOCKED = "blocked"
ERROR = "error"

SUCCESS = frozenset({OK, CAPTCHA})

# Что дёргаем на порталах. Оба адреса взяты из БД — это реальные дела, которые уже
# заводились в системе; синтетика тут не годится, портал должен ответить настоящей
# карточкой.
#
# УИД московского дела (case.id=14, участок № 2). Если дело когда-нибудь снимут с
# публикации, проба начнёт отдавать «выдача пустая» — тогда сюда нужен свежий УИД.
MOS_SUD_UID = "77MS0002-01-2026-001579-64"
# Карточка дела на движке msudrf.ru (case_url.id=202, судебный участок № 369 МО).
MSUDRF_CASE_URL = (
    "https://369.mo.msudrf.ru/modules.php"
    "?case_id=436712856&delo_id=1540005&name=sud_delo&op=cs"
)
# Карточка дела на портале Санкт-Петербурга (участок № 98, суд 78MS0098).
SPB_CASE_URL = "https://mirsud.spb.ru/cases/detail/98/?id=2-2983%2F2026-98"


@dataclass(frozen=True)
class ProbeResult:
    """Чем закончилась одна проба одного портала через один прокси."""

    verdict: str
    detail: str
    elapsed: float

    @property
    def ok(self) -> bool:
        return self.verdict in SUCCESS

    def __str__(self) -> str:
        return f"{self.verdict} {self.detail}".strip()


@dataclass(frozen=True)
class Probe:
    """Описание пробы одного портала.

    run — что делать после того, как страница открылась и её статус проверен.
    Принимает сессию и статус навигации, возвращает пару (вердикт, пояснение).
    """

    name: str
    url: str
    ignore_https_errors: bool
    run: Callable[[ChromiumSession, int | None], tuple[str, str]]


def _run_mos_sud(session: ChromiumSession, status: int | None) -> tuple[str, str]:
    """Портал Москвы (тип A): реально ищем дело по УИД.

    Проверять один GET страницы поиска мало: форму портал отдаёт всем, а отсекает
    уже на самом запросе. Капчи здесь нет, поиск бесплатный — гоняем его целиком.
    """
    session.fill(UID_INPUT, MOS_SUD_UID)
    results_status = session.submit_and_wait(SEARCH_BUTTON)
    if results_status is not None:
        status = results_status
    # Статус выдачи проверяем до подсчёта ссылок: на странице отказа их тоже ноль.
    if is_retryable_status(status):
        return BLOCKED, f"HTTP {status} на выдаче"

    found = session.page.locator(DETAIL_LINK).count()
    if found:
        return OK, f"дел в выдаче: {found}"
    return ERROR, "выдача пустая (дело сняли с публикации или поехала разметка)"


def _run_msudrf(session: ChromiumSession, status: int | None) -> tuple[str, str]:
    """Портал движка msudrf.ru (тип B): открываем карточку дела по прямой ссылке."""
    html = session.content()
    if CAPTCHA_MARK in html:
        # До забаненного IP портал капчу не доводит — значит, прокси проходит.
        return CAPTCHA, "портал показал проверку — прокси доходит"

    match = CASE_CODE_RE.search(html)
    if match:
        return OK, f"ДЕЛО № {match.group(1).strip()}"
    return ERROR, "открылась не карточка дела"


def _run_spb(session: ChromiumSession, status: int | None) -> tuple[str, str]:
    """Портал Санкт-Петербурга (тип D): карточка дорисовывается фоновой задачей.

    Ждём именно отрисовки: сразу после goto таблицы ещё пустые, и проба на такой
    странице зеленела бы, ничего на самом деле не проверив. Ветки captcha здесь нет —
    капчи на портале не бывает.
    """
    session.page.wait_for_selector(RENDERED_MARK, timeout=RENDER_TIMEOUT)
    uid = find_uid(session.content())
    if uid:
        return OK, f"УИД {uid}"
    return ERROR, "карточка отрисовалась, но УИД на ней нет"


SITE_PROBES: dict[str, Probe] = {
    "mos-sud": Probe(
        name="mos-sud",
        url=SEARCH_URL,
        # Сертификат mos-sud.ru в порядке — ослаблять проверку незачем.
        ignore_https_errors=False,
        run=_run_mos_sud,
    ),
    "msudrf": Probe(
        name="msudrf",
        url=MSUDRF_CASE_URL,
        # У поддоменов движка сертификат не совпадает с именем: без этого флага
        # Chromium не откроет страницу вообще (ERR_CERT_COMMON_NAME_INVALID).
        ignore_https_errors=True,
        run=_run_msudrf,
    ),
    "spb": Probe(
        name="spb",
        url=SPB_CASE_URL,
        # У mirsud.spb.ru сертификат в порядке — ослаблять проверку незачем.
        ignore_https_errors=False,
        run=_run_spb,
    ),
}


def one_line(text: str, limit: int = 70) -> str:
    """Первая строка сообщения, укороченная до limit символов.

    Playwright пишет в исключение весь call log на десятки строк — в таблицу такое
    не влезает и рвёт её на части. Нужен из него ровно первый абзац: там и
    net::ERR_*, и Timeout, то есть всё, по чему отличают отказ прокси от отказа
    портала.
    """
    lines = (text or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    return first if len(first) <= limit else first[: limit - 3] + "..."


def short_error(exc: BaseException) -> str:
    """Короткое имя отказа: тип плюс первая строка сообщения.

    Тип нужен рядом с текстом: у Playwright половина отказов приходит классом Error
    с говорящим net::ERR_*, а половина — TimeoutError с пустым по сути сообщением.
    """
    text = one_line(str(exc))
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def probe_site(
    probe: Probe, proxy: ProxySettings | None = None, headless: bool = True
) -> ProbeResult:
    """Сходить одним прокси на один портал и сказать, дошли ли.

    На каждую пару «прокси × портал» — своя сессия браузера: cookie и состояние релея
    от одного портала не должны влиять на другой, иначе результат зависит от порядка
    проверки.
    """
    started = time.monotonic()
    try:
        with ChromiumSession(
            headless=headless,
            proxy=proxy,
            ignore_https_errors=probe.ignore_https_errors,
        ) as session:
            response = session.goto(probe.url)
            status = response.status if response is not None else None
            if is_retryable_status(status):
                # 403/429/5xx — портал нас видит и не пускает. Дальше идти незачем.
                return ProbeResult(BLOCKED, f"HTTP {status}", time.monotonic() - started)
            verdict, detail = probe.run(session, status)
            return ProbeResult(verdict, detail, time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001 — проба обязана вернуть вердикт, а не упасть
        return ProbeResult(ERROR, short_error(exc), time.monotonic() - started)
