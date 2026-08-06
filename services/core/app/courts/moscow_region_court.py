"""Клиент мировых судов Московской области (порталы *.mo.msudrf.ru), страница типа B.

Чем отличается от Москвы. Там пользователь даёт УИД, и клиент сам ищет дело формой
поиска. Здесь на вход приходит ПРЯМАЯ ССЫЛКА на карточку, например
https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=429386415&delo_id=1540005
а УИД мы, наоборот, извлекаем из полученной страницы (extract_uid) — и уже по нему
привязываем суд. Выводить суд из ссылки нельзя: поддомен — это номер судебного участка
из названия, и с номером в коде суда он совпадает не всегда (у 50MS0392 участок № 235,
то есть 235.mo.msudrf.ru), а в справочнике вдобавок есть битые адреса.

Две особенности портала, из-за которых клиент выглядит именно так:

* Сертификат поддоменов не совпадает с именем — без ignore_https_errors Chromium
  вообще не открывает страницу (ERR_CERT_COMMON_NAME_INVALID).
* Вместо карточки портал может отдать страницу проверки с капчей — причём с обычным
  статусом 200. Её разгадываем сервисом распознавания и отправляем форму. Капча
  выпадает не всегда, а иногда прилетает второй раз подряд уже после верного ответа,
  поэтому проверка идёт циклом.

Метод parse() делегирует парсеру типа B — его ещё предстоит написать.
"""
import logging
import re
from datetime import datetime

from app.browser import ChromiumSession, ProxySettings
from app.captcha import solve_image
from app.config import CAPTCHA_ATTEMPTS
from app.courts.base import (
    CaseNotFound,
    CourtClient,
    CourtError,
    FetchFailed,
    capture_page,
    check_status,
)
from app.parsers.registry import get_parser
from app.storage import save_captcha

logger = logging.getLogger(__name__)

# Признак страницы проверки. Ищем по заголовку, а не по слову «captcha»: оно есть и в
# коде обычных страниц портала.
CAPTCHA_MARK = "Для продолжения необходимо пройти дополнительную проверку"

# Разметка формы проверки (пример — html_examples/mo_captcha_form.html).
# У формы нет action, поэтому она уходит POST-ом на тот же адрес дела.
CAPTCHA_IMAGE = "#kcaptchaForm img"
CAPTCHA_INPUT = 'input[name="captcha-response"]'
CAPTCHA_SUBMIT = "#kcaptchaForm button[type='submit']"

# УИД дела: 50MS0095-01-2026-002990-16.
UID_RE = re.compile(r"\b\d{2}[A-Z]{2}\d{4}-\d{2}-\d{4}-\d{6}-\d{2}\b")


class MoscowRegionCourtClient(CourtClient):
    """Клиент мировых судов Московской области. Страницы считаем типом B."""

    page_type = "B"

    def __init__(
        self, headless: bool = True, proxy: ProxySettings | None = None
    ) -> None:
        self._headless = headless
        # Прокси, арендованный из пула на этот поход (None — идём напрямую).
        self._proxy = proxy
        # Сколько капч пришлось разгадать за последний поход — для отчёта скрипта.
        self.captchas_solved = 0

    def fetch_case_html(self, url: str) -> str:
        """Пройти по ссылке на карточку дела и вернуть её HTML.

        На вход именно ссылка, а не УИД: у портала области нет поиска по УИД, зато
        карточка доступна по прямому адресу.
        """
        self.captchas_solved = 0
        with ChromiumSession(
            headless=self._headless, proxy=self._proxy, ignore_https_errors=True
        ) as session:
            status = None
            try:
                response = session.goto(url)
                status = response.status if response is not None else None
                check_status(session, url, status, "Страница дела")

                html = self._pass_captcha(session, url, status)
                return html
            except CourtError:
                # Наши ошибки снимок уже несут (или он не нужен) — пробрасываем как есть.
                raise
            except Exception as exc:
                raise FetchFailed(url, exc, page=capture_page(session, status)) from exc

    def _pass_captcha(self, session: ChromiumSession, url: str, status: int | None) -> str:
        """Пройти проверку, если её показали, и вернуть HTML страницы дела.

        Всё происходит в одной сессии браузера: cookie, выданные вместе с капчей,
        должны уехать обратно вместе с ответом, иначе проверка начнётся заново.
        """
        html = session.content()
        for attempt in range(1, CAPTCHA_ATTEMPTS + 1):
            if CAPTCHA_MARK not in html:
                return html  # проверки нет или она уже пройдена

            logger.info("Портал просит пройти проверку (попытка %d): %s", attempt, url)
            answer = self._solve_visible_captcha(session, url)
            session.page.fill(CAPTCHA_INPUT, answer)
            # Форма без action — POST уходит на тот же адрес дела.
            session.submit_and_wait(CAPTCHA_SUBMIT)
            self.captchas_solved += 1
            html = session.content()

        # Попытки кончились, а страница всё ещё просит проверку. Ошибка временная:
        # задача поретраится и на следующем заходе возьмёт другой прокси.
        raise FetchFailed(
            url,
            RuntimeError(f"Не удалось пройти проверку за {CAPTCHA_ATTEMPTS} попыток"),
            page=capture_page(session, status),
        )

    @staticmethod
    def _solve_visible_captcha(session: ChromiumSession, url: str) -> str:
        """Снять картинку капчи со страницы, отложить в S3 и получить разгадку.

        Именно СКРИНШОТ элемента, а не скачивание /captcha.php по адресу: повторный
        запрос к нему сгенерирует НОВУЮ картинку, и разгаданный ответ к показанной на
        странице уже не подойдёт.
        """
        png = session.page.locator(CAPTCHA_IMAGE).screenshot()
        save_captcha(url, png, datetime.utcnow())
        answer, task_id = solve_image(png)
        logger.debug("Капча разгадана (задача %s): %r", task_id, answer)
        return answer

    def extract_uid(self, html: str) -> str:
        """Достать УИД дела со страницы карточки.

        По нему потом резолвится суд (первые 8 символов — его код в справочнике).
        """
        found = UID_RE.search(html)
        if found is None:
            # Страница есть, но это не карточка: дело сняли с публикации или поехала
            # разметка. Повторять бессмысленно — отказ окончательный.
            raise CaseNotFound("На странице нет уникального идентификатора дела")
        return found.group(0)

    def parse(self, html: str) -> dict:
        """Разбор HTML карточки в данные дела — делегируем парсеру по типу страницы."""
        return get_parser(self.page_type).parse(html)
