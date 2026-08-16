"""Скрипт-команда: проверить прокси настоящим Chromium и (по желанию) завести его в БД.

Проверяем именно браузером, а не curl: в бою на портал суда ходит Playwright/Chromium,
и только такая проверка показывает то, что реально будет работать. Скрипт открывает
сервис определения IP через прокси и печатает, какой адрес увидел сайт. Успех — это
когда напечатан IP прокси, а не IP вашей машины.

С флагом --sites скрипт вдобавок ходит на сами порталы судов. Это разные вопросы:
«прокси жив» и «прокси годится для дела». Прокси бывают живые, но забаненные
порталом, причём выборочно — один ходит на mos-sud.ru и не ходит на msudrf.ru,
другой наоборот. Ради этого флаг и нужен: перед покупкой (или сразу после) видно,
на что именно новый адрес годится. Пробы описаны в app/courts/site_probe.py.

Капчу скрипт не разгадывает — она стоит денег, а показанная капча и так означает,
что прокси до портала доходит. Такой исход считается успехом.

Годятся обе схемы, http и socks5, в том числе с логином и паролем: Chromium ходит в
прокси не сам, а через локальный релей (app/browser/relay.py) — он и разбирается с
авторизацией.

Запуск (из папки services/core, чтобы резолвился пакет app):
    # разовая проверка строки, без БД
    python scripts/check_proxy.py --url http://user:pass@host:port
    # купили новую прокси: проверить её по всем порталам судов
    python scripts/check_proxy.py --url http://user:pass@host:port --sites all
    # проверить и, если жив, записать в таблицу proxy
    python scripts/check_proxy.py --url http://user:pass@host:port --save "куплен до 01.09"
    # проверить все включённые прокси из БД (нужна поднятая БД)
    python scripts/check_proxy.py
    # весь пул по всем порталам, заодно посмотреть, как отвечают напрямую
    python scripts/check_proxy.py --sites all --direct
    # заодно посмотреть глазами, что видит браузер
    python scripts/check_proxy.py --url ... --no-headless

В БД скрипт пишет только по флагу --save (и только строку из --url). Результаты
проверки по порталам нигде не сохраняются: комментарии к прокси в /admin ведутся
руками, скрипт лишь подсказывает готовую строку.

Адрес БД берётся из app.config.DATABASE_URL (env DATABASE_URL).
"""
import argparse
import sys
import time
from pathlib import Path

# Добавляем корень core в sys.path, чтобы `import app...` работал при запуске из любой папки.
CORE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_ROOT))

from app.browser import ChromiumSession, ProxySettings, parse_proxy_url  # noqa: E402
from app.courts.site_probe import (  # noqa: E402
    SITE_PROBES,
    ProbeResult,
    one_line,
    probe_site,
)
from app.models.database import Proxy, session_scope  # noqa: E402
from app.repositories import ProxyRepository  # noqa: E402

# Сервис, который отдаёт видимый снаружи IP простым текстом (без разметки и JS).
IP_CHECK_URL = "https://api.ipify.org"

# Как подписан поход напрямую в таблице результатов.
DIRECT_LABEL = "напрямую"


def check(proxy: ProxySettings | None, headless: bool = True) -> tuple[bool, str, float]:
    """Сходить через прокси за своим внешним IP.

    Возвращает (успех, увиденный IP или текст ошибки, время в секундах).
    """
    started = time.monotonic()
    try:
        with ChromiumSession(headless=headless, proxy=proxy) as session:
            session.goto(IP_CHECK_URL)
            # На странице только сам IP текстом — берём его из body.
            ip = session.page.inner_text("body").strip()
        return True, ip, time.monotonic() - started
    except Exception as exc:
        return False, str(exc), time.monotonic() - started


def _report(label: str, ok: bool, detail: str, elapsed: float) -> None:
    mark = "OK " if ok else "FAIL"
    print(f"[{mark}] {label} -> {detail}  ({elapsed:.1f} c)")


def _print_matrix(rows: list[dict], site_names: list[str]) -> None:
    """Свести всё в таблицу «прокси × портал».

    Построчный вывод по ходу дела остаётся (проверка идёт минутами, и молчащий
    терминал выглядит как зависший), но сравнивать прокси между собой удобно только
    в таблице — ради неё и весь флаг --sites.
    """
    headers = ["Прокси", "ipify"] + site_names
    table = [headers]
    for row in rows:
        cells = [row["label"], row["ipify"]]
        cells += [str(row["sites"].get(name, "—")) for name in site_names]
        table.append(cells)

    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    print("\nИтог:")
    for line in table:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)))


def _comment_hint(row: dict, site_names: list[str]) -> str:
    """Строка для поля comment в /admin — её предлагаем скопировать руками.

    Сам скрипт comment не трогает: там живут заметки человека (у кого куплено, до
    какого числа), и затирать их выводом проверки нельзя.
    """
    parts = [
        f"{name} — {'да' if row['sites'][name].ok else 'нет'}"
        for name in site_names
        if name in row["sites"]
    ]
    return "; ".join(parts)


