"""Скрипт-команда: проверить прокси настоящим Chromium и (по желанию) завести его в БД.

Проверяем именно браузером, а не curl: в бою на портал суда ходит Playwright/Chromium,
и только такая проверка показывает то, что реально будет работать. Скрипт открывает
сервис определения IP через прокси и печатает, какой адрес увидел сайт. Успех — это
когда напечатан IP прокси, а не IP вашей машины.

Годятся обе схемы, http и socks5, в том числе с логином и паролем: Chromium ходит в
прокси не сам, а через локальный релей (app/browser/relay.py) — он и разбирается с
авторизацией.

Запуск (из папки services/core, чтобы резолвился пакет app):
    # разовая проверка строки, без БД
    python scripts/check_proxy.py --url http://user:pass@host:port
    # проверить и, если жив, записать в таблицу proxy
    python scripts/check_proxy.py --url http://user:pass@host:port --save "куплен до 01.09"
    # проверить все включённые прокси из БД (нужна поднятая БД)
    python scripts/check_proxy.py
    # заодно посмотреть глазами, что видит браузер
    python scripts/check_proxy.py --url ... --no-headless

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
from app.models.database import Proxy, session_scope  # noqa: E402
from app.repositories import ProxyRepository  # noqa: E402

# Сервис, который отдаёт видимый снаружи IP простым текстом (без разметки и JS).
IP_CHECK_URL = "https://api.ipify.org"


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
    args = parser.parse_args()

    if args.save and not args.url:
        parser.error("--save работает только вместе с --url")

    headless = not args.no_headless

    if args.direct:
        ok, detail, elapsed = check(None, headless=headless)
        _report("напрямую (без прокси)", ok, detail, elapsed)

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

    failed = 0
    for proxy in proxies:
        ok, detail, elapsed = check(proxy, headless=headless)
        _report(str(proxy), ok, detail, elapsed)
        if not ok:
            failed += 1
            continue
        if args.save:
            _save(proxy, args.save)

    # Ненулевой код возврата, чтобы скрипт годился для проверки в CI/по расписанию.
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
