"""Единственное место, где этот сервис говорит с core_v2.

Простые функции на `requests`, по одной на ручку. Ни клиента-класса, ни сгенерированного
SDK, ни обёрток над обёртками: ручек семь, и все они «отправить запрос — вернуть JSON».

Смысл этого модуля не в переиспользовании, а в границе. `requests` не должен появляться во
views: иначе таймауты, обработка отказов и знание об адресах core расползутся по
обработчикам, и починить их разом станет негде.

## Про отказы

`CoreUnavailable` означает «до core не дозвонились или он ответил непонятным». Вызывающий
решает, что с этим делать: на странице списка — показать дела без витрин, при подписке —
записать подписку и разойтись во времени с синхронизацией мониторинга.

Чего мы НЕ делаем — не превращаем отказ core в 500 у пользователя. Недоступность соседнего
сервиса это ожидаемое состояние, а не исключительная ситуация.
"""
import logging
from typing import Iterable, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class CoreUnavailable(Exception):
    """core не ответил или ответил так, что разобрать нельзя."""


class MonitoringRefused(Exception):
    """core отказался принять список дел на мониторинг (409).

    Отдельно от CoreUnavailable, и это не педантизм. Единственная причина такого отказа —
    пустой список без `force`, то есть срабатывание защиты от НАШЕЙ аварии. Свалить это в
    «core недоступен» значило бы отправить того, кто разбирается, искать проблему в
    соседнем сервисе, тогда как проблема у нас: не собрался queryset.
    """


# Одна сессия на процесс: держит пул соединений, и каждый запрос не платит за новый
# TCP-хендшейк. Для страницы «мои дела» это заметно — она зовёт core на каждый показ.
_session = requests.Session()


def _url(path: str) -> str:
    return f"{settings.CORE_API_URL.rstrip('/')}{path}"


def _request(method: str, path: str, *, expected: tuple[int, ...] = (200,), **kwargs):
    """Запрос к core: вернуть разобранный JSON либо поднять CoreUnavailable.

    `expected` перечисляет коды, при которых ответ считается осмысленным. Список нужен
    из-за `POST /search_case`: он отдаёт 422 с ПОЛЕЗНЫМ телом («не тот формат УИД», «этот
    портал не поддержан»), и трактовать это как аварию было бы неверно — пользователю надо
    показать ровно то, что там написано.
    """
    url = _url(path)
    try:
        response = _session.request(
            method, url, timeout=settings.CORE_API_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        logger.warning("core недоступен: %s %s — %s", method, url, exc)
        raise CoreUnavailable(f"{method} {path}: {exc}") from exc

    if response.status_code not in expected:
        logger.warning(
            "core ответил %s на %s %s: %s",
            response.status_code, method, url, response.text[:300],
        )
        raise CoreUnavailable(f"{method} {path}: HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError as exc:
        logger.warning("core вернул не JSON на %s %s", method, url)
        raise CoreUnavailable(f"{method} {path}: ответ не JSON") from exc


# --------------------------------------------------------------- добавление дела
def search_case(query: str, force: bool = False) -> dict:
    """Попросить core завести дело по ссылке или УИД.

    Одно поле на оба способа: что именно прислали, core определяет сам по схеме адреса. У
    части порталов поиска по УИД нет вовсе, и там дело открывается только прямой ссылкой.

    Возвращает тело как есть — у него пять осмысленных исходов в поле `status`
    (`exists`, `processing`, `invalid_query`, `invalid_uid`, `link_required`,
    `unsupported_court`), и решать по нему — дело view. Поле `message` содержит ГОТОВЫЙ
    человеческий текст отказа: его надо показывать как есть, а не сочинять свой.

    202 — задача заведена, 200 — дело уже есть, 422 — отказ с объяснением. Все три
    нормальные ответы.
    """
    return _request(
        "POST",
        "/search_case",
        expected=(200, 202, 422),
        json={"query": query, "force": force},
    )


def get_search_task(task_id: int) -> dict:
    """Состояние задачи поиска: `pending` → `running` → `success` | `failed`.

    Её опрашивают, пока дело ищется. УИД в ответе пустой, пока до портала ещё не дошли:
    он становится известен только с полученной страницы.
    """
    return _request("GET", f"/search_case/tasks/{task_id}")


# ------------------------------------------------------------- показ судебных данных
def get_case(case_id: int) -> dict:
    """Полная карточка дела со всеми вложенными сущностями — для страницы дела.

    Весит сотни килобайт, поэтому только для страницы одного дела; для списка есть
    `list_case_summaries`.
    """
    return _request("GET", f"/cases/{case_id}")


def list_case_summaries(case_ids: Iterable[int]) -> list[dict]:
    """Витрины сразу нескольких дел — ОДНИМ запросом.

    Ради страницы «мои дела». Без этой ручки она стоила бы N последовательных вызовов
    `/cases/{id}/summary`, то есть N+1 по сети, где каждый шаг — десятки миллисекунд.

    Отсутствующие id в ответе молча не появляются: дело могло исчезнуть из core, и это не
    причина не показать остальные. Заметить пропажу можно, сравнив длину.
    """
    ids = sorted({int(case_id) for case_id in case_ids})
    if not ids:
        # Пустой запрос до core не доходит: спрашивать «расскажи про ноль дел» незачем.
        return []
    return _request(
        "GET", "/cases", params={"ids": ",".join(str(case_id) for case_id in ids)}
    )


def summaries_by_id(case_ids: Iterable[int]) -> dict[int, dict]:
    """То же, но словарём — так удобнее раскладывать витрины по подпискам в шаблоне."""
    return {summary["id"]: summary for summary in list_case_summaries(case_ids)}


# ------------------------------------------------------------------- мониторинг
def replace_monitored_cases(
    case_ids: Iterable[int], force: bool = False
) -> dict:
    """Сказать core, за какими делами следить. Список ПОЛНЫЙ, семантика замещающая.

    После запроса на регулярном обходе ровно эти дела. Полный список, а не дифф, потому
    что своё состояние мы знаем целиком, а состояние core — нет: дифф пришлось бы считать
    от того, чего мы не видим.

    Пустой список core отклонит с 409, если не передать `force`. Это защита от нашей же
    аварии: пустой queryset из-за опечатки в фильтре выглядит точно так же, как «ни на что
    больше не подписаны», а снятое зря дело перестаёт обновляться молча.

    Возвращает `{"monitored": …, "added": …, "removed": …, "unknown_ids": [...]}`.
    `unknown_ids` — подписки на дела, которых в core нет; их стоит показать в логе, а не
    проглотить.
    """
    ids = sorted({int(case_id) for case_id in case_ids})
    params = {"force": "true"} if force else None
    answer = _request(
        "PUT",
        "/monitoring/cases",
        expected=(200, 409),
        params=params,
        json={"case_ids": ids},
    )
    # 409 — это не «core сломался», а «core не поверил пустому списку». Разница важна для
    # того, кто будет разбираться: искать надо у нас, а не в соседнем сервисе.
    if "monitored" not in answer:
        raise MonitoringRefused(answer.get("detail") or "core отклонил список")
    return answer


def ping() -> Optional[dict]:
    """Живой ли core. Нужен диагностике, а не рабочему пути."""
    return _request("GET", "/ping")
