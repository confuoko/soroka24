"""HTTP-клиент к core-api.

Границу между сервисами держим здесь: в БД core клиент не ходит, две ORM на одних
таблицах — источник поломок при каждой alembic-миграции.

Все функции поднимают CoreApiError на любой сетевой сбой и на любой ответ, который
не 2xx. Разбирать статусы бизнес-уровня («суд не поддержан», «дело уже есть») —
дело сервисного слоя, а не транспорта.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Статусы ответа POST /search_case (см. services/core/app/api/routes.py).
STATUS_EXISTS = "exists"
STATUS_PROCESSING = "processing"
STATUS_INVALID_QUERY = "invalid_query"
STATUS_INVALID_UID = "invalid_uid"
STATUS_LINK_REQUIRED = "link_required"
STATUS_UNSUPPORTED_COURT = "unsupported_court"

# Статусы SearchTask (см. SearchStatus в services/core/app/models/database.py).
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_FAILED = "failed"


class CoreApiError(Exception):
    """core-api недоступен или ответил ошибкой."""


def _request(method: str, path: str, **kwargs) -> dict:
    """Сходить в core и вернуть распарсенный JSON.

    422 отдельно НЕ выделяем: core отвечает им на «дело нельзя взять» и кладёт в
    тело нормальный CaseSyncResponse со status и message — его должен увидеть
    сервисный слой, а не потерять транспорт.
    """
    url = f"{settings.CORE_API_URL}{path}"
    try:
        response = requests.request(
            method, url, timeout=settings.CORE_API_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise CoreApiError(f"core-api недоступен ({url}): {exc}") from exc

    if response.status_code == 422 and _looks_like_sync_response(response):
        return response.json()

    if not response.ok:
        raise CoreApiError(
            f"core-api ответил {response.status_code} на {method} {path}: "
            f"{response.text[:500]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise CoreApiError(f"core-api вернул не JSON на {method} {path}") from exc


def _looks_like_sync_response(response: requests.Response) -> bool:
    """Это осмысленный отказ core (со status), а не 422 от валидации FastAPI?"""
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and "status" in body


def request_case_sync(query: str, force: bool = False) -> dict:
    """POST /search_case — поставить дело в очередь на разбор.

    query — ссылка на карточку или УИД: core различает их сам по схеме адреса,
    поэтому поле одно.

    Возвращает {status, case_id, case_ids, task_id, message}.
    """
    return _request("POST", "/search_case", json={"query": query, "force": force})


def get_search_task(task_id: int) -> dict:
    """GET /search_case/tasks/{id} — состояние задачи разбора.

    Возвращает {task_id, uid, source_url, status, case_id, attempts, last_error}.
    """
    return _request("GET", f"/search_case/tasks/{task_id}")


def get_case(case_id: int) -> dict:
    """GET /cases/{id} — карточка дела целиком (со всеми событиями и документами)."""
    return _request("GET", f"/cases/{case_id}")


def get_case_summary(case_id: int) -> dict:
    """GET /cases/{id}/summary — только витрина: статус и даты.

    Отдельный лёгкий эндпоинт нужен потому, что полная карточка тянет все события,
    заседания и документы — для списка дел это сотни килобайт на каждое дело.
    """
    return _request("GET", f"/cases/{case_id}/summary")


def set_monitoring(case_id: int, enabled: bool) -> dict:
    """POST /cases/{id}/monitoring — включить/выключить периодический обход дела.

    Кто именно из пользователей следит за делом, core не знает и знать не должен:
    это состояние клиента. В core хранится только сам факт, что дело обходить надо.
    """
    return _request("POST", f"/cases/{case_id}/monitoring", json={"enabled": enabled})
