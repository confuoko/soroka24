"""Клиент мировых судов города Москвы (портал mos-sud.ru), страница типа A.

Как достаёт дело: через Chromium заходит на страницу поиска, вводит УИД, жмёт
«Найти», берёт ссылку на карточку дела (с /details/) и открывает её.
Метод parse() пока заглушка — разбор HTML напишем позже (в app/parsers/).
"""
from app.browser import ChromiumSession
from app.courts.base import (
    CaseNotFound,
    CourtClient,
    CourtError,
    FetchFailed,
    PageSnapshot,
)
from app.parsers.registry import get_parser

# Адрес страницы поиска портала мировых судей Москвы.
SEARCH_URL = "https://mos-sud.ru/search"

# Селекторы из разметки страниц (см. html_examples/).
UID_INPUT = 'input[name="uid"]'                # поле «Уникальный идентификатор дела»
SEARCH_BUTTON = "#case-index-search-form-btn"   # кнопка «Найти»
# Ссылка на карточку дела: из detailsLink берём те, что ведут на /details/.
DETAIL_LINK = 'table.custom_table tbody a.detailsLink[href*="/details/"]'


def _capture(session: ChromiumSession, status: int | None) -> PageSnapshot | None:
    """Снять страницу для разбора отказа. Само снятие не должно ронять ничего сверху.

    Браузер в момент отказа может быть уже нездоров (упал контекст, повисла вкладка),
    поэтому любую ошибку снятия глотаем: исходная причина отказа важнее снимка.
    """
    try:
        return PageSnapshot(html=session.content(), url=session.page.url, status=status)
    except Exception:
        return None


class MoscowMirCourtClient(CourtClient):
    """Клиент мировых судов Москвы. Все страницы считаем типом A (по умолчанию)."""

    page_type = "A"

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless

    def fetch_case_html(self, uid: str) -> str:
        """Найти дело по УИД на mos-sud.ru и вернуть HTML карточки дела.

        Если по дороге что-то упало, к исключению прикладывается снимок страницы: снять
        его можно только здесь, пока браузер не закрыт (на выходе из `with` страницы уже
        не будет). Без него отказ выглядит как голый «Page.fill: Timeout» — по нему не
        отличить капчу от блокировки и от поехавшей разметки.
        """
        with ChromiumSession(headless=self._headless) as session:
            status = None
            try:
                # 1. Открываем страницу поиска (Chromium исполнит JS и получит cookie).
                response = session.goto(SEARCH_URL)
                status = response.status if response is not None else None
                # 2. Вводим УИД и запускаем поиск.
                session.fill(UID_INPUT, uid)
                session.submit_and_wait(SEARCH_BUTTON)
                # 3. Берём первую ссылку на карточку дела.
                links = session.page.locator(DETAIL_LINK)
                if links.count() == 0:
                    raise CaseNotFound(uid, page=_capture(session, status))
                # 4. Открываем карточку кликом (в новой вкладке) и забираем её HTML.
                return session.open_in_new_tab(links.first)
            except CourtError:
                # Наши ошибки снимок уже несут (или он не нужен) — пробрасываем как есть.
                raise
            except Exception as exc:
                raise FetchFailed(uid, exc, page=_capture(session, status)) from exc

    def parse(self, html: str) -> dict:
        """Разбор HTML карточки в данные дела — делегируем парсеру по типу страницы."""
        return get_parser(self.page_type).parse(html)
