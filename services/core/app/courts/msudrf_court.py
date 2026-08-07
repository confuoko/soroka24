"""Клиент мировых судов Московской области (движок msudrf.ru), страница типа B.

Сейчас резолвер отдаёт этому клиенту только Московскую область — поддомены
*.mo.msudrf.ru, это 374 суда (см. MO_DOMAIN ниже). Сам движок общий для 6064 судов из
71 региона (78% мировых судов страны): разметка, капча и адреса карточек у них
одинаковые, различаются только поддомены. Так что класс писан один на все такие порталы
и другой регион подключается одной строкой в COURT_BY_DOMAIN — но подключать его стоит,
только когда разметку этого региона реально посмотрели.

Чем отличается от Москвы. Там пользователь даёт УИД, и клиент ищет дело формой поиска
на mos-sud.ru. Здесь поиска по УИД нет: на вход приходит ПРЯМАЯ ССЫЛКА на карточку,
например
https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=429386415&delo_id=1540005
а УИД, наоборот, извлекается из полученной страницы.

Суд определяется по хосту ссылки: у каждого участка движка свой поддомен, и он сверяется
с хостом Court.base_url. Обратите внимание, что поддомен — это номер участка, и с числом в
коде суда он совпадает не всегда (у 50MS0392 участок № 235, то есть 235.mo.msudrf.ru),
поэтому сопоставление идёт по самому хосту, а не по арифметике над кодом.

Две особенности движка, из-за которых клиент выглядит именно так:

* Сертификат поддоменов не совпадает с именем — без ignore_https_errors Chromium
  вообще не открывает страницу (ERR_CERT_COMMON_NAME_INVALID).
* Вместо карточки портал может отдать страницу проверки с капчей — причём с обычным
  статусом 200. Её разгадываем сервисом распознавания и отправляем форму. Капча
  выпадает не всегда, а иногда прилетает второй раз подряд уже после верного ответа,
  поэтому проверка идёт циклом.

Метод parse() делегирует парсеру типа B — его ещё предстоит написать.
"""
import dataclasses
import logging
import re
from datetime import datetime

from app.browser import ChromiumSession, ProxySettings
from app.captcha import AttemptSink, solve_image
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

# Домен движка целиком: 6064 суда из 71 региона. Сам по себе для резолвера НЕ используется
# — см. MO_DOMAIN ниже.
DOMAIN = "msudrf.ru"

# Домен Московской области — то, что резолвер реально обслуживает. Соответствие точное:
# все 374 суда МО (код 50MS) сидят на *.mo.msudrf.ru, и ни один чужой суд — нет.
# Сузили сознательно: движок у регионов общий, но разметку мы проверяли только на МО,
# и обещать остальные 5690 судов, ни разу их не открыв, нечестно. Чтобы добавить регион,
# достаточно дописать его домен в COURT_BY_DOMAIN (app/courts/resolver.py).
MO_DOMAIN = "mo.msudrf.ru"

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

# Номер дела на карточке движка: в таблице его нет, он стоит только в заголовке —
# <h2>ДЕЛО № 2-1244/2026</h2> (примеры — html_examples/mo_case_*.html).
CASE_CODE_RE = re.compile(r"ДЕЛО\s*№\s*([^<]+)", re.IGNORECASE)


class MsudrfCourtClient(CourtClient):
    """Клиент порталов на движке msudrf.ru. Страницы считаем типом B."""

    page_type = "B"

    def __init__(
        self,
        headless: bool = True,
        proxy: ProxySettings | None = None,
        on_captcha_attempt: AttemptSink | None = None,
    ) -> None:
        self._headless = headless
        # Прокси, арендованный из пула на этот поход (None — идём напрямую).
        self._proxy = proxy
        # Куда сообщать о каждой оплаченной капче. Сам клиент в БД не ходит: он лишь
        # дописывает в запись то, что знает только он (номер проверки за поход и ключ
        # картинки в S3), а хранит расход вызывающий код.
        self._on_captcha_attempt = on_captcha_attempt
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
            answer = self._solve_visible_captcha(session, url, attempt)
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

    def _solve_visible_captcha(
        self, session: ChromiumSession, url: str, attempt_no: int
    ) -> str:
        """Снять картинку капчи со страницы, отложить в S3 и получить разгадку.

        Именно СКРИНШОТ элемента, а не скачивание /captcha.php по адресу: повторный
        запрос к нему сгенерирует НОВУЮ картинку, и разгаданный ответ к показанной на
        странице уже не подойдёт.

        attempt_no — какая это проверка по счёту за поход: в учёте расходов по нему
        видно, что портал показал капчу не один раз.
        """
        png = session.page.locator(CAPTCHA_IMAGE).screenshot()
        stored = save_captcha(url, png, datetime.utcnow())

        def _report(attempt):
            """Дополнить запись тем, что известно только здесь, и отдать в учёт."""
            self._on_captcha_attempt(
                dataclasses.replace(
                    attempt,
                    attempt_no=attempt_no,
                    captcha_key=stored["captcha_key"] if stored else None,
                )
            )

        # Решатель сам решает, о каких исходах сообщать (о таймауте — тоже: деньги за
        # него могли списаться). Если учёт не подключён, ему нечего и передавать.
        solved = solve_image(png, on_attempt=_report if self._on_captcha_attempt else None)
        logger.debug(
            "Капча разгадана (задача %s, стоимость %s): %r",
            solved.task_id, solved.cost, solved.text,
        )
        return solved.text

    def extract_case_code(self, html: str) -> str:
        """Достать номер дела из заголовка карточки: <h2>ДЕЛО № 2-1244/2026</h2>.

        В таблице карточки номера нет — только в заголовке, поэтому берём его оттуда.
        Номер входит в ключ карточки, так что без него дело не сохранить: отсутствие
        заголовка означает, что открылась не карточка (или поехала разметка), и повторять
        поход бессмысленно.
        """
        match = CASE_CODE_RE.search(html)
        code = match.group(1).strip() if match else ""
        if not code:
            raise CaseNotFound("На странице нет номера дела")
        return code

    def parse(self, html: str) -> dict:
        """Разбор HTML карточки в данные дела — делегируем парсеру по типу страницы."""
        return get_parser(self.page_type).parse(html)
