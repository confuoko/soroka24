"""Celery-таски мониторинга.

sync_case — синхронизация дела по УИД: сходить браузером в суд, найти карточку,
разобрать её и создать/обновить Case. Тяжёлая часть (Chromium) вынесена из API в
фоновую задачу; API лишь ставит задачу и отдаёт её id.

enqueue_case_resync — поставить дело на повторный парсинг, зная только его id в БД
(sync_case принимает id задачи, а не дела).

Каждый вызов парсинга оставляет след: HTML страницы ложится снапшотом в S3, а в
Case.diff_history дозаписывается запись о том, что изменилось — в том числе когда
изменений нет и когда сайт суда не открылся.
"""
from datetime import datetime

from celery.exceptions import Retry
from celery.utils.log import get_task_logger

from app.browser import lease_proxy
from app.celery_app import celery_app
from app.config import HTML_SNAPSHOT_ENABLED
from app.courts import (
    CaseNotFound,
    NewCourtException,
    UnsupportedCourt,
    define_court_by_uid,
)
from app.models.database import Case, session_scope
from app.monitoring.case_update import CaseChanges, update_case
from app.monitoring.parse_history import (
    STATUS_CHANGED,
    STATUS_FETCH_ERROR,
    STATUS_NO_CHANGES,
    STATUS_PARSE_ERROR,
    append_parse_entry,
    build_entry,
    changes_to_dict,
    last_entry,
)
from app.repositories import CaseRepository, CourtRepository, SearchTaskRepository
from app.storage import is_failure_key, save_snapshot, snapshot_sha256

logger = get_task_logger(__name__)


def _log_changes(uid: str, changes: CaseChanges) -> None:
    """Записать в лог, что изменилось по делу за эту синхронизацию."""
    if not changes.has_changes():
        logger.info("Дело %s: изменений нет", uid)
        return
    for change in changes.field_changes:
        logger.info(
            "Изменилось поле дела %s: %s: %r -> %r", uid, change.field, change.old, change.new
        )
    for event in changes.new_events:
        logger.info("Новое событие по делу %s: %s — %s", uid, event.event_date, event.state_description)
    for event in changes.updated_events:
        logger.info("Изменён документ события по делу %s: %s — %s", uid, event.event_date, event.state_description)
    for event in changes.removed_events:
        logger.info("Удалено событие по делу %s: %s — %s", uid, event.event_date, event.state_description)
    for place in changes.new_places:
        logger.info("Новое местонахождение по делу %s: %s — %s", uid, place.place_date, place.place_description)
    for place in changes.updated_places:
        logger.info("Изменён комментарий местонахождения по делу %s: %s — %s", uid, place.place_date, place.place_description)
    for place in changes.removed_places:
        logger.info("Удалено местонахождение по делу %s: %s — %s", uid, place.place_date, place.place_description)
    for session in changes.new_sessions:
        logger.info("Назначено заседание по делу %s: %s — %s", uid, session.session_date, session.stage)
    for session in changes.updated_sessions:
        logger.info(
            "Изменено заседание по делу %s: %s — %s (результат: %s)",
            uid, session.session_date, session.stage, session.result,
        )
    for session in changes.removed_sessions:
        logger.info("Снято заседание по делу %s: %s — %s", uid, session.session_date, session.stage)
    for document in changes.new_documents:
        logger.info("Новый документ по делу %s: %s — %s", uid, document.document_date, document.document_type)
    for document in changes.removed_documents:
        logger.info("Удалён документ по делу %s: %s — %s", uid, document.document_date, document.document_type)
    for judge in changes.added_judges:
        logger.info("Привязан судья: %s к делу %s", judge.full_name, uid)
    for judge in changes.removed_judges:
        logger.info("Отвязан судья: %s от дела %s", judge.full_name, uid)
    for side in changes.added_sides:
        logger.info("Привязана сторона: %s (%s) к делу %s", side.full_name, side.type.value, uid)
    for side in changes.removed_sides:
        logger.info("Отвязана сторона: %s (%s) от дела %s", side.full_name, side.type.value, uid)


