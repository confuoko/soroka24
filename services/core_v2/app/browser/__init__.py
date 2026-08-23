# Работа с браузером Chromium (Playwright) — переиспользуемые методы.
#
# Основной класс — ChromiumSession из browser/chromium.py. Описание прокси —
# browser/proxy.py, прослойка между браузером и прокси — browser/relay.py.
#
# АРЕНДЫ прокси здесь нет: она открывает сессию БД и живёт в
# app/services/proxy_pool.py. В старом core она лежала здесь и тянула за собой
# app/repositories — это и был цикл импортов, из-за которого в репозитории судов
# приходилось делать отложенный импорт внутри функции.
from app.browser.chromium import ChromiumSession
from app.browser.proxy import ProxySettings, parse_proxy_url
from app.browser.relay import ProxyRelay

__all__ = [
    "ChromiumSession",
    "ProxyRelay",
    "ProxySettings",
    "parse_proxy_url",
]
