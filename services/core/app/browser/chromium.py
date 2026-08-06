
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
        ignore_https_errors: bool = False,
    ) -> None:
        self._headless = headless
        self._locale = locale
        # У сайтов мировых судов Московской области сертификат не совпадает с именем
        # поддомена, и без этого флага Chromium вообще не открывает страницу
        # (ERR_CERT_COMMON_NAME_INVALID). По умолчанию выключено: там, где сертификат
        # в порядке, ослаблять проверку незачем.
        self._ignore_https_errors = ignore_https_errors
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
        self._context = self._browser.new_context(
            locale=self._locale, ignore_https_errors=self._ignore_https_errors
        )
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

    def submit_and_wait(self, selector: str, timeout: int = WAIT_TIMEOUT) -> int | None:
        """Кликнуть кнопку и дождаться страницы результатов.

        Форма обычно method=get → происходит переход. Если перехода нет (результат
        отрисован на месте) — просто дожидаемся, пока сеть успокоится.

        Возвращает HTTP-статус перехода (или None, если перехода не было): по нему
        вызывающий код отличает нормальную выдачу от страницы ошибки портала.
        """
        try:
            with self.page.expect_navigation(
                wait_until="networkidle", timeout=timeout
            ) as navigation:
                self.page.click(selector)
            response = navigation.value
            return response.status if response is not None else None
        except PlaywrightTimeoutError:
            self.page.wait_for_load_state("networkidle", timeout=timeout)
            return None

    def open_in_new_tab(self, locator, timeout: int = WAIT_TIMEOUT) -> tuple[str, int | None]:
        """Кликнуть ссылку target=_blank, дождаться новой вкладки и вернуть её HTML.

        Открытие именно кликом важно: браузер сам шлёт Referer/Sec-Fetch, иначе
        сайт отдаёт 403 на «заход из ниоткуда».

        Возвращает пару (HTML, HTTP-статус). Статус приходится ловить слушателем
        заранее: у всплывающей вкладки нет способа спросить ответ её навигации
        постфактум, а без статуса страница ошибки портала неотличима от карточки —
        она так же успешно «открывается», просто содержит не то.
        """
        navigations = []

        def _remember(response) -> None:
            # Отсеиваем картинки и XHR, оставляем только навигации.
            #
            # response.frame здесь трогать НЕЛЬЗЯ: у всплывающей вкладки запрос
            # создаётся раньше фрейма, и обращение к нему бросает исключение прямо
            # внутри обработчика события — Playwright роняет им всю сессию, и падает
            # даже последующий page.content(). Отличить страницу от вложенного фрейма
            # поэтому нечем, зато статус мы всё равно сверяем по адресу вкладки.
            try:
                if response.request.is_navigation_request():
                    navigations.append(response)
            except Exception:  # noqa: BLE001 — обработчик события не имеет права падать
                pass

        self._context.on("response", _remember)
        try:
            with self._context.expect_page(timeout=timeout) as new_page_info:
                locator.click()
            new_page = new_page_info.value
            new_page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            html = new_page.content()
            status = self._navigation_status(navigations, new_page.url)
            new_page.close()
        finally:
            self._context.remove_listener("response", _remember)
        return html, status

    @staticmethod
    def _navigation_status(navigations: list, url: str) -> int | None:
        """Статус ответа, которым открылась вкладка (последний — после всех редиректов).

        Сверяем строго по адресу: в список могли попасть и навигации вложенных фреймов,
        а ошибиться тут дороже, чем не узнать статус. Не нашли — возвращаем None, и
        проверка статуса просто не сработает.
        """
        for response in reversed(navigations):
            try:
                if response.url == url:
                    return response.status
            except Exception:  # noqa: BLE001 — объект ответа мог устареть
                continue
        return None

    def content(self) -> str:
        """HTML текущей страницы (после работы JS)."""
        try:
            return self.page.content()
        except Exception:
            # Страница могла начать очередной переход прямо в момент чтения — портал
            # судов Московской области так делает после отправки формы проверки.
            # Дожидаемся, пока сеть успокоится, и читаем ещё раз.
            self.page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            return self.page.content()
