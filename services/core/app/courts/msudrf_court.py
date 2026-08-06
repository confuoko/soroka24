"""Клиент порталов мировых судов на движке msudrf.ru, страница типа B.

Один класс на все такие порталы, а не на регион: движок общий для 6063 судов из 72
регионов (78% мировых судов страны) — разметка, капча и адреса карточек у них
одинаковые, различаются только поддомены. Московская область (50MS) — просто первый
регион, который через него пошёл. Появится регион с другой разметкой — тогда и
разделим; заводить пустые классы-наследники заранее незачем.

Чем отличается от Москвы. Там пользователь даёт УИД, и клиент ищет дело формой поиска
на mos-sud.ru. Здесь поиска по УИД нет: на вход приходит ПРЯМАЯ ССЫЛКА на карточку,
например
https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=429386415&delo_id=1540005
а УИД, наоборот, извлекается из полученной страницы — и уже по нему привязывается суд.
Выводить суд из ссылки нельзя: поддомен — это номер судебного участка, и с номером в
коде суда он совпадает не всегда (у 50MS0392 участок № 235, то есть 235.mo.msudrf.ru).

Две особенности движка, из-за которых клиент выглядит именно так:

* Сертификат поддоменов не совпадает с именем — без ignore_https_errors Chromium
  вообще не открывает страницу (ERR_CERT_COMMON_NAME_INVALID).
* Вместо карточки портал может отдать страницу проверки с капчей — причём с обычным
  статусом 200. Её разгадываем сервисом распознавания и отправляем форму. Капча
  выпадает не всегда, а иногда прилетает второй раз подряд уже после верного ответа,
  поэтому проверка идёт циклом.

Метод parse() делегирует парсеру типа B — его ещё предстоит написать.
"""
import logging
from datetime import datetime

from app.browser import ChromiumSession, ProxySettings
from app.captcha import solve_image
from app.config import CAPTCHA_ATTEMPTS
from app.courts.base import (
    CourtClient,
    CourtError,
    FetchFailed,
    capture_page,
    check_status,
)
from app.parsers.registry import get_parser
from app.storage import save_captcha

logger = logging.getLogger(__name__)

# Домен движка: по нему резолвер понимает, что ссылку обслуживает этот клиент.
DOMAIN = "msudrf.ru"

# Как выглядит адрес карточки дела. Показываем пользователю, когда его дело по УИД
# не ищется: у всех порталов движка адрес одинаковый, меняется только поддомен.
CASE_URL_EXAMPLE = (
    "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs"
    "&case_id=429386415&delo_id=1540005"
)

# Признак страницы проверки. Ищем по заголовку, а не по слову «captcha»: оно есть и в
# коде обычных страниц портала.
CAPTCHA_MARK = "Для продолжения необходимо пройти дополнительную проверку"

# Разметка формы проверки (пример — html_examples/mo_captcha_form.html).
# У формы нет action, поэтому она уходит POST-ом на тот же адрес дела.
CAPTCHA_IMAGE = "#kcaptchaForm img"
CAPTCHA_INPUT = 'input[name="captcha-response"]'
CAPTCHA_SUBMIT = "#kcaptchaForm button[type='submit']"


class MsudrfCourtClient(CourtClient):
    """Клиент порталов на движке msudrf.ru. Страницы считаем типом B."""

    page_type = "B"

    def __init__(
        self, headless: bool = True, proxy: ProxySettings | None = None
    ) -> None:
        self._headless = headless
        # Прокси, арендованный из пула на этот поход (None — идём напрямую).
        self._proxy = proxy
        # Сколько капч пришлось разгадать за последний поход — для отчёта скрипта.
        self.captchas_solved = 0

    def fetch_case_html_by_url(self, url: str) -> str:
        """Пройти по ссылке на карточку дела и вернуть её HTML.

        Ссылка — постоянный адрес дела: по ней ходим и в первый раз, и при каждом
        повторном обходе. Отдельной ветки «взять сохранённую страницу из S3» нет —
        свежая разметка нужна ровно затем, чтобы увидеть изменения.
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
                return self._pass_captcha(session, url, status)
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

    def parse(self, html: str) -> dict:
        """Разбор HTML карточки в данные дела — делегируем парсеру по типу страницы."""
        return get_parser(self.page_type).parse(html)
