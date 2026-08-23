# Прикладные операции над делами: то, что делает core, если убрать HTTP и Celery.
#
# Здесь лежит бизнес-логика, а не обёртки над ней: и FastAPI-роут, и Celery-задача
# должны быть тонкими вызовами отсюда. Обратной зависимости нет — этот пакет ничего
# не знает ни про HTTP, ни про брокер.
from app.services.case_sync import CaseChanges, sync_case
from app.services.identity import resolve_case_code, resolve_case_uid
from app.services.proxy_pool import ProxyUnavailable, lease_proxy

__all__ = [
    "CaseChanges",
    "ProxyUnavailable",
    "lease_proxy",
    "resolve_case_code",
    "resolve_case_uid",
    "sync_case",
]
