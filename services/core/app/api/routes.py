"""REST-роуты для работы с делами (Case) и задачами их синхронизации."""
from fastapi import APIRouter, HTTPException, Response, status

from app.api.schemas import CaseSyncRequest, CaseSyncResponse, SearchTaskResponse
from app.courts import UnsupportedCourt, define_court_by_uid
from app.models.database import session_scope
from app.monitoring.tasks import sync_case
from app.repositories import CaseRepository, SearchTaskRepository
from app.validators import normalize_uid, validate_uid

# Роутер с общим префиксом /search_case.
router = APIRouter(prefix="/search_case", tags=["search_case"])


@router.post("", response_model=CaseSyncResponse, status_code=status.HTTP_202_ACCEPTED)
def request_for_case_sync(payload: CaseSyncRequest, response: Response) -> CaseSyncResponse:
    """Принять УИД, вернуть id существующего дела или запустить фоновую синхронизацию."""
    # 1. Нормализуем и проверяем формат УИД.
    uid = normalize_uid(payload.uid)
    if not validate_uid(uid):
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return CaseSyncResponse(status="invalid_uid")

    # 2. Определяем суд по префиксу УИД; неизвестный суд — отказ.
    try:
        define_court_by_uid(uid)
    except UnsupportedCourt:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return CaseSyncResponse(status="unsupported_court")

    with session_scope() as session:
        # cases — репозиторий дел: инкапсулирует все запросы к таблице case.
        cases = CaseRepository(session)
        # tasks — репозиторий задач поиска: работа с таблицей search_task.
        tasks = SearchTaskRepository(session)

        # 3. Дело уже в БД — сразу отдаём его id.
        #    С force=true не выходим, а идём парсить заново: так наполняется история
        #    diff'ов (Case.diff_history) и подтягиваются свежие события.
        existing_case = cases.get_by_uid(uid)
        if existing_case is not None and not payload.force:
            response.status_code = status.HTTP_200_OK
            return CaseSyncResponse(status="exists", case_id=existing_case.id)

        # 4. Уже есть незавершённая задача по этому УИД — отдаём её (без дублей).
        active_task = tasks.get_active_by_uid(uid)
        if active_task is not None:
            return CaseSyncResponse(status="processing", task_id=active_task.id)

        # 5. Иначе создаём новую задачу (id сохраняем до закрытия сессии).
        task_id = tasks.create(uid).id

    # 6. Ставим фоновую обработку в срочную очередь и отвечаем id задачи.
    sync_case.apply_async(args=[task_id], queue="urgent")
    return CaseSyncResponse(status="processing", task_id=task_id)


@router.get("/tasks/{task_id}", response_model=SearchTaskResponse)
def get_search_task(task_id: int) -> SearchTaskResponse:
    """Вернуть текущее состояние задачи синхронизации по её id."""
    with session_scope() as session:
        # Достаём задачу; если её нет — 404.
        task = SearchTaskRepository(session).get(task_id)
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
        # Собираем ответ вручную (пока атрибуты доступны в открытой сессии).
        return SearchTaskResponse(
            task_id=task.id,
            uid=task.uid,
            status=task.status,
            case_id=task.case_id,
            attempts=task.attempts,
            last_error=task.last_error,
        )
