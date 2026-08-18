"""Скрипт-команда: сходить по прямой ссылке на карточку дела.

Проходит весь путь боевого клиента — открывает ссылку через прокси, при необходимости
разгадывает капчу (там, где она есть), забирает HTML карточки и достаёт из него УИД.
Суд ищет в справочнике так же, как это делает задача синхронизации.

Нужен, чтобы отлаживать путь до карточки и копить примеры разметки для парсеров, не
гоняя ради этого очередь Celery. Работает со всеми порталами, подключёнными к
резолверу: движок msudrf.ru (тип B) и mirsud.spb.ru (тип D, разбор ещё не написан —
как раз под сбор образцов).

Запуск (из папки services/core, чтобы резолвился пакет app):
    python scripts/fetch_case_by_url.py --url "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs&case_id=429386415&delo_id=1540005"
    python scripts/fetch_case_by_url.py --url ... --url ... --save-html
    python scripts/fetch_case_by_url.py --url ... --save-s3     # положить снимок в S3
    python scripts/fetch_case_by_url.py --url ... --proxy http://user:pass@host:port
    python scripts/fetch_case_by_url.py --url ... --no-headless      # посмотреть глазами

Без --proxy прокси берётся из пула в БД (таблица proxy), как в бою. Учтите, что не
всякий прокси доходит до msudrf.ru — если поход упал на туннеле, повторите запуск,
пул выдаст следующий адрес.

Ссылку в shell берите в кавычки: в ней есть символы & и ?.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

# Добавляем корень core в sys.path, чтобы `import app...` работал при запуске из любой папки.
CORE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_ROOT))

from app.browser import lease_proxy, parse_proxy_url  # noqa: E402
from app.courts import define_court_by_url  # noqa: E402
from app.courts.base import find_uid  # noqa: E402
from app.models.database import session_scope  # noqa: E402
from app.repositories import CourtRepository  # noqa: E402
from app.storage.html_snapshots import save_snapshot  # noqa: E402
from app.validators import synthetic_uid  # noqa: E402

HTML_DIR = CORE_ROOT / "html_examples"


def _court(url: str) -> tuple[str, str | None]:
    """(название суда, код) из справочника по ссылке; название-пометка, если суда там нет.

    Именно get_by_url, а не get_by_host: у порталов с одним хостом на весь регион
    (Петербург) по хосту суд не определить, там он ищется по номеру участка из пути.
    Тем же методом суд определяет и задача синхронизации.
    """
    with session_scope() as session:
        court = CourtRepository(session).get_by_url(url)
        if court is None:
            return "НЕТ В СПРАВОЧНИКЕ", None
        return court.name, court.code


def _save_html(url: str, html: str, uid: str) -> Path:
    """Сложить страницу в html_examples/ под именем, по которому её потом найти.

    Имя строим из УИД, а не из адреса: он единственный идентификатор, одинаково
    осмысленный на всех порталах, и по нему же названа папка снимка в S3. Разбор адреса
    для этого не годится — у Петербурга в ссылке нет ничего, кроме номера участка и
    номера дела со слэшем, и файл получал бы имя-хэш.
    """
    host = (urlsplit(url).hostname or "unknown-host").split(".", 1)[0]
    path = HTML_DIR / f"case_{host}_{uid}.html"
    path.write_text(html, encoding="utf-8")
    return path


def _captcha_report(attempts: list) -> str:
    """«2 шт., 0.06 RUB» — сколько капч решено за поход и во сколько это обошлось.

    Цену с неизвестной стоимостью (не дождались ответа) считаем отдельно: занижать
    расход молчанием нельзя.
    """
    if not attempts:
        return "0 шт."
    known = [a.cost for a in attempts if a.cost is not None]
    if not known:
        return f"{len(attempts)} шт., цена неизвестна"
    unknown = len(attempts) - len(known)
    tail = f", без цены: {unknown}" if unknown else ""
    return f"{len(attempts)} шт., {sum(known)} {attempts[0].currency}{tail}"


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
    parser.add_argument(
        "--save-s3",
        action="store_true",
        help="положить снимок страницы в S3 (папка — УИД со страницы)",
    )
    args = parser.parse_args()

    proxy = parse_proxy_url(args.proxy) if args.proxy else lease_proxy()
    print(f"Прокси: {proxy or 'напрямую'}\n")

    failed = 0
    for url in args.url:
        print(url)
        # Расходы копим в список, а не пишем в БД: скрипт запускают руками, задачи под
        # него нет, а увидеть живые цены сервиса полезно — они плавают от нагрузки.
        spent = []
        client = define_court_by_url(
            url,
            proxy=proxy,
            headless=not args.no_headless,
            on_captcha_attempt=spent.append,
        )
        started = datetime.utcnow()
        court_name, court_code = _court(url)
        try:
            html = client.fetch_case_html_by_url(url)
            # Тем же правилом, что и задача синхронизации: УИД со страницы, а если его там
            # нет вовсе (архивные дела движка) — самодельный ключ от ссылки. Иначе скрипт
            # падал бы ровно на тех карточках, ради сбора которых его и запускают.
            uid = find_uid(html) or synthetic_uid(court_code or "unknown", url)
        except Exception as exc:
            failed += 1
            print(f"  [FAIL] {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            print(f"  капчи: {_captcha_report(spent)}\n")
            continue

        elapsed = (datetime.utcnow() - started).total_seconds()
        print(f"  [OK ] html: {len(html)} симв., капчи: {_captcha_report(spent)}")
        print(f"  УИД: {uid}")
        print(f"  суд по ссылке: {court_name}")
        if args.save_html:
            print(f"  сохранено: {_save_html(url, html, uid)}")
        if args.save_s3:
            # Папка снимка — УИД, он к этому моменту уже прочитан со страницы. Уровень
            # карточки (card=) не передаём: номер дела для него достаёт клиент, а у
            # порталов, ради которых скрипт и запускают, разбор ещё не написан.
            stored = save_snapshot(uid, html, started)
            print(f"  в S3: {stored['html_key']} ({stored['html_size']} б)")
        print(f"  время: {elapsed:.0f} c\n")

    # Ненулевой код возврата, чтобы скрипт годился для проверки в CI/по расписанию.
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
