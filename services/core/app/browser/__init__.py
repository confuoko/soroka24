# Работа с браузером Chromium (Playwright) — переиспользуемые методы.
# Основной класс — ChromiumSession из browser/chromium.py.
# Пул прокси, через которые ходит браузер, — в browser/proxy.py,
# а прослойка между браузером и прокси — в browser/relay.py.
from app.browser.chromium import ChromiumSession
from app.browser.proxy import (
    ProxySettings,
    ProxyUnavailable,
    lease_pinned_proxy,
    lease_proxy,
    parse_proxy_url,
)
from app.browser.relay import ProxyRelay

__all__ = [
    "ChromiumSession",
    "ProxyRelay",
    "ProxySettings",
    "ProxyUnavailable",
    "lease_pinned_proxy",
    "lease_proxy",
    "parse_proxy_url",
]
