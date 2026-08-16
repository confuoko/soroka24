"""Клиент мировых судов Санкт-Петербурга (портал mirsud.spb.ru), страница типа D.

Третий движок в проекте, и от двух предыдущих он отличается в двух местах.

Первое: КАРТОЧКА РИСУЕТСЯ АСИНХРОННО. Страница приходит пустой, а данные подтягивает
Angular: дёргает /cases/api/detail/?id=<номер дела>&court_site_id=<участок>, получает
идентификатор фоновой задачи и раз в пять секунд опрашивает /cases/api/results/, пока
та не ответит finished. Поэтому одного goto() мало — networkidle наступает ЗАДОЛГО до
того, как в таблицах появятся данные, и снимок уезжает пустой. Ждём появления разметки
карточки (RENDERED_MARK). Сходить в этот API в обход страницы нельзя: на прямой запрос
портал отвечает 403, ему нужны заголовки внутристраничного XHR.

Второе: ОДИН ХОСТ НА ВСЕ 211 СУДОВ РЕГИОНА. У msudrf.ru суд определяется по поддомену,
здесь поддоменов нет — номер участка стоит в пути ссылки:
https://mirsud.spb.ru/cases/detail/98/?id=2-2983%2F2026-98
Отсюда participok_from_url() и REGION_CODE: по ним CourtRepository.get_by_url находит
суд, потому что get_by_host на общем хосте (и правильно) отказывается выбирать.

Важно, что в пути стоит именно номер УЧАСТКА, а не число из кода суда: у участка № 126
код 78MS0124, а 78MS0126 — это участок № 128. Никакой арифметики, только поиск по
названию (CourtRepository.get_by_participok разбирает номер оттуда).

Приятное отличие от msudrf.ru: КАПЧИ ЗДЕСЬ НЕТ ни в каком виде, и сертификат у портала
в порядке — ignore_https_errors не нужен.

Про справочник, чтобы не выяснять заново: у шести судов Петербурга в courts.json битый
base_url — у 78MS0212/213/214 стоит /court-sites/212,213,214 вместо их участков
146/147/7, а у 78MS0215/216/217 вместо адреса участка стоит голый http://mirsud.spb.ru.
На определение суда это не влияет: номер участка берётся из НАЗВАНИЯ, а не из адреса.
Чинить справочник — отдельная задача, сознательно не трогаем.

Разбор карточки — app/parsers/spb_type_d.py. Там же расписаны грабли разметки, главные
из которых — два представления одних и тех же данных на одной странице.
"""
import logging
import re
from urllib.parse import urlsplit

from app.browser import ChromiumSession, ProxySettings
from app.captcha import AttemptSink
from app.courts.base import (
    CaseNotFound,
    CourtClient,
    CourtError,
    FetchFailed,
    capture_page,
    check_status,
)
from app.parsers.registry import get_parser

logger = logging.getLogger(__name__)

# Единственный хост региона: 211 мировых судов Санкт-Петербурга, код 78MS.
DOMAIN = "mirsud.spb.ru"

# Префикс кода судов региона — для поиска по номеру участка.
REGION_CODE = "78MS"

# Номер судебного участка в пути ссылки: /cases/detail/98/?id=2-2983%2F2026-98.
# Это НОМЕР УЧАСТКА, а не число из кода суда — см. докстринг модуля.
CASE_PATH_RE = re.compile(r"^/cases/detail/(\d+)/")

# Подпись поля в карточке. Появляется только после того, как отработает фоновая задача
# портала, поэтому годится как признак «карточка отрисована».
RENDERED_MARK = "b.table-title"

# Сколько ждать отрисовки (мс). Портал опрашивает свою задачу раз в 5 секунд, так что
# ожидание здесь заведомо длиннее обычного WAIT_TIMEOUT.
RENDER_TIMEOUT = 90000

# Номер дела в заголовке страницы и в хлебных крошках: «Судебное дело №2-2983/2026-98».
# Обрываемся и на «<», и на «|»: в <title> за номером идёт « | Мировые судьи…».
CASE_CODE_RE = re.compile(r"Судебное дело\s*№\s*([^<|]+)")


def participok_from_url(url: str) -> int | None:
    """Номер судебного участка из ссылки на карточку (или None, если его там нет).

    Нужен, потому что по хосту суд Петербурга не определить: он общий на весь регион.
    """
    match = CASE_PATH_RE.match(urlsplit(url or "").path)
    return int(match.group(1)) if match else None


class SpbMirCourtClient(CourtClient):
    """Клиент мировых судов Санкт-Петербурга. Страницы считаем типом D."""

    page_type = "D"

    def __init__(
        self,
        headless: bool = True,
        proxy: ProxySettings | None = None,
        on_captcha_attempt: AttemptSink | None = None,
    ) -> None:
        self._headless = headless
        # Прокси, арендованный из пула на этот поход (None — идём напрямую).
        self._proxy = proxy
        # on_captcha_attempt принимаем и не используем: на mirsud.spb.ru капчи нет, а
        # сигнатура конструктора у всех клиентов судов общая (её задаёт резолвер).

    def fetch_case_html_by_url(self, url: str) -> str:
        """Открыть карточку дела по прямой ссылке и вернуть её HTML.

        Ссылка — постоянный адрес дела: по ней ходим и в первый раз, и при каждом
        повторном обходе.
        """
        with ChromiumSession(headless=self._headless, proxy=self._proxy) as session:
            status = None
            try:
                response = session.goto(url)
                status = response.status if response is not None else None
                # Статус проверяем ДО ожидания разметки: иначе на странице отказа мы
                # честно прождали бы полторы минуты и вернули «Timeout» вместо HTTP 403.
                check_status(session, url, status, "Страница дела")

                # Ждём, пока портал дорисует карточку данными своей фоновой задачи.
                session.page.wait_for_selector(RENDERED_MARK, timeout=RENDER_TIMEOUT)
                return session.content()
            except CourtError:
                # Наши ошибки снимок уже несут (или он не нужен) — пробрасываем как есть.
                raise
            except Exception as exc:
                # Сюда попадает и таймаут ожидания отрисовки. Это ВРЕМЕННЫЙ отказ:
                # задача поретраится и на следующем заходе возьмёт другой прокси.
                raise FetchFailed(url, exc, page=capture_page(session, status)) from exc

    def extract_case_code(self, html: str) -> str:
        """Достать номер дела из заголовка страницы: «Судебное дело №2-2983/2026-98».

        Номер входит в ключ карточки и в имя папки снимка в S3, так что без него дело
        не сохранить. Отсутствие заголовка означает, что открылась не карточка, —
        повторять поход бессмысленно.
        """
        match = CASE_CODE_RE.search(html)
        code = match.group(1).strip() if match else ""
        if not code:
            raise CaseNotFound("На странице нет номера дела")
        return code

    def parse(self, html: str) -> dict:
        """Разбор HTML карточки в данные дела — делегируем парсеру по типу страницы."""
        return get_parser(self.page_type).parse(html)
