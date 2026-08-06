"""Скрипт-команда: сходить по ссылке на дело мирового суда Московской области.

Проходит весь путь боевого клиента — открывает ссылку через прокси, при необходимости
разгадывает капчу, забирает HTML карточки и достаёт из него УИД. По УИД проверяет,
есть ли такой суд в справочнике (первые 8 символов — его код).

Нужен, чтобы отлаживать путь до карточки и копить примеры разметки для парсера типа B,
не гоняя ради этого очередь Celery.

Запуск (из папки services/core, чтобы резолвился пакет app):
    python scripts/fetch_mo_case.py --url https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=429386415&delo_id=1540005
    python scripts/fetch_mo_case.py --url ... --url ... --save-html
    python scripts/fetch_mo_case.py --url ... --proxy http://user:pass@host:port
    python scripts/fetch_mo_case.py --url ... --no-headless      # посмотреть глазами

Без --proxy прокси берётся из пула в БД (таблица proxy), как в бою. Учтите, что не
всякий прокси доходит до msudrf.ru — если поход упал на туннеле, повторите запуск,
пул выдаст следующий адрес.

Ссылку в shell берите в кавычки: в ней есть символы & и ?.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

# Добавляем корень core в sys.path, чтобы `import app...` работал при запуске из любой папки.
CORE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_ROOT))

from app.browser import lease_proxy, parse_proxy_url  # noqa: E402
from app.courts import MoscowRegionCourtClient  # noqa: E402
from app.models.database import session_scope  # noqa: E402
from app.repositories import CourtRepository  # noqa: E402

HTML_DIR = CORE_ROOT / "html_examples"


def _court_name(uid: str) -> str:
    """Название суда из справочника по коду из УИД (или пометка, что его там нет)."""
    with session_scope() as session:
        court = CourtRepository(session).get_by_code(uid[:8])
        return court.name if court is not None else "НЕТ В СПРАВОЧНИКЕ"


def _save_html(url: str, html: str) -> Path:
    """Сложить страницу в html_examples/ под именем, по которому её потом найти."""
    case_id = url.rsplit("case_id=", 1)[-1].split("&")[0]
    host = url.split("//", 1)[-1].split(".", 1)[0]
    path = HTML_DIR / f"mo_case_{host}_{case_id}.html"
    path.write_text(html, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сходить по ссылке на дело мирового суда Московской области."
    )
    parser.add_argument(
        "--url", action="append", required=True, help="ссылка на карточку дела; можно несколько раз"
    )
    parser.add_argument(
        "--proxy",
        metavar="http://user:pass@host:port",
        help="конкретный прокси; по умолчанию берётся из пула в БД",
    )
    parser.add_argument("--no-headless", action="store_true", help="показать окно браузера")
    parser.add_argument(
        "--save-html", action="store_true", help=f"сохранить страницы в {HTML_DIR}"
    )
    args = parser.parse_args()

    proxy = parse_proxy_url(args.proxy) if args.proxy else lease_proxy()
    print(f"Прокси: {proxy or 'напрямую'}\n")

    failed = 0
    for url in args.url:
        print(url)
        client = MoscowRegionCourtClient(headless=not args.no_headless, proxy=proxy)
        started = datetime.utcnow()
        try:
            html = client.fetch_case_html(url)
            uid = client.extract_uid(html)
        except Exception as exc:
            failed += 1
            print(f"  [FAIL] {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            print(f"  капч разгадано: {client.captchas_solved}\n")
            continue

        elapsed = (datetime.utcnow() - started).total_seconds()
        print(f"  [OK ] html: {len(html)} симв., капч разгадано: {client.captchas_solved}")
        print(f"  УИД: {uid}")
        print(f"  суд {uid[:8]}: {_court_name(uid)}")
        if args.save_html:
            print(f"  сохранено: {_save_html(url, html)}")
        print(f"  время: {elapsed:.0f} c\n")

    # Ненулевой код возврата, чтобы скрипт годился для проверки в CI/по расписанию.
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
