"""Клиент мировых судов города Москвы (портал mos-sud.ru), страница типа A.

Как достаёт дело: через Chromium заходит на страницу поиска, вводит УИД, жмёт
«Найти», берёт ссылку на карточку дела (с /details/) и открывает её.
Метод parse() пока заглушка — разбор HTML напишем позже (в app/parsers/).
"""
from app.browser import ChromiumSession
from app.courts.base import CaseNotFound, CourtClient

# Адрес страницы поиска портала мировых судей Москвы.
SEARCH_URL = "https://mos-sud.ru/search"

# Селекторы из разметки страниц (см. html_examples/).
UID_INPUT = 'input[name="uid"]'                # поле «Уникальный идентификатор дела»
SEARCH_BUTTON = "#case-index-search-form-btn"   # кнопка «Найти»
# Ссылка на карточку дела: из detailsLink берём те, что ведут на /details/.
DETAIL_LINK = 'table.custom_table tbody a.detailsLink[href*="/details/"]'


class MoscowMirCourtClient(CourtClient):
    """Клиент мировых судов Москвы. Все страницы считаем типом A (по умолчанию)."""

    page_type = "A"

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless

    def fetch_case_html(self, uid: str) -> str:
        """Найти дело по УИД на mos-sud.ru и вернуть HTML карточки дела."""
        with ChromiumSession(headless=self._headless) as session:
            # 1. Открываем страницу поиска (Chromium исполнит JS и получит cookie).
            session.goto(SEARCH_URL)
            # 2. Вводим УИД и запускаем поиск.
            session.fill(UID_INPUT, uid)
            session.submit_and_wait(SEARCH_BUTTON)
            # 3. Берём первую ссылку на карточку дела.
            links = session.page.locator(DETAIL_LINK)
            if links.count() == 0:
                raise CaseNotFound(uid)
            # 4. Открываем карточку кликом (в новой вкладке) и забираем её HTML.
            return session.open_in_new_tab(links.first)

    def parse(self, html: str) -> dict:
        """Разбор HTML карточки в данные дела. ЗАГЛУШКА — напишем позже."""
        # TODO: парсер страницы типа A (стороны, судья, события, документы, ...).
        return {}
