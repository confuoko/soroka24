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
import collections
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

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

# Номер судебного участка в названии суда: «Судебный участок № 235 ...».
PARTICIPOK_RE = re.compile(r"участок\s*№\s*(\d+)")

# Суды Московской области живут на поддоменах msudrf.ru, где поддомен — номер участка
# (у 369 из 374 записей; остальные пять в исходной странице побиты, см. _normalize_base_url).
MO_CODE_PREFIX = "50MS"
MO_HOST_TEMPLATE = "https://{number}.mo.msudrf.ru"

# Домен, внутри которого сайт суда — это отдельный поддомен на каждый участок, поэтому
# хост там годится как ключ поиска и обязан быть уникальным. Остальные порталы общие
# (mos-sud.ru — один на 471 московский суд), их уникальность не проверяем.
PER_COURT_DOMAIN = "msudrf.ru"


def _clean(text: str) -> str:
    """Убрать HTML-сущности и лишние пробелы (в т.ч. неразрывные)."""
    return html.unescape(text).replace("\xa0", " ").strip()


def participok_no(name: str) -> int | None:
    """Номер судебного участка из названия суда (или None, если его там нет).

    Есть не у всех: у 802 судов справочника номера в названии не указано.
    """
    match = PARTICIPOK_RE.search(name)
    return int(match.group(1)) if match else None


def _normalize_base_url(code: str, name: str, url: str | None) -> str | None:
    """Починить адрес сайта суда там, где в исходной странице он побит.

    Чинить приходится только Московскую область: у пяти её судов адрес на странице
    sudrf.ru либо потерял цифру поддомена (у 50MS0122 стоит 22.mo вместо 122.mo — то есть
    сайт ЧУЖОГО, Воскресенского суда), либо испорчен склейкой схемы («http://htt253.mo...»).
    Из-за этого три суда МО делили хост с соседями, а хост нам нужен как ключ поиска.

    Правило восстановления — поддомен равен номеру участка из названия; оно выполняется
    у 369 записей МО из 374, так что пять оставшихся — именно ошибки, а не исключения.
    Для остальных регионов адрес оставляем как есть: там свои форматы (у Петербурга это
    путь mirsud.spb.ru/court-sites/154, а не поддомен), и общего правила нет.
    """
    if not code.startswith(MO_CODE_PREFIX):
        return url

    number = participok_no(name)
    if number is None:
        return url

    expected = MO_HOST_TEMPLATE.format(number=number)
    host = urlsplit(url or "").hostname or ""
    # Совпало — не трогаем, чтобы не гнать в диффе 369 записей ради смены схемы на https.
    if host == f"{number}.mo.msudrf.ru":
        return url
    return expected


def check_hosts_unique(courts: list[dict]) -> list[str]:
    """Найти суды, которые после нормализации всё ещё делят один поддомен msudrf.ru.

    Возвращает описания коллизий (пустой список — всё хорошо). Молча выбирать «правильный»
    суд из двух нельзя: по хосту мы определяем суд дела, пришедшего ссылкой, и ошибка здесь
    означает привязку дела к чужому суду.
    """
    by_host: dict[str, list[dict]] = collections.defaultdict(list)
    for court in courts:
        host = urlsplit(court.get("base_url") or "").hostname or ""
        if host.endswith(PER_COURT_DOMAIN):
            by_host[host].append(court)

    problems = []
    for host, group in sorted(by_host.items()):
        if len(group) > 1:
            names = ", ".join(f"{c['code']} ({c['name']})" for c in group)
            problems.append(f"{host}: {names}")
    return problems


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

        code = match.group("code").strip()
        name = _clean(match.group("name"))
        base_url = href_match.group("url").strip() if href_match else None

        courts.append({
            "code": code,
            "name": name,
            "level": level,
            "region": _clean(region_match.group("region")) if region_match else "",
            "base_url": _normalize_base_url(code, name, base_url),
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

    # Файл не выпускаем, пока два суда делят поддомен: по нему определяется суд дела.
    problems = check_hosts_unique(courts)
    if problems:
        print("Один поддомен msudrf.ru на несколько судов — справочник не записан:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        raise SystemExit(1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(courts, ensure_ascii=False, indent=2), encoding="utf-8")

    regions = {court["region"] for court in courts}
    print(f"Записано в {args.out}")
    print(f"судов: {len(courts)}")
    print(f"регионов: {len(regions)}")


if __name__ == "__main__":
    main()
