"""REST-роуты для работы с делами (Case) и задачами их синхронизации."""
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response, status

from app.api.schemas import CaseSyncRequest, CaseSyncResponse, SearchTaskResponse
from app.courts import UnsupportedCourt, define_court_by_uid, is_supported_url
from app.courts.msudrf import CASE_URL_EXAMPLE
from app.database import session_scope
from app.tasks import run_search_task
from app.repositories import CaseRepository, CourtRepository, SearchTaskRepository
from app.validators import (
    is_synthetic_uid,
    looks_like_url,
    normalize_uid,
    normalize_url,
    validate_uid,
    validate_url,
)

# Роутер с общим префиксом /search_case.
router = APIRouter(prefix="/search_case", tags=["search_case"])


@router.post("", response_model=CaseSyncResponse, status_code=status.HTTP_202_ACCEPTED)
def request_for_case_sync(payload: CaseSyncRequest, response: Response) -> CaseSyncResponse:
    """Принять УИД или ссылку на дело и запустить фоновую синхронизацию.

    Сюда приходят оба способа, потому что порталы устроены по-разному: у mos-sud.ru
    есть поиск по УИД, а у msudrf.ru и большинства региональных его нет — там дело
    доступно только по прямой ссылке. Что прислали, определяем по схеме адреса.

    В портал здесь НЕ ходим: поход занимает полминуты, требует прокси и разгадывания
    капчи. Всё это делает фоновая задача — эндпоинт только заводит её и сразу отдаёт
    task_id, по которому потом спрашивают состояние.
    """
    value = payload.query.strip()
    if not value:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return CaseSyncResponse(status="invalid_query")

    if looks_like_url(value):
        return _sync_by_url(normalize_url(value), payload.force, response)
    return _sync_by_uid(normalize_uid(value), payload.force, response)


def _sync_by_uid(uid: str, force: bool, response: Response) -> CaseSyncResponse:
    """Дело задано УИД: портал умеет искать по нему сам.

    Поиск заводится ВСЕГДА, даже если дела с этим УИД в БД уже есть. Причина в том, что
    УИД сквозной: найденные карточки могли прийти ссылками со страниц других инстанций,
    и ни одной карточки из мировых судов Москвы среди них может не быть. А если есть —
    рядом могло появиться ещё одно производство по тому же УИД (портал показывает их
    одной таблицей). Найденное отдаём вместе с id заведённой задачи.

    Поэтому force на эту ветку не влияет: перепарсинг здесь и так происходит каждый раз.
    """
    # 1. Проверяем формат УИД.
    if not validate_uid(uid):
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return CaseSyncResponse(status="invalid_uid")

    # 2. Определяем суд по префиксу УИД. Поиск по УИД есть только у части порталов
    #    (сейчас — у мировых судов Москвы), поэтому отказ здесь ещё не значит, что
    #    дело недоступно: чаще всего его просто надо прислать ссылкой.
    try:
        define_court_by_uid(uid)
    except UnsupportedCourt:
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return _explain_uid_not_searchable(uid)

    with session_scope() as session:
        cases = CaseRepository(session)
        tasks = SearchTaskRepository(session)

        # 3. Что по этому УИД уже есть в БД. Отдаём как есть, из любых судов: УИД
        #    сквозной, и эти карточки могли прийти со страниц других инстанций.
        #    Выходить по ним нельзя — см. докстринг.
        existing_cases = cases.list_by_uid(uid)
        case_ids = [case.id for case in existing_cases] or None
        newest_id = (
            max(existing_cases, key=lambda case: case.updated_at).id
            if existing_cases
            else None
        )

        # 4. Уже есть незавершённая задача по этому УИД — отдаём её (без дублей).
        active_task = tasks.get_active_by_uid(uid)
        if active_task is not None:
            return CaseSyncResponse(
                status="processing",
                task_id=active_task.id,
                case_id=newest_id,
                case_ids=case_ids,
            )

        # 5. Иначе создаём новую задачу (id сохраняем до закрытия сессии).
        task_id = tasks.create(uid=uid).id

    return _enqueue(task_id, case_id=newest_id, case_ids=case_ids)


