"""Скрипт-помощник: найти дело на mos-sud.ru по УИД или номеру дела/материала.

Портал мировых судов Москвы ищет дела простой GET-формой (поля uid и caseNumber),
поэтому веб-браузер (Selenium) не нужен — достаточно обычного HTTP-запроса, который
выглядит как переход из формы (Referer + Sec-Fetch-заголовки) и не частит (пауза + ретраи).

Запуск (из папки services/core):
    python scripts/search_case.py                          # УИД по умолчанию
    python scripts/search_case.py --uid 77MS0466-01-2026-003751-93
    python scripts/search_case.py --case-number 02-0123/123/2020
    python scripts/search_case.py --uid ... --out my_page.html

Результат сохраняется в services/core/html_examples/<имя_файла>.
"""
import argparse
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Optional

# Значения по умолчанию для запуска без аргументов.
DEFAULT_UID = "77MS0466-01-2026-003751-93"
DEFAULT_OUT = "after_search_page.html"

# Адрес страницы поиска и папка для сохранённых страниц (фикстур парсера).
SEARCH_URL = "https://mos-sud.ru/search"
CORE_ROOT = Path(__file__).resolve().parent.parent
HTML_DIR = CORE_ROOT / "html_examples"

# Пауза (сек) между заходом на страницу и поиском — чтобы не выглядеть как бот.
PAUSE_BEFORE_SEARCH = 3

# Браузерный User-Agent — иначе сайт суда часто отвечает 403.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class MosSudSearch:
    """Мини-клиент поиска дел на портале мировых судов Москвы (mos-sud.ru).

    Держит одну сессию с cookie: сначала заходит на страницу поиска (чтобы получить
    cookie), затем отправляет GET-запрос с параметрами формы и возвращает HTML.
    """

    def __init__(self) -> None:
        # opener с общим хранилищем cookie — ведёт себя как «сессия» браузера.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        # Базовые заголовки как у браузера при обычной навигации по странице.
        self._headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
        }

    def _get(self, url: str, referer: Optional[str] = None, retries: int = 4) -> bytes:
        # Собираем заголовки; Referer ставим, если это «переход» с другой страницы.
        headers = dict(self._headers)
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"  # переход внутри того же сайта
        else:
            headers["Sec-Fetch-Site"] = "none"  # прямой заход (адрес введён вручную)

        request = urllib.request.Request(url, headers=headers)

        delay = PAUSE_BEFORE_SEARCH
        for attempt in range(1, retries + 1):
            try:
                with self._opener.open(request, timeout=30) as response:
                    return response.read()  # читаем байтами, чтобы не портить кодировку
            except urllib.error.HTTPError as error:
                # 429 = сработал троттлинг: ждём (по Retry-After, иначе backoff) и повторяем.
                if error.code == 429 and attempt < retries:
                    retry_after = error.headers.get("Retry-After")
                    wait = int(retry_after) if (retry_after or "").isdigit() else delay
                    print(f"429 Too Many Requests — жду {wait}s, повтор {attempt}/{retries - 1}")
                    time.sleep(wait)
                    delay *= 2  # экспоненциальный backoff
                    continue
                raise

    def open_search_page(self) -> None:
        """Зайти на страницу поиска — так сервер выдаёт cookie сессии."""
        self._get(SEARCH_URL)

    def search(
        self, uid: Optional[str] = None, case_number: Optional[str] = None
    ) -> bytes:
        """Ввести УИД или номер дела/материала в форму и вернуть HTML страницы результата.

        Форма на сайте — method=get, поэтому «ввод и поиск» = GET с параметрами:
        uid — Уникальный идентификатор дела, caseNumber — Номер дела/материала.
        Referer = страница поиска: имитируем переход из формы, а не запрос «из ниоткуда».
        """
        if not uid and not case_number:
            raise ValueError("Нужно передать uid или case_number")

        # formType=shortForm — скрытое поле формы (простая форма поиска).
        params = {"formType": "shortForm"}
        if uid:
            params["uid"] = uid
        if case_number:
            params["caseNumber"] = case_number

        url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
        return self._get(url, referer=SEARCH_URL)


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск дела на mos-sud.ru")
    parser.add_argument("--uid", help="Уникальный идентификатор дела")
    parser.add_argument("--case-number", help="Номер дела или материала")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Имя файла в html_examples/")
    args = parser.parse_args()

    # Если ничего не передали — ищем УИД по умолчанию.
    uid = args.uid
    case_number = args.case_number
    if not uid and not case_number:
        uid = DEFAULT_UID

    client = MosSudSearch()
    client.open_search_page()  # получить cookie сессии
    time.sleep(PAUSE_BEFORE_SEARCH)  # небольшая пауза, чтобы не частить
    html = client.search(uid=uid, case_number=case_number)

    HTML_DIR.mkdir(parents=True, exist_ok=True)
    target = HTML_DIR / args.out
    target.write_bytes(html)
    print(f"Сохранено {target.stat().st_size} байт -> {target}")


if __name__ == "__main__":
    main()
