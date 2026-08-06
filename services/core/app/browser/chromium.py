
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.browser.proxy import ProxySettings
from app.browser.relay import ProxyRelay

# Таймауты по умолчанию (мс): навигация и ожидание элементов.
NAV_TIMEOUT = 60000
WAIT_TIMEOUT = 30000


class ChromiumSession:
    """Сессия Chromium как контекст-менеджер: держит браузер, контекст и страницу.

    Открытая страница доступна как self.page — для гибких сценариев можно работать
    с Playwright напрямую (self.page.locator(...) и т.п.).
    """

    def __init__(
        self,
        headless: bool = True,
        locale: str = "ru-RU",
        proxy: ProxySettings | None = None,
    ) -> None:
        self._headless = headless
        self._locale = locale
        # Прокси, через который ходим (None — напрямую). Chromium не умеет ни SOCKS5
        # с авторизацией, ни CONNECT без Host, поэтому в прокси он ходит не сам, а
        # через локальный релей — его поднимает эта же сессия (см. browser/relay.py).
        self._proxy = proxy
        self._relay = None
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None  # Playwright Page — доступна между __enter__ и __exit__

    def __enter__(self) -> "ChromiumSession":
        # Если идём через прокси — поднимаем релей и отдаём браузеру его адрес.
        proxy_options = None
        if self._proxy is not None:
            self._relay = ProxyRelay(self._proxy).__enter__()
            proxy_options = self._relay.to_playwright()

        # Запускаем Playwright и Chromium; locale ru-RU — как у московского пользователя.
        # Прокси задаём на launch, а не на контекст: каждая сессия и так поднимает свой
        # браузер, а на уровне запуска прокси гарантированно накрывает весь его трафик.
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self._headless, proxy=proxy_options
        )
        self._context = self._browser.new_context(locale=self._locale)
        self.page = self._context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        # Закрываем всё в обратном порядке, даже если внутри была ошибка.
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        # Релей гасим последним: пока браузер жив, он может держать соединения.
        if self._relay is not None:
            self._relay.__exit__(*exc)

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
