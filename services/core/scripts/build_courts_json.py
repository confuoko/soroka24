"""Скрипт-помощник: собрать JSON-справочник судов из сохранённой HTML-страницы sudrf.ru.

Разбирает фикстуру со списком судов (по умолчанию — мировые из mir_court_list_full.html)
и пишет services/core/data/courts.json. Дальше этот JSON заливается в БД скриптом sync_courts.py.

Запуск (из папки services/core или откуда угодно):
    python scripts/build_courts_json.py
    python scripts/build_courts_json.py --src html_examples/mir_court_list_full.html \
        --out data/courts.json --level mirsud

Зависимостей нет — только стандартная библиотека (re / html / json), как и в остальных скриптах.
"""
import argparse
import html
import json
import re
from pathlib import Path

# Корень сервиса core (на уровень выше папки scripts) и пути по умолчанию.
CORE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = CORE_ROOT / "html_examples" / "mir_court_list_full.html"
DEFAULT_OUT = CORE_ROOT / "data" / "courts.json"

# Страница sudrf.ru отдаётся в кодировке windows-1251.
SRC_ENCODING = "windows-1251"

# Допустимые уровни суда (совпадают с CourtLevel в app/models/database.py).
LEVELS = ("mirsud", "general", "appeal", "kas")

# Один суд в списке: ссылка с onclick=listcontrol(<idx>,"<код>"); ...>Название</a>.
COURT_RE = re.compile(r"""listcontrol\(\d+,\s*["'](?P<code>[0-9A-Za-z]+)["']\)[^>]*>(?P<name>.*?)</a>""", re.S)
# Регион (субъект РФ) внутри блока суда.
REGION_RE = re.compile(r"class=['\"]sud_ter_name['\"]>(?P<region>.*?)</div>", re.S)
# Первая ссылка в блоке — официальный сайт суда (может отсутствовать).
HREF_RE = re.compile(r"href=['\"](?P<url>[^'\"]*)['\"]")


def _clean(text: str) -> str:
    """Убрать HTML-сущности и лишние пробелы (в т.ч. неразрывные)."""
    return html.unescape(text).replace("\xa0", " ").strip()


def parse_courts(html_text, level):
    """Разобрать HTML-список судов в список словарей code/name/level/region/base_url."""
    courts = []
    matches = list(COURT_RE.finditer(html_text))
    for i, match in enumerate(matches):
        # Блок текущего суда — от конца его ссылки до начала следующей (или до конца файла).
        block_end = matches[i + 1].start() if i + 1 < len(matches) else len(html_text)
        block = html_text[match.end():block_end]

        region_match = REGION_RE.search(block)
        href_match = HREF_RE.search(block)

        courts.append({
            "code": match.group("code").strip(),
            "name": _clean(match.group("name")),
            "level": level,
            "region": _clean(region_match.group("region")) if region_match else "",
            "base_url": href_match.group("url").strip() if href_match else None,
        })
    return courts


def main() -> None:
    parser = argparse.ArgumentParser(description="Собрать JSON-справочник судов из HTML-страницы sudrf.ru.")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="исходный HTML-файл со списком судов")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="куда записать JSON")
    parser.add_argument("--level", choices=LEVELS, default="mirsud", help="уровень судов в этом списке")
    args = parser.parse_args()

    html_text = args.src.read_text(encoding=SRC_ENCODING)
    courts = parse_courts(html_text, args.level)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(courts, ensure_ascii=False, indent=2), encoding="utf-8")

    regions = {court["region"] for court in courts}
    print(f"Записано в {args.out}")
    print(f"судов: {len(courts)}")
    print(f"регионов: {len(regions)}")


if __name__ == "__main__":
    main()
