"""Клиенты судов: как добраться до страницы карточки дела.

Клиент отвечает на один вопрос — **как получить страницу?** Навигация, формы поиска,
капча, прокси, cookies, ошибки транспорта. Что написано ВНУТРИ страницы — не его дело:
это разбирают парсеры (app/parsers/).

Главное правило, из которого следует вся раскладка этого пакета:

    **Клиент соответствует СПОСОБУ ДОСТУПА к порталу, а не вёрстке страницы.**

Три клиента, потому что есть три разных способа добраться до карточки:

| Клиент         | Портал        | Как добираемся |
|----------------|---------------|----------------|
| `MoscowClient` | mos-sud.ru    | форма поиска по УИД, затем открытие строк таблицы результатов |
| `MsudrfClient` | msudrf.ru     | прямая ссылка + прохождение капчи; сертификат субдоменов невалиден |
| `SpbClient`    | mirsud.spb.ru | прямая ссылка + ожидание, пока Angular дорисует карточку |

Вёрсток при этом ЧЕТЫРЕ: движок msudrf.ru отдаёт две разные разметки карточки (типы B и
C), и какую именно — заранее неизвестно, потому что регионы подключают по домену. Поэтому
клиент у него один, а парсера два.

В старом core клиентов было четыре: у msudrf их было два, и второй состоял целиком из
одной строки `page_type = "C"`. Ходить на портал ему было нечем отличаться.

**Новая вёрстка добавляет парсер, а не клиента.** Новый клиент нужен только тогда, когда
меняется сам способ добраться до страницы.

Номер дела со страницы достают обычные функции рядом с клиентом своего портала —
`extract_msudrf_case_code`, `extract_spb_case_code`. Не методы клиента: номер это
содержимое страницы. У Москвы такой функции нет вовсе — там номер известен из таблицы
результатов поиска ещё до открытия карточки, и он приезжает в `FetchedCard.case_code`.
"""
from app.courts.base import (
    CaseNotFound,
    CourtClient,
    CourtError,
    FetchedCard,
    FetchFailed,
    PageSnapshot,
    UnsupportedCourt,
    find_uid,
    is_retryable_status,
)
from app.courts.moscow import MoscowClient
from app.courts.msudrf import MsudrfClient, extract_msudrf_case_code
from app.courts.resolver import (
    client_class_by_uid,
    client_class_by_url,
    define_court_by_uid,
    define_court_by_url,
    is_supported_url,
    portal_for,
)
from app.courts.spb import SpbClient, extract_spb_case_code

__all__ = [
    # общее
    "CaseNotFound",
    "CourtClient",
    "CourtError",
    "FetchFailed",
    "FetchedCard",
    "PageSnapshot",
    "UnsupportedCourt",
    "find_uid",
    "is_retryable_status",
    # клиенты
    "MoscowClient",
    "MsudrfClient",
    "SpbClient",
    # номер дела со страницы
    "extract_msudrf_case_code",
    "extract_spb_case_code",
    # выбор клиента
    "client_class_by_uid",
    "client_class_by_url",
    "define_court_by_uid",
    "define_court_by_url",
    "is_supported_url",
    "portal_for",
]
