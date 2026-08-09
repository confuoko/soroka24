"""Клиент к core-api: единственное место, где client знает адреса и формат core."""
from apps.core_client.client import (
    CoreApiError,
    get_case,
    get_case_summary,
    get_search_task,
    request_case_sync,
    set_monitoring,
)

__all__ = [
    "CoreApiError",
    "request_case_sync",
    "get_search_task",
    "get_case",
    "get_case_summary",
    "set_monitoring",
]
