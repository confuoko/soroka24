"""REST-роуты чтения дел: отдать всю информацию по делу по его id в БД.

Отдельный модуль, потому что роутер в app/api/routes.py живёт под префиксом
/search_case и относится к запуску синхронизации, а не к чтению карточки.
"""
from fastapi import APIRouter, HTTPException, status

from app.api.schemas import (
    CaseDetailResponse,
    CaseSummaryResponse,
    MonitoringRequest,
    MonitoringResponse,
)
from app.models.database import session_scope
from app.repositories import CaseRepository

router = APIRouter(prefix="/cases", tags=["cases"])


@router.get("/{case_id}", response_model=CaseDetailResponse)
def get_case(case_id: int) -> CaseDetailResponse:
    """Вернуть дело со всеми привязанными сущностями по его id в БД."""
    with session_scope() as session:
        # get_full подтягивает все связи сразу — иначе сборка ответа за пределами
        # сессии упала бы на ленивой загрузке.
        case = CaseRepository(session).get_full(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
        # Собираем ответ, пока сессия ещё открыта.
        return CaseDetailResponse.model_validate(case)


@router.get("/{case_id}/summary", response_model=CaseSummaryResponse)
def get_case_summary(case_id: int) -> CaseSummaryResponse:
    """Вернуть витрину дела: статус, дата изменения, дата последней проверки.

    Клиентскому сервису для списка дел нужны только эти поля, а полная карточка
    тянет все события, заседания и документы — на каждое дело в списке это лишние
    сотни килобайт по сети.
    """
    with session_scope() as session:
        case = CaseRepository(session).get_with_court(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
        return CaseSummaryResponse.model_validate(case)


@router.post("/{case_id}/monitoring", response_model=MonitoringResponse)
def set_case_monitoring(case_id: int, payload: MonitoringRequest) -> MonitoringResponse:
    """Включить или выключить периодический обход дела.

    Кто именно следит за делом, core не знает: это состояние клиентского сервиса.
    Здесь хранится только сам факт, что карточку надо переобходить, — по нему
    планировщик (sync_monitored_cases) и набирает дела.
    """
    with session_scope() as session:
        case = CaseRepository(session).set_monitoring(case_id, payload.enabled)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
        # Значение забираем до закрытия сессии.
        return MonitoringResponse(case_id=case_id, monitoring_enabled=case.monitoring_enabled)