def _take_snapshot(uid: str, html: str, fetched_at: datetime) -> tuple[dict | None, bool]:
    """Положить HTML страницы в S3. Возвращает (данные снапшота, html_unchanged).

    Если разметка побайтово совпала с прошлым разом (тот же sha256), новый объект не
    заливаем — переиспользуем ключ предыдущей записи истории. Это частый случай: дело
    проверяется регулярно, а меняется редко.

    Недоступный S3 не должен ронять парсинг: дело важнее архива разметки, поэтому
    ошибку заливки только логируем, а разбор продолжается со snapshot=None.
    """
    if not HTML_SNAPSHOT_ENABLED:
        return None, False

    sha = snapshot_sha256(html)

    # Короткое чтение: чем закончился предыдущий парсинг этого дела.
    with session_scope() as session:
        case = CaseRepository(session).get_by_uid(uid)
        previous = last_entry(case) if case is not None else None

    # Ключ переиспользуем только от УСПЕШНОГО парсинга: в последней записи истории теперь
    # может лежать ключ страницы отказа (капча/блокировка), и подставлять его карточке
    # нельзя — история дела стала бы ссылаться на мусор.
    previous_key = previous.get("html_key") if previous is not None else None
    if previous_key and not is_failure_key(previous_key) and previous.get("html_sha256") == sha:
        return (
            {
                "html_bucket": previous.get("html_bucket"),
                "html_key": previous["html_key"],
                "html_sha256": sha,
                "html_size": previous.get("html_size"),
            },
            True,
        )

    try:
        return save_snapshot(uid, html, fetched_at), False
    except Exception as exc:
        logger.warning("Не удалось сохранить снапшот HTML дела %s в S3: %s", uid, exc)
        return None, False


def _record_parse_entry(
    uid: str,
    status: str,
    fetched_at: datetime,
    task_id: int,
    snapshot: dict | None = None,
    error: str | None = None,
    html_unchanged: bool = False,
) -> None:
    """Дозаписать в историю дела запись о неудачном парсинге (если дело уже есть в БД).

    Если дела в БД ещё нет (первый парсинг провалился), дозаписывать некуда — такой
    провал остаётся в SearchTask (last_error/status).
    """
    with session_scope() as session:
        case = CaseRepository(session).get_by_uid(uid)
        if case is None:
            logger.info("Дело %s ещё не создано — запись истории парсинга пропущена", uid)
            return
        append_parse_entry(
            case,
            build_entry(
                status=status,
                fetched_at=fetched_at,
                task_id=task_id,
                snapshot=snapshot,
                error=error,
                html_unchanged=html_unchanged,
            ),
        )


def _save_failure_page(
    uid: str, exc: BaseException, fetched_at: datetime
) -> tuple[dict | None, int | None]:
    """Положить в S3 страницу, на которой упали. Возвращает (данные снапшота, HTTP-статус).

    Снимок приходит приложенным к исключению клиента суда (CourtError.page): живой браузер
    есть только внутри клиента, здесь его уже нет. Если снимка нет (упало до открытия
    страницы или снять не удалось) — сохранять нечего.

    Ошибку S3 глотаем: архив разметки не важнее самой причины отказа, ради записи которой
    мы сюда и пришли.
    """
    page = getattr(exc, "page", None)
    if page is None:
        return None, None
    if not HTML_SNAPSHOT_ENABLED:
        return None, page.status
    try:
        return save_snapshot(uid, page.html, fetched_at, failed=True), page.status
    except Exception as storage_exc:
        logger.warning(
            "Не удалось сохранить страницу отказа дела %s в S3: %s", uid, storage_exc
        )
        return None, page.status


