"""Временный разведочный скрипт: сходить на карточку дела mirsud.spb.ru через прокси."""
import sys
from urllib.parse import urlsplit

sys.path.insert(0, "/app")

from app.browser import ChromiumSession, lease_proxy  # noqa: E402

URLS = [
    "https://mirsud.spb.ru/cases/detail/126/?id=2-1557%2F2026-126",
    "https://mirsud.spb.ru/cases/detail/9/?id=5-1628%2F2026-9",
]

proxy = lease_proxy()
print("Прокси:", proxy or "напрямую", flush=True)

for i, url in enumerate(URLS):
    print("\n===", url, flush=True)
    with ChromiumSession(headless=True, proxy=proxy) as session:
        try:
            response = session.goto(url)
            status = response.status if response is not None else None
            html = session.content()
        except Exception as exc:
            print("  FAIL:", type(exc).__name__, str(exc)[:300], flush=True)
            continue
        print("  status:", status, "len:", len(html), "final url:", session.page.url, flush=True)
        path = f"/app/html_examples/_probe_spb_{i}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print("  saved:", path, flush=True)
