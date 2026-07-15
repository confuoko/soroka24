"""Celery-таски мониторинга.

sync_case — синхронизация дела по УИД: сходить браузером в суд, найти карточку,
разобрать её (пока заглушка) и создать/обновить Case. Тяжёлая часть (Chromium)
вынесена из API в фоновую задачу; API лишь ставит задачу и отдаёт её id.
"""
from celery.utils.log import get_task_logger

from app.celery_app import celery_app
from app.courts import (
    CaseNotFound,
    NewCourtException,
    UnsupportedCourt,
    define_court_by_uid,
)
from app.models.database import session_scope
from app.repositories import (
    CaseRepository,
    CourtRepository,
    JudgeRepository,
    SearchTaskRepository,
    SideRepository,
)

logger = get_task_logger(__name__)


def _record_error(task_id: int, error: str, page_status=None) -> None:
    """Записать ошибку в задачу, не меняя статус (попытки ещё могут остаться)."""
    with session_scope() as session:
        task = SearchTaskRepository(session).get(task_id)
        if task is not None:
            task.last_error = error
            if page_status is not None:
                task.page_status = page_status


def _mark_failed(task_id: int, error: str) -> None:
    """Пометить задачу окончательно проваленной."""
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is not None:
            repo.mark_failed(task, error)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def sync_case(self, task_id: int) -> None:
    """Обработать задачу поиска: найти дело по УИД и сохранить Case."""
    # 1. Помечаем задачу «в работе» (короткая транзакция) и берём УИД.
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        task = repo.get(task_id)
        if task is None:
            return  # задача удалена — делать нечего
        repo.mark_running(task)
        uid = task.uid

    # 2. Долгая часть без БД: сходить браузером в суд и разобрать карточку.
    try:
        client = define_court_by_uid(uid)
        html = client.fetch_case_html(uid)
        data = client.parse(html)  # пока заглушка -> {}
    except (UnsupportedCourt, CaseNotFound) as exc:
        # Окончательные ошибки — повторять бессмысленно.
        _mark_failed(task_id, str(exc))
        return
    except Exception as exc:
        # Временная ошибка (403/timeout/сеть): записать и повторить, пока есть попытки.
        _record_error(task_id, str(exc))
        try:
            raise self.retry(exc=exc, countdown=30)
        except self.MaxRetriesExceededError:
            _mark_failed(task_id, f"Исчерпаны попытки: {exc}")
            return

    # 3. Успех: привязать суд + судью и создать/обновить Case.
    #    Суд резолвим ПЕРВЫМ: если его нет в БД — NewCourtException, транзакция
    #    откатывается и Case НЕ создаётся (заводить дело без суда не хотим).
    try:
        with session_scope() as session:
            # Код суда для мировых судов Москвы — первые 8 символов УИД (напр. 77MS0001).
            court = CourtRepository(session).get_by_code(uid[:8])
            if court is None:
                raise NewCourtException(uid[:8])

            case = CaseRepository(session).upsert_by_uid(uid, data)
            if court not in case.courts:  # идемпотентно при повторной синхронизации
                case.courts.append(court)
                logger.info("Найден и привязан суд: %s (%s) к делу %s", court.name, court.code, uid)
            for judge in JudgeRepository(session).get_or_create_many(data.get("judge_names", [])):
                if judge not in case.judges:
                    case.judges.append(judge)
                    logger.info("Привязан судья: %s к делу %s", judge.full_name, uid)
            for side in SideRepository(session).get_or_create_many(data.get("sides", [])):
                if side not in case.sides:
                    case.sides.append(side)
                    logger.info(
                        "Привязана сторона: %s (%s) к делу %s",
                        side.full_name, side.type.value, uid,
                    )
            case_id = case.id
    except NewCourtException as exc:
        # Новый суд — повторять бессмысленно, помечаем задачу проваленной.
        _mark_failed(task_id, f"Новый суд, требуется завести справочник: {exc}")
        return

    # Успех фиксируем отдельной транзакцией (первая уже закоммичена).
    with session_scope() as session:
        repo = SearchTaskRepository(session)
        repo.mark_success(repo.get(task_id), case_id)
