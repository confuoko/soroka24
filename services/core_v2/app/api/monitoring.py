"""REST-роут управления списком дел на регулярном обходе.

Единственная ручка, через которую в core попадает знание извне: какие дела кому-то
интересны. Всё остальное core выясняет сам, сходив на портал.

Граница ответственности здесь ровно такая:

    клиентский сервис            core
    какие дела интересны   →     как и когда их обновлять

Поэтому ручка НЕ принимает ни интервал обхода, ни приоритет, ни пользователя: это уже
«как и когда», и решает это core (см. app/tasks.py, sync_monitored_cases).
"""
import logging

from fastapi import APIRouter, HTTPException, Query, status

from app.api.schemas import MonitoringCasesRequest, MonitoringCasesResponse
from app.database import session_scope
from app.repositories import CaseRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


@router.put("/cases", response_model=MonitoringCasesResponse)
def replace_monitored_cases(
    payload: MonitoringCasesRequest,
    force: bool = Query(
        default=False,
        description="Разрешить пустой список, то есть снять с мониторинга все дела.",
    ),
) -> MonitoringCasesResponse:
    """Привести список дел на мониторинге к присланному.

    Семантика — ЗАМЕЩЕНИЕ: после запроса на мониторинге ровно эти дела. Присланного нет в
    базе — сообщаем в unknown_ids, но запрос не отклоняем: одно исчезнувшее дело не повод
    не обновить остальные.

    Операция идемпотентна: повторный запрос с тем же списком вернёт added=0, removed=0.

    PUT, а не POST, именно из-за этого: метод описывает не «добавь», а «пусть будет так», и
    повторная отправка того же тела ничего не меняет.

    ## Почему пустой список требует force

    Пустой список — законное состояние («ни на что больше не подписаны»), но он же —
    самая вероятная форма аварии на стороне клиента: упавший запрос к своей БД, пустой
    queryset из-за опечатки в фильтре, миграция, потерявшая подписки. Разницы между
    «правда ни на что не подписаны» и «список не собрался» в теле запроса нет.

    Цена ошибки несимметрична. Лишний обход дела стоит одного похода на портал; снятое
    зря дело перестаёт обновляться МОЛЧА, и никто этого не замечает, пока пользователь не
    спросит, почему по делу третью неделю ничего нет. Поэтому пустой список без явного
    force отклоняется.
    """
    with session_scope() as session:
        repo = CaseRepository(session)

        if not payload.case_ids and not force:
            monitored = len(repo.list_monitored_ids())
            if monitored:
                logger.warning(
                    "Отклонён пустой список мониторинга: сейчас на нём %s дел. "
                    "Если это правда, повторите с ?force=true",
                    monitored,
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"empty case_ids would unmonitor {monitored} cases; "
                        "repeat with ?force=true if this is intended"
                    ),
                )

        result = repo.set_monitoring_list(payload.case_ids)

    # В лог — потому что это единственное место, где меняется состав регулярного обхода,
    # и по логу должно быть видно, кто и когда его поменял.
    logger.info(
        "Список мониторинга обновлён: на мониторинге %s, добавлено %s, снято %s, "
        "неизвестных id %s",
        result.monitored,
        result.added,
        result.removed,
        len(result.unknown_ids),
    )
    if result.unknown_ids:
        logger.warning(
            "В списке мониторинга есть id, которых нет в базе: %s", result.unknown_ids
        )

    return MonitoringCasesResponse(
        monitored=result.monitored,
        added=result.added,
        removed=result.removed,
        unknown_ids=result.unknown_ids,
    )