def _record_error(task_id: int, error: str, page_status=None) -> None:
    """Записать ошибку в задачу, не меняя статус (попытки ещё могут остаться)."""
    with session_scope() as session:
        task = SearchTaskRepository(session).get(task_id)
        if task is not None:
            task.last_error = error
            if page_status is not None:
                task.page_status = page_status


def _mark_failed(task_id: int, error: str, page_status: int | None = None) -> None:
    """Пометить задачу окончательно проваленной."""
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is not None:
            repo.mark_failed(task, error, page_status=page_status)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_case(self, task_id: int) -> None:
    """Обработать задачу поиска: найти дело по УИД и сохранить Case.

    Тело — в _sync_case; здесь только страховка на непредвиденную ошибку. Без неё любое
    исключение, не перечисленное в _sync_case (IntegrityError на коммите дела, недоступная
    БД внутри записи истории), оставляло бы задачу в RUNNING навсегда: терминальный статус
    ставится только явными вызовами, и никто её потом не подберёт. Хуже того, RUNNING
    считается активным статусом (SearchTaskRepository.get_active_by_uid), поэтому такая
    задача навсегда блокирует повторный запрос этого УИД через API.
    """
    try:
        _sync_case(self, task_id)
    except Retry:
        # Штатный ретрай (self.retry бросает Retry — наследника Exception): задача вернётся,
        # статус RUNNING сохраняется осознанно. Без этой ветки каждый ретрай уходил бы в FAILED.
        raise
    except Exception as exc:
        _mark_failed(task_id, f"Непредвиденная ошибка: {exc}")
        raise  # пробрасываем дальше, чтобы трейс остался в логах воркера


