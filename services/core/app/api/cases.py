"""REST-роуты чтения дел: отдать всю информацию по делу по его id в БД.

Отдельный модуль, потому что роутер в app/api/routes.py живёт под префиксом
/search_case и относится к запуску синхронизации, а не к чтению карточки.
"""
from fastapi import APIRouter, HTTPException, status

from app.api.schemas import CaseDetailResponse
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
