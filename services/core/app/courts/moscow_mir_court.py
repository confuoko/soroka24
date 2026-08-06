"""Клиент мировых судов города Москвы (портал mos-sud.ru), страница типа A.

Как достаёт дело: через Chromium заходит на страницу поиска, вводит УИД, жмёт
«Найти», берёт ссылку на карточку дела (с /details/) и открывает её.
Метод parse() пока заглушка — разбор HTML напишем позже (в app/parsers/).
"""
from app.browser import ChromiumSession, ProxySettings
from app.courts.base import (
    CaseNotFound,
    CourtClient,
    CourtError,
    FetchFailed,
    PageSnapshot,
    capture_page,
    check_status,
    is_retryable_status,
)
from app.parsers.registry import get_parser

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

    def __init__(
        self, headless: bool = True, proxy: ProxySettings | None = None
    ) -> None:
        self._headless = headless
        # Прокси, арендованный из пула на этот поход (None — идём напрямую).
        self._proxy = proxy

    def fetch_case_html(self, uid: str) -> str:
        """Найти дело по УИД на mos-sud.ru и вернуть HTML карточки дела.

        Если по дороге что-то упало, к исключению прикладывается снимок страницы: снять
        его можно только здесь, пока браузер не закрыт (на выходе из `with` страницы уже
        не будет). Без него отказ выглядит как голый «Page.fill: Timeout» — по нему не
        отличить капчу от блокировки и от поехавшей разметки.
        """
        with ChromiumSession(headless=self._headless, proxy=self._proxy) as session:
            status = None
            try:
                # 1. Открываем страницу поиска (Chromium исполнит JS и получит cookie).
                response = session.goto(SEARCH_URL)
                status = response.status if response is not None else None
                check_status(session, uid, status, "Страница поиска")
                # 2. Вводим УИД и запускаем поиск.
                session.fill(UID_INPUT, uid)
                results_status = session.submit_and_wait(SEARCH_BUTTON)
                if results_status is not None:
                    status = results_status
                # Проверяем ДО подсчёта ссылок: на странице ошибки их тоже ноль, и без
                # проверки временный отказ портала выглядел бы как «дело не найдено» —
                # то есть окончательный отказ, который никто не повторит.
                check_status(session, uid, status, "Страница результатов")
                # 3. Берём первую ссылку на карточку дела.
                links = session.page.locator(DETAIL_LINK)
                if links.count() == 0:
                    raise CaseNotFound(uid, page=capture_page(session, status))
                # 4. Открываем карточку кликом (в новой вкладке) и забираем её HTML.
                html, card_status = session.open_in_new_tab(links.first)
                if is_retryable_status(card_status):
                    # Вкладка уже закрыта, поэтому снимок собираем из того, что забрали.
                    raise FetchFailed(
                        uid,
                        RuntimeError(f"Карточка дела ответила HTTP {card_status}"),
                        page=PageSnapshot(html=html, status=card_status),
                    )
                return html
            except CourtError:
                # Наши ошибки снимок уже несут (или он не нужен) — пробрасываем как есть.
                raise
            except Exception as exc:
                raise FetchFailed(uid, exc, page=capture_page(session, status)) from exc

    def parse(self, html: str) -> dict:
        """Разбор HTML карточки в данные дела — делегируем парсеру по типу страницы."""
        return get_parser(self.page_type).parse(html)
