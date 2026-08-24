"""REST-роуты чтения дел: отдать всю информацию по делу по его id в БД.

Отдельный модуль, потому что роутер в app/api/routes.py живёт под префиксом
/search_case и относится к запуску синхронизации, а не к чтению карточки.
"""
from fastapi import APIRouter, HTTPException, Query, status

from app.api.schemas import CaseDetailResponse, CaseSummaryResponse
from app.database import session_scope
from app.repositories import CaseRepository

router = APIRouter(prefix="/cases", tags=["cases"])

# Сколько дел отдаём одним запросом. Ограничение есть, потому что ids едут в строке
# запроса, а она у прокси и серверов не бесконечна.
MAX_SUMMARY_IDS = 500


@router.get("", response_model=list[CaseSummaryResponse])
def list_case_summaries(
    ids: str = Query(
        description="id дел через запятую, например 10,17,481.",
        examples=["10,17,481"],
    ),
) -> list[CaseSummaryResponse]:
    """Витрины сразу нескольких дел — одним запросом.

    Ради страницы «мои дела» клиентского сервиса: у него список дел свой, и без этой
    ручки он собирал бы страницу из N последовательных вызовов /cases/{id}/summary. То
    есть N+1, только по сети, где каждый шаг стоит не миллисекунды, а десятки.

    Путь именно /cases, а не /cases/summary: второй перехватило бы роутом /cases/{case_id}
    ниже, и запрос упал бы с 422 на попытке разобрать «summary» как int.

    Отсутствующие id молча не попадают в ответ, и 404 здесь не место: это список, а не
    карточка, и «одного из тридцати дел уже нет» — не причина не показать остальные
    двадцать девять. Заметить пропажу клиент может сам, сравнив длину.

    Порядок ответа — по возрастанию id, а не такой, в каком id прислали: клиент всё равно
    раскладывает витрины по своим подпискам, а обещать порядок значило бы запретить себе
    отдавать их одним SQL.
    """
    case_ids = _parse_ids(ids)
    if not case_ids:
        return []

    with session_scope() as session:
        cases = CaseRepository(session).list_summaries(case_ids)
        # Собираем ответ, пока сессия ещё открыта: суд подтянут заранее, но выйти из
        # сессии до сборки значило бы полагаться на это молчаливо.
        return [CaseSummaryResponse.model_validate(case) for case in cases]


def _parse_ids(raw: str) -> list[int]:
    """Разобрать «10,17,481» в список id. Мусор — 422, а не молчаливый пропуск.

    Молчаливо игнорировать нечисло нельзя: клиент увидел бы короткий список и решил, что
    дела удалены, а на самом деле у него поехала сборка строки запроса.
    """
    parts = [part.strip() for part in raw.split(",")]
    try:
        parsed = [int(part) for part in parts if part]
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ids must be a comma-separated list of integers",
        )

    unique = sorted(set(parsed))
    if len(unique) > MAX_SUMMARY_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"at most {MAX_SUMMARY_IDS} ids per request",
        )
    return unique


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

    Для списка дел нужны только эти поля, а полная карточка тянет все события,
    заседания и документы — на каждое дело в списке это лишние сотни килобайт по сети.
    Ради этого в CaseRepository и есть отдельный get_with_court.
    """
    with session_scope() as session:
        case = CaseRepository(session).get_with_court(case_id)
        if case is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="case not found")
        return CaseSummaryResponse.model_validate(case)