def _explain_uid_not_searchable(uid: str) -> CaseSyncResponse:
    """Объяснить, почему по этому УИД дело не найти, и что делать вместо этого.

    Первые 8 символов УИД — код суда в справочнике, так что суд мы почти всегда можем
    назвать по имени, даже когда искать по УИД у него нельзя. Без такого пояснения
    ответ выглядел бы как «суд не поддержан», хотя дело прекрасно достаётся ссылкой.

    Это единственное место, где по УИД ищется суд, и здесь это допустимо: имя нужно
    только для текста ошибки. Суд КАРТОЧКИ так определять нельзя — для этого есть номер
    участка из таблицы результатов и хост ссылки (см. app/services/discovery.py).
    """
    code = uid[:8]
    with session_scope() as session:
        court = CourtRepository(session).get_by_code(code)
        # Забираем название до закрытия сессии.
        court_name = court.name if court is not None else None

    if court_name is None:
        return CaseSyncResponse(
            status="unsupported_court",
            message=f"Суд с кодом {code} не найден в справочнике судов.",
        )

    return CaseSyncResponse(
        status="link_required",
        message=(
            f"{court_name}: поиск по УИД на этом портале не поддерживается. "
            f"Пришлите ссылку на карточку дела, например {CASE_URL_EXAMPLE}"
        ),
    )


def _sync_by_url(url: str, force: bool, response: Response) -> CaseSyncResponse:
    """Дело задано ссылкой: поиска по УИД у портала нет, карточка открывается напрямую.

    УИД станет известен только внутри задачи, когда она получит страницу, поэтому и
    дело, и активную задачу ищем по самой ссылке.
    """
    # 1. Проверяем, что это вообще адрес.
    if not validate_url(url):
        response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
        return CaseSyncResponse(
            status="invalid_query", message="Это не похоже на адрес карточки дела."
        )

    host = (urlsplit(url).hostname or "").lower()

    with session_scope() as session:
        cases = CaseRepository(session)
        tasks = SearchTaskRepository(session)

        # 2. Первым делом — есть ли такой суд в справочнике. Именно первым: если суда нет,
        #    неважно, умеем ли мы работать с его порталом — карточку всё равно не к чему
        #    привязать. Суд определяется по ссылке и известен уже здесь, до похода
        #    на портал (а поход занимает полминуты и стоит капчи).
        #    Именно get_by_url, а не get_by_host: у порталов с одним хостом на весь
        #    регион (Петербург) суд ищется по номеру участка из пути. Тем же методом
        #    его определяет и задача синхронизации — иначе она отказала бы после того,
        #    как эндпоинт задачу уже принял.
        court = CourtRepository(session).get_by_url(url)
        if court is None:
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return CaseSyncResponse(
                status="unsupported_court",
                message=f"Суда с сайтом {host} нет в справочнике судов.",
            )
        # Название забираем сразу: за пределами session_scope объекта уже не будет.
        court_name = court.name

        # 3. Суд нашёлся — умеем ли мы открывать его портал? Клиент есть пока только к
        #    движку msudrf.ru, а в справочнике 85 регионов со своими порталами.
        if not is_supported_url(url):
            response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return CaseSyncResponse(
                status="unsupported_court",
                message=f"{court_name}: портал {host} пока не поддержан.",
            )

        # 4. Карточка с этой ссылкой уже разобрана — отдаём её id (адрес уникален).
        existing_case = cases.get_by_url(url)
        if existing_case is not None and not force:
            response.status_code = status.HTTP_200_OK
            return CaseSyncResponse(
                status="exists",
                case_id=existing_case.id,
                case_ids=[existing_case.id],
            )

        # 5. По этой же ссылке уже идёт задача — отдаём её, дубль не заводим.
        active_task = tasks.get_active_by_url(url)
        if active_task is not None:
            return CaseSyncResponse(status="processing", task_id=active_task.id)

        task_id = tasks.create(source_url=url).id

    return _enqueue(task_id)


def _enqueue(
    task_id: int,
    case_id: int | None = None,
    case_ids: list[int] | None = None,
) -> CaseSyncResponse:
    """Поставить задачу в срочную очередь и вернуть её id.

    case_id/case_ids — то, что по этому делу уже есть в БД на момент постановки задачи
    (у ветки по УИД такое бывает: поиск заводится независимо от найденного).
    """
    # apply_async только ПОСЛЕ коммита: иначе воркер может схватить задачу раньше,
    # чем строка появится в БД.
    run_search_task.apply_async(args=[task_id], queue="urgent")
    return CaseSyncResponse(
        status="processing", task_id=task_id, case_id=case_id, case_ids=case_ids
    )


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
            # Самодельный ключ карточки (у дел без УИД на портале) наружу не отдаём —
            # как и в карточке дела, см. Case.public_uid.
            uid=None if task.uid and is_synthetic_uid(task.uid) else task.uid,
            source_url=task.source_url,
            status=task.status,
            case_id=task.case_id,
            attempts=task.attempts,
            last_error=task.last_error,
        )
