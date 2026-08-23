"""Скрипт-команда: сходить по прямой ссылке на карточку дела.

Проходит весь путь боевого обхода: открывает ссылку через прокси, при необходимости
разгадывает капчу, забирает HTML карточки и опознаёт её ТЕМИ ЖЕ функциями, что и обход
(resolve_case_uid, resolve_case_code). Суд ищет в справочнике тем же методом.

Это важно именно так: пока у скрипта была своя копия правила опознания, он мог назвать
карточку иначе, чем боевой обход, и никакой тест этого бы не поймал.

Нужен, чтобы отлаживать путь до карточки и копить примеры разметки для парсеров, не
гоняя ради этого очередь Celery. Работает со всеми порталами, подключёнными к
резолверу: движок msudrf.ru (обе вёрстки) и mirsud.spb.ru.

Запуск (из папки services/core_v2, чтобы резолвился пакет app):
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Добавляем корень core в sys.path, чтобы `import app...` работал при запуске из любой папки.
CORE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_ROOT))

from app.browser import parse_proxy_url  # noqa: E402
from app.courts import define_court_by_url, portal_for  # noqa: E402
from app.database import session_scope  # noqa: E402
from app.repositories import CourtRepository  # noqa: E402
from app.services.identity import resolve_case_code, resolve_case_uid  # noqa: E402
from app.services.proxy_pool import lease_proxy  # noqa: E402
from app.storage.html_snapshots import save_snapshot  # noqa: E402

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

    # Прокси арендуем ПОД ПОРТАЛ, как это делает боевой обход: до разных порталов
    # доходят разные адреса (см. Proxy.portals). Без фильтра скрипт брал первый попавшийся
    # и мог упереться в таймаут на msudrf с адресом для mos-sud — причём выглядело бы это
    # как «портал не отвечает», хотя дело в адресе.
    #
    # Ссылок можно передать несколько, портал берём по первой: скрипт запускают руками и
    # обычно с адресами одного портала.
    portal = portal_for(url=args.url[0]) if args.url else None
    proxy = parse_proxy_url(args.proxy) if args.proxy else lease_proxy(portal=portal)
    where = f" (портал {portal})" if portal else ""
    print(f"Прокси: {proxy or 'напрямую'}{where}\n")

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
        started = datetime.now(timezone.utc)
        court_name, court_code = _court(url)
        try:
            fetched = client.fetch_card_by_url(url)
            html = fetched.html
            # ТОТ ЖЕ код, которым опознаёт карточку боевой обход, а не своя копия правила.
            # В старом core здесь стояла упрощённая версия: «УИД со страницы, иначе
            # самодельный от ссылки» — без первого шага, то есть без проверки, не заведена
            # ли уже карточка по этому адресу. Скрипт и обход могли назвать одну и ту же
            # карточку по-разному, и заметить это было нечем.
            with session_scope() as session:
                uid = resolve_case_uid(session, html, url, court_code or "unknown")
            case_code = resolve_case_code(portal_for(url=url) or "", fetched)
        except Exception as exc:
            failed += 1
            print(f"  [FAIL] {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
            print(f"  капчи: {_captcha_report(spent)}\n")
            continue

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"  [OK ] html: {len(html)} симв., капчи: {_captcha_report(spent)}")
        print(f"  УИД: {uid}")
        print(f"  номер дела: {case_code}")
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
