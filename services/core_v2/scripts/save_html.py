"""Скрипт-помощник: скачать HTML-страницу и сохранить её в core/html_examples/.

Запуск (из папки services/core или откуда угодно):
    python scripts/save_html.py                     # url и имя файла по умолчанию
    python scripts/save_html.py <url> <имя_файла>   # свои значения

Файлы кладутся в services/core/html_examples/ — это фикстуры для отладки парсеров.
"""
import sys
import urllib.request
from pathlib import Path

# Значения по умолчанию — сохранить главную страницу поиска mos-sud.ru.
DEFAULT_URL = "https://sudrf.ru/index.php?id=300&act=go_ms_search&searchtype=ms&var=true&ms_type=ms&court_subj=0"
DEFAULT_FILENAME = "mir_court_list_full.html"

# Корень сервиса core (на уровень выше папки scripts) и папка для фикстур.
CORE_ROOT = Path(__file__).resolve().parent.parent
HTML_DIR = CORE_ROOT / "html_examples"

# Браузерный User-Agent — иначе сайт суда часто отвечает 403 на дефолтный urllib.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def save_html(url: str, filename: str) -> Path:
    """Скачать страницу по url и сохранить её байты в html_examples/<filename>."""
    HTML_DIR.mkdir(parents=True, exist_ok=True)  # создать папку, если её нет
    target = HTML_DIR / filename

    # Запрос с заголовком User-Agent и таймаутом на случай зависания.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()  # читаем как байты, чтобы не портить кодировку

    target.write_bytes(content)
    return target


def main() -> None:
    # Аргументы командной строки необязательны: [url] [имя_файла].
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    filename = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_FILENAME

    saved = save_html(url, filename)
    print(f"Сохранено {saved.stat().st_size} байт -> {saved}")


if __name__ == "__main__":
    main()
