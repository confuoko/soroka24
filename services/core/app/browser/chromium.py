
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# Таймауты по умолчанию (мс): навигация и ожидание элементов.
NAV_TIMEOUT = 60000
WAIT_TIMEOUT = 30000


class ChromiumSession:
    """Сессия Chromium как контекст-менеджер: держит браузер, контекст и страницу.

    Открытая страница доступна как self.page — для гибких сценариев можно работать
    с Playwright напрямую (self.page.locator(...) и т.п.).
    """

    def __init__(self, headless: bool = True, locale: str = "ru-RU") -> None:
        self._headless = headless
        self._locale = locale
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None  # Playwright Page — доступна между __enter__ и __exit__

    def __enter__(self) -> "ChromiumSession":
        # Запускаем Playwright и Chromium; locale ru-RU — как у московского пользователя.
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(locale=self._locale)
        self.page = self._context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        # Закрываем всё в обратном порядке, даже если внутри была ошибка.
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()

    def goto(self, url: str, timeout: int = NAV_TIMEOUT):
        """Открыть URL и дождаться, пока сеть успокоится (отработает JS).

        Возвращает Response навигации (или None, если её не было — например переход
        внутри SPA). Нужен ради HTTP-статуса: по нему видно 403/429, даже когда тело
        похоже на обычную страницу.
        """
        return self.page.goto(url, wait_until="networkidle", timeout=timeout)

    def fill(self, selector: str, value: str) -> None:
        """Ввести значение в поле формы."""
        self.page.fill(selector, value)

    def submit_and_wait(self, selector: str, timeout: int = WAIT_TIMEOUT) -> None:
        """Кликнуть кнопку и дождаться страницы результатов.

        Форма обычно method=get → происходит переход. Если перехода нет (результат
        отрисован на месте) — просто дожидаемся, пока сеть успокоится.
        """
        try:
            with self.page.expect_navigation(wait_until="networkidle", timeout=timeout):
                self.page.click(selector)
        except PlaywrightTimeoutError:
            self.page.wait_for_load_state("networkidle", timeout=timeout)

    def open_in_new_tab(self, locator, timeout: int = WAIT_TIMEOUT) -> str:
        """Кликнуть ссылку target=_blank, дождаться новой вкладки и вернуть её HTML.

        Открытие именно кликом важно: браузер сам шлёт Referer/Sec-Fetch, иначе
        сайт отдаёт 403 на «заход из ниоткуда».
        """
        with self._context.expect_page(timeout=timeout) as new_page_info:
            locator.click()
        new_page = new_page_info.value
        new_page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
        html = new_page.content()
        new_page.close()
        return html

    def content(self) -> str:
        """HTML текущей страницы (после работы JS)."""
        return self.page.content()