def _sync_case(celery_task, task_id: int) -> None:
    """Тело задачи синхронизации. celery_task — сам таск (нужен для retry)."""
    # 1. Помечаем задачу «в работе» (короткая транзакция) и берём УИД.
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is None:
            return  # задача удалена — делать нечего
        repo.mark_running(task)
        uid = task.uid

    # 2. Долгая часть без БД: сходить браузером в суд за HTML карточки.
    fetched_at = datetime.utcnow()
    try:
        # Прокси арендуем внутри try: если пул пуст при COURT_PROXY_REQUIRED=1,
        # ProxyUnavailable уйдёт в ветку временных ошибок ниже — задача поретраится,
        # а браузер даже не запустится (на портал не с того IP ходить нельзя).
        proxy = lease_proxy()
        logger.info("Дело %s: идём через прокси %s", uid, proxy or "напрямую")
        client = define_court_by_uid(uid, proxy=proxy)
        html = client.fetch_case_html(uid)
        fetched_at = datetime.utcnow()
    except (UnsupportedCourt, CaseNotFound) as exc:
        # Окончательные ошибки — повторять бессмысленно.
        snapshot, page_status = _save_failure_page(uid, exc, fetched_at)
        _record_parse_entry(
            uid, STATUS_FETCH_ERROR, fetched_at, task_id, snapshot=snapshot, error=str(exc)
        )
        _mark_failed(task_id, str(exc), page_status=page_status)
        return
    except Exception as exc:
        # Временная ошибка (403/timeout/сеть): записать и повторить, пока есть попытки.
        # Запись в историю делаем на каждой попытке — так видно, сколько раз суд не открылся.
        snapshot, page_status = _save_failure_page(uid, exc, fetched_at)
        _record_parse_entry(
            uid, STATUS_FETCH_ERROR, fetched_at, task_id, snapshot=snapshot, error=str(exc)
        )
        # Счётчик попыток проверяем САМИ, до вызова retry: если в retry(exc=...) передан exc,
        # то при исчерпании попыток Celery пробрасывает именно его, а не MaxRetriesExceededError
        # (celery/app/task.py: `if max_retries is not None and retries > max_retries` →
        # `raise_with_context(exc)`). Ловить MaxRetriesExceededError здесь бесполезно — эта
        # ветка не срабатывала, и задача оставалась в RUNNING с исчерпанными попытками.
        if celery_task.request.retries >= celery_task.max_retries:
            _mark_failed(task_id, f"Исчерпаны попытки: {exc}", page_status=page_status)
            return
        _record_error(task_id, str(exc), page_status=page_status)
        raise celery_task.retry(exc=exc, countdown=30)

    # 2a. Снапшот HTML — до разбора, чтобы разметка сохранилась даже если парсер упадёт.
    snapshot, html_unchanged = _take_snapshot(uid, html, fetched_at)

    # 2b. Разбор карточки. Ошибка разбора не временная (сломалась разметка или тип
    #     страницы неизвестен) — ретраить нечего, помечаем задачу проваленной.
    try:
        data = client.parse(html)  # состав словаря — в CaseParser.parse
    except Exception as exc:
        _record_parse_entry(
            uid, STATUS_PARSE_ERROR, fetched_at, task_id,
            snapshot=snapshot, error=str(exc), html_unchanged=html_unchanged,
        )
        _mark_failed(task_id, f"Не удалось разобрать страницу: {exc}")
        return

    # 3. Успех: привязать суд и создать/обновить Case со сверкой судей/сторон/событий.
    #    Суд резолвим ПЕРВЫМ: если его нет в БД — NewCourtException, транзакция
    #    откатывается и Case НЕ создаётся (заводить дело без суда не хотим).
    try:
        with session_scope() as session:
            # Код суда для мировых судов Москвы — первые 8 символов УИД (напр. 77MS0001).
            court = CourtRepository(session).get_by_code(uid[:8])
            if court is None:
                raise NewCourtException(uid[:8])

            changes = update_case(session, uid, data, court)
            case_id = changes.case.id
            _log_changes(uid, changes)

            # Историю пишем здесь же, до коммита: у удалённых событий и местонахождений
            # атрибуты в этот момент ещё загружены в сессии.
            append_parse_entry(
                changes.case,
                build_entry(
                    status=STATUS_CHANGED if changes.has_changes() else STATUS_NO_CHANGES,
                    fetched_at=fetched_at,
                    task_id=task_id,
                    snapshot=snapshot,
                    diff=changes_to_dict(changes),
                    html_unchanged=html_unchanged,
                ),
            )
    except NewCourtException as exc:
        # Новый суд — повторять бессмысленно, помечаем задачу проваленной.
        _mark_failed(task_id, f"Новый суд, требуется завести справочник: {exc}")
        return

    # Успех фиксируем отдельной транзакцией (первая уже закоммичена).
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        repo.mark_success(repo.get(task_id), case_id)


def enqueue_case_resync(case_id: int, queue: str = "regular") -> int | None:
    """Поставить дело на повторный парсинг по его id в БД.

    sync_case принимает id ЗАДАЧИ, а не дела, поэтому задачу надо сначала создать по
    УИД дела — этим и занимается функция. Возвращает id созданной задачи (по нему
    можно следить через GET /search_case/tasks/{task_id}) или None, если дела с таким
    id нет.

    Дедупликации по активным задачам здесь намеренно нет: задача, воркер которой умер
    жёстко, навсегда остаётся в статусе RUNNING, и такая проверка заблокировала бы
    ручной перезапуск дела совсем. Лишний парсинг — меньшее зло.

    Очередь по умолчанию regular: ручной прогон не должен вытеснять срочные запросы
    пользователей из urgent.
    """
    with session_scope() as session:
        case = session.get(Case, case_id)
        if case is None:
            logger.info("Дело id=%s не найдено — повторный парсинг не запущен", case_id)
            return None
        task_id = SearchTaskRepository(session).create(case.uid).id

    # apply_async только ПОСЛЕ коммита: иначе воркер может схватить задачу раньше,
    # чем строка появится в БД (тот же порядок, что в app/api/routes.py).
    sync_case.apply_async(args=[task_id], queue=queue)
    return task_id