def _save(proxy: ProxySettings, comment: str) -> None:
    """Записать проверенный прокси в таблицу proxy (или обновить существующий)."""
    with session_scope() as session:
        repo = ProxyRepository(session)
        existing = repo.get_by_host_port(proxy.host, proxy.port)
        if existing is not None:
            # Тот же адрес уже заведён — обновляем учётные данные и включаем обратно.
            existing.scheme = proxy.scheme
            existing.username = proxy.username
            existing.password = proxy.password
            existing.enabled = True
            existing.comment = comment
            print(f"обновлён существующий прокси id={existing.id}")
            return
        session.add(
            Proxy(
                scheme=proxy.scheme,
                host=proxy.host,
                port=proxy.port,
                username=proxy.username,
                password=proxy.password,
                enabled=True,
                comment=comment,
            )
        )
        print("прокси добавлен в пул")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Проверить прокси настоящим Chromium и при желании завести его в БД."
    )
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        metavar="http://user:pass@host:port",
        help="строка прокси; можно указать несколько раз. Без --url проверяются прокси из БД",
    )
    parser.add_argument(
        "--save",
        metavar="КОММЕНТАРИЙ",
        help="записать прокси в таблицу proxy, если проверка прошла (только вместе с --url)",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="заодно сходить напрямую, без прокси — видно, какой IP у самой машины",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="показать окно браузера (по умолчанию headless)",
    )
    parser.add_argument(
        "--sites",
        action="append",
        default=[],
        choices=["all", *SITE_PROBES],
        help="проверить ещё и порталы судов; можно указать несколько раз (all — все)",
    )
    args = parser.parse_args()

    if args.save and not args.url:
        parser.error("--save работает только вместе с --url")

    headless = not args.no_headless
    # Порядок колонок задаём по SITE_PROBES, а не по порядку флагов: так таблица
    # выглядит одинаково при любом наборе аргументов.
    site_names = (
        [name for name in SITE_PROBES if "all" in args.sites or name in args.sites]
        if args.sites
        else []
    )

    # Что проверяем: явно переданные строки либо весь включённый пул из БД.
    if args.url:
        proxies = [parse_proxy_url(url) for url in args.url]
    else:
        with session_scope() as session:
            proxies = [
                ProxySettings(
                    scheme=row.scheme,
                    host=row.host,
                    port=row.port,
                    username=row.username,
                    password=row.password,
                )
                for row in ProxyRepository(session).list_enabled()
            ]
        if not proxies:
            print("В таблице proxy нет включённых прокси — проверять нечего.")
            return
        print(f"Проверяем пул из БД: {len(proxies)} шт.")

    # None — поход без прокси. Идёт первым: полезно видеть, как порталы отвечают
    # самой машине, прежде чем разбираться, что не так с прокси.
    targets: list[ProxySettings | None] = ([None] if args.direct else []) + list(proxies)

    failed = 0
    rows: list[dict] = []
    for proxy in targets:
        label = DIRECT_LABEL if proxy is None else str(proxy)
        ok, detail, elapsed = check(proxy, headless=headless)
        # Отказ приходит с call log Playwright на десяток строк — в таблицу берём
        # только первую, иначе она разъезжается.
        detail = detail if ok else one_line(detail)
        _report(f"{label} — ipify", ok, detail, elapsed)
        row = {"label": label, "ipify": detail if ok else f"FAIL {detail}", "sites": {}}
        rows.append(row)

        if not ok:
            # Мёртвый прокси гонять по порталам незачем: это ещё минута на адрес,
            # а вердикт всё равно будет про сеть, а не про портал.
            # Поход напрямую в счётчик отказов не идёт: он справочный, а код возврата
            # должен говорить про прокси.
            failed += proxy is not None
            continue

        for name in site_names:
            result: ProbeResult = probe_site(
                SITE_PROBES[name], proxy=proxy, headless=headless
            )
            _report(f"{label} — {name}", result.ok, str(result), result.elapsed)
            row["sites"][name] = result

        # Прокси, не прошедший НИ ОДИН портал, бесполезен, даже если ipify открылся.
        if proxy is not None and site_names and not any(r.ok for r in row["sites"].values()):
            failed += 1

        if args.save and proxy is not None:
            _save(proxy, args.save)

    if site_names:
        _print_matrix(rows, site_names)
        # Подсказку печатаем только по тем, кто до порталов вообще доехал: у мёртвого
        # прокси проб нет, и предлагать по нему строку комментария не о чем.
        probed = [row for row in rows if row["sites"]]
        if probed:
            print("\nСтрока для поля comment в /admin (скопировать руками):")
            for row in probed:
                print(f"  {row['label']}: {_comment_hint(row, site_names)}")

    # Ненулевой код возврата, чтобы скрипт годился для проверки в CI/по расписанию.
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
