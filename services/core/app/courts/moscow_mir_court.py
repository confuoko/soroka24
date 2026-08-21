"""Клиент мировых судов города Москвы (портал mos-sud.ru), страница типа A.

Как достаёт дела: через Chromium заходит на страницу поиска, вводит УИД, жмёт «Найти» и
обходит ВСЕ строки таблицы результатов, открывая карточку каждой.

Строк может быть несколько, потому что УИД сквозной: по нему на портале видны и приказное
производство, и последовавшее исковое, причём иногда в разных участках. Из каждой строки
до открытия карточки забираем номер дела (первый столбец) и номер участка (первый сегмент
пути в ссылке) — и то, и другое входит в ключ карточки, а на самой странице номер приходит
под разными метками, суда же там нет вовсе.
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
    FetchedCard,
    FetchFailed,
    PageSnapshot,
    capture_page,
    check_status,
    is_retryable_status,
)
from app.parsers.registry import get_parser

logger = logging.getLogger(__name__)

# Адрес страницы поиска портала мировых судей Москвы.
SEARCH_URL = "https://mos-sud.ru/search"

# Селекторы из разметки страниц (см. html_examples/).
UID_INPUT = 'input[name="uid"]'                # поле «Уникальный идентификатор дела»
SEARCH_BUTTON = "#case-index-search-form-btn"   # кнопка «Найти»
# Ссылка на карточку дела: из detailsLink берём те, что ведут на /details/.
# Обязательно внутри .wrapper-search-tables: рядом на странице лежит скрытая копия той же
# таблицы (<div id="modalTable" style="display:none">), и без этого ограничения каждая
# строка результатов находилась бы дважды — то есть каждое дело качалось бы по два раза.
DETAIL_LINK = (
    'div.wrapper-search-tables table.custom_table tbody a.detailsLink[href*="/details/"]'
)

# Номер участка — первый сегмент пути в ссылке на карточку: /463/cases/claim-civil/...
PARTICIPOK_IN_HREF = re.compile(r"^/(\d+)/")


def participok_from_href(href: str) -> int | None:
    """Номер судебного участка из ссылки на карточку (или None, если его там нет)."""
    match = PARTICIPOK_IN_HREF.match(urlsplit(href or "").path)
    return int(match.group(1)) if match else None


class MoscowMirCourtClient(CourtClient):
    """Клиент мировых судов Москвы. Все страницы считаем типом A (по умолчанию)."""

    page_type = "A"
    portal = "mos-sud"

    def __init__(
        self,
        headless: bool = True,
        proxy: ProxySettings | None = None,
        on_captcha_attempt: AttemptSink | None = None,
    ) -> None:
        self._headless = headless
        # Прокси, арендованный из пула на этот поход (None — идём напрямую).
        self._proxy = proxy
        # on_captcha_attempt принимаем и не используем: на mos-sud.ru капчи нет, а
        # сигнатура конструктора у всех клиентов судов общая (её задаёт резолвер).

    def fetch_cases_by_uid(self, uid: str) -> list[FetchedCard]:
        """Найти по УИД все дела на mos-sud.ru и вернуть их карточки.

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

                links = session.page.locator(DETAIL_LINK)
                found = links.count()
                if found == 0:
                    raise CaseNotFound(uid, page=capture_page(session, status))

                # 3. Обходим все строки таблицы. Локатор перебираем по индексу, а не через
                #    .first: страница поиска остаётся открытой, закрывается только вкладка
                #    карточки, поэтому ссылки живы до конца обхода.
                cards: list[FetchedCard] = []
                skipped: list[str] = []
                for index in range(found):
                    link = links.nth(index)
                    code = (link.inner_text() or "").strip()
                    participok_no = participok_from_href(link.get_attribute("href") or "")

                    # Номер дела и участок — часть ключа карточки, без них сохранять нечего.
                    # Роняем строку, а не весь заход: остальные дела этого УИД ни при чём.
                    if not code or participok_no is None:
                        skipped.append(f"строка {index + 1}: номер={code!r}, участок={participok_no}")
                        continue

                    html, card_status = session.open_in_new_tab(link)
                    if is_retryable_status(card_status):
                        # Вкладка уже закрыта, снимок собираем из того, что забрали.
                        raise FetchFailed(
                            uid,
                            RuntimeError(f"Карточка дела ответила HTTP {card_status}"),
                            page=PageSnapshot(html=html, status=card_status),
                        )
                    cards.append(
                        FetchedCard(code=code, html=html, participok_no=participok_no)
                    )

                if skipped:
                    logger.warning(
                        "Дело %s: пропущены строки таблицы без номера или участка (%s)",
                        uid,
                        "; ".join(skipped),
                    )
                if not cards:
                    # Строки были, но ни из одной не вышло карточки — разметка таблицы
                    # изменилась. Повторять бессмысленно.
                    raise CaseNotFound(
                        f"{uid}: в таблице результатов {found} строк, но ни в одной нет "
                        f"номера дела и участка",
                        page=capture_page(session, status),
                    )
                return cards
            except CourtError:
                # Наши ошибки снимок уже несут (или он не нужен) — пробрасываем как есть.
                raise
            except Exception as exc:
                raise FetchFailed(uid, exc, page=capture_page(session, status)) from exc

    def parse(self, html: str) -> dict:
        """Разбор HTML карточки в данные дела — делегируем парсеру по типу страницы."""
        return get_parser(self.page_type).parse(html)
