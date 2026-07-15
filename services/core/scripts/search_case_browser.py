"""Скрипт-помощник: поиск дела на mos-sud.ru через настоящий браузер Chromium (Playwright).

Зачем браузер, а не urllib: на странице поиска работает JavaScript, который ставит
анти-бот cookie. Обычный HTTP-клиент JS не исполняет — и поиск отвечает 429.
Chromium исполняет JS, получает cookie и ищет как живой пользователь.

Что делает скрипт:
    1. открывает страницу поиска и вводит УИД (или номер дела/материала);
    2. жмёт «Найти» и сохраняет страницу результатов;
    3. считает, сколько дел найдено;
    4. заходит в первое найденное дело (ссылка с /details/) и сохраняет его карточку.

Установка (один раз):
    pip install playwright
    playwright install chromium

Запуск (из папки services/core):
    python scripts/search_case_browser.py                       # УИД по умолчанию
    python scripts/search_case_browser.py --uid 77MS0466-01-2026-003751-93
    python scripts/search_case_browser.py --case-number 02-0123/123/2020
    python scripts/search_case_browser.py --show                # видимый браузер (отладка)

Результаты сохраняются в services/core/html_examples/.
"""
import argparse
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Optional

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "Не установлен Playwright. Выполни:\n"
        "    pip install playwright\n"
        "    playwright install chromium"
    )

# Значения по умолчанию для запуска без аргументов.
DEFAULT_UID = "77MS0466-01-2026-003751-93"
DEFAULT_RESULTS_OUT = "after_search_page.html"  # страница результатов поиска
DEFAULT_CASE_OUT = "case_details_page_2.html"      # карточка первого дела

# Адреса и папка для сохранённых страниц (фикстур парсера).
BASE_URL = "https://mos-sud.ru"
SEARCH_URL = BASE_URL + "/search"
CORE_ROOT = Path(__file__).resolve().parent.parent
HTML_DIR = CORE_ROOT / "html_examples"

# Селекторы из разметки страниц (см. main_search_page.html / after_search_page.html).
UID_INPUT = 'input[name="uid"]'                  # поле «Уникальный идентификатор дела»
CASE_NUMBER_INPUT = 'input[name="caseNumber"]'   # поле «Номер дела/материала»
SEARCH_BUTTON = "#case-index-search-form-btn"     # кнопка «Найти»
RESULT_TEXT = ".resultsearch_text"                # блок «найдено записей: N»
# Ссылка на карточку дела — из всех detailsLink берём те, что ведут на /details/.
DETAIL_LINK = 'table.custom_table tbody a.detailsLink[href*="/details/"]'


def _extract_count(page) -> int:
    """Вытащить число найденных дел из текста «найдено записей: N» (0, если нет)."""
    node = page.locator(RESULT_TEXT)
    if node.count() == 0:
        return 0
    match = re.search(r"найдено записей:\s*(\d+)", node.first.inner_text())
    return int(match.group(1)) if match else 0


def run(
    uid: Optional[str] = None,
    case_number: Optional[str] = None,
    headless: bool = True,
) -> dict:
    """Выполнить поиск и (если есть результат) открыть первое дело.

    Возвращает словарь: count, found_rows, case_number, results_html, case_html.
    """
    if not uid and not case_number:
        raise ValueError("Нужно передать uid или case_number")

    with sync_playwright() as playwright:
        # Запускаем Chromium; locale ru-RU — как у московского пользователя.
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(locale="ru-RU")
        page = context.new_page()

        # 1. Заходим на страницу поиска и ждём, пока отработает JS.
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)

        # 2. «Вводим» значение в нужное поле формы.
        if uid:
            page.fill(UID_INPUT, uid)
        if case_number:
            page.fill(CASE_NUMBER_INPUT, case_number)

        # 3. Жмём «Найти» и ждём страницу результатов (форма method=get → переход).
        try:
            with page.expect_navigation(wait_until="networkidle", timeout=30000):
                page.click(SEARCH_BUTTON)
        except PlaywrightTimeoutError:
            page.wait_for_load_state("networkidle", timeout=30000)

        results_html = page.content()

        # 4. Сколько дел нашлось. Таблица на странице дублируется (есть скрытая
        #    «развёрнутая» версия), поэтому одни и те же ссылки убираем в уникальные.
        count = _extract_count(page)
        links = page.locator(DETAIL_LINK)
        all_hrefs = links.evaluate_all("els => els.map(e => e.getAttribute('href'))")
        unique_hrefs = list(dict.fromkeys(h for h in all_hrefs if h))
        found = len(unique_hrefs)

        # 5. Если что-то нашли — заходим в ПЕРВОЕ дело и забираем его карточку.
        #    Открываем именно КЛИКОМ: ссылка target="_blank", и на прямой goto сайт
        #    отдаёт 403 — нужен настоящий переход (браузер сам шлёт Referer/Sec-Fetch).
        case_number_text: Optional[str] = None
        case_html: Optional[str] = None
        if found > 0:
            case_number_text = links.first.inner_text().strip()
            try:
                with context.expect_page(timeout=30000) as new_page_info:
                    links.first.click()
                case_page = new_page_info.value  # открывшаяся новая вкладка
                case_page.wait_for_load_state("networkidle", timeout=60000)
                case_html = case_page.content()
                case_page.close()
            except PlaywrightTimeoutError:
                # Запасной путь: прямой переход, но с Referer = страница результатов.
                case_url = urllib.parse.urljoin(BASE_URL, unique_hrefs[0])
                page.goto(case_url, wait_until="networkidle", referer=page.url, timeout=60000)
                case_html = page.content()

        browser.close()
        return {
            "count": count,
            "found": found,
            "case_number": case_number_text,
            "results_html": results_html,
            "case_html": case_html,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск дела на mos-sud.ru через Chromium")
    parser.add_argument("--uid", help="Уникальный идентификатор дела")
    parser.add_argument("--case-number", help="Номер дела или материала")
    parser.add_argument("--out", default=DEFAULT_RESULTS_OUT, help="Имя файла страницы результатов")
    parser.add_argument("--case-out", default=DEFAULT_CASE_OUT, help="Имя файла карточки дела")
    parser.add_argument(
        "--show", action="store_true", help="Показать браузер (по умолчанию headless)"
    )
    args = parser.parse_args()

    # Если ничего не передали — ищем УИД по умолчанию.
    uid = args.uid
    case_number = args.case_number
    if not uid and not case_number:
        uid = DEFAULT_UID

    result = run(uid=uid, case_number=case_number, headless=not args.show)

    HTML_DIR.mkdir(parents=True, exist_ok=True)

    # Сохраняем страницу результатов.
    results_path = HTML_DIR / args.out
    results_path.write_text(result["results_html"], encoding="utf-8")
    print(f"Найдено дел: {result['count']} (уникальных карточек: {result['found']})")
    print(f"Результаты -> {results_path} ({len(result['results_html'])} символов)")

    # Сохраняем карточку первого дела, если оно есть.
    if result["case_html"] is not None:
        case_path = HTML_DIR / args.case_out
        case_path.write_text(result["case_html"], encoding="utf-8")
        print(f"Первое дело: {result['case_number']}")
        print(f"Карточка дела -> {case_path} ({len(result['case_html'])} символов)")
    else:
        print("Дел не найдено — карточку не сохраняю.")


if __name__ == "__main__":
    main()
