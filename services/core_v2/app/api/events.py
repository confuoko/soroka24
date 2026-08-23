"""REST-роут чтения событий об изменениях по делу.

Отдельный модуль, потому что это ЧТЕНИЕ потока изменений, а не работа с карточкой:
у него своя семантика курсора и свой потребитель — тот сервис, который решает, кому что
сообщать.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.database import session_scope
from app.models import Case
from app.repositories import OutboxEventRepository

router = APIRouter(prefix="/cases", tags=["events"])


class CaseEventOut(BaseModel):
    """Одно обнаруженное изменение по делу.

    Полей доставки здесь нет и не будет: core не знает ни пользователей, ни каналов.
    Строка означает «на портале это изменилось», а не «кому-то надо это отправить».
    """

    id: int
    event_type: str
    payload: dict
    # Момент обнаружения. Он же курсор: с ним приходят за следующей порцией.
    created_at: datetime


@router.get("/{case_id}/events", response_model=list[CaseEventOut])
def list_case_events(
    case_id: int,
    since: datetime | None = Query(
        default=None,
        description="Отдать события, обнаруженные ПОЗЖЕ этого момента. "
        "Пусто — с самого начала.",
    ),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[CaseEventOut]:
    """Изменения по делу, обнаруженные после указанного момента.

    Так читающий сервис забирает то, чего ещё не видел: он помнит момент последнего
    показанного события и приходит с ним снова. Сравнение строгое, поэтому повторный
    вызов с этим моментом не отдаст то же событие второй раз.

    В старом core такого эндпоинта не было вовсе: события писались, но прочитать их можно
    было только глазами в админке.

    404, если дела с таким id нет: пустой список означал бы «изменений не было», а это
    другое утверждение.
    """
    # Пусто — значит «всё с начала»: момент заведомо раньше любого события.
    since = since or datetime.min.replace(tzinfo=timezone.utc)

    with session_scope() as session:
        if session.get(Case, case_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="case not found"
            )
        rows = OutboxEventRepository(session).list_since(case_id, since, limit=limit)
        # Собираем ответ ВНУТРИ сессии: после выхода из неё строки отвязаны.
        return [
            CaseEventOut(
                id=row.id,
                event_type=row.event_type.value,
                payload=row.payload,
                created_at=row.created_at,
            )
            for row in rows
        ]
