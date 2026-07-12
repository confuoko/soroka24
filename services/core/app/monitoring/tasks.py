"""Celery-таски мониторинга.

sync_case — синхронизация дела по УИД: сходить браузером в суд, найти карточку,
разобрать её (пока заглушка) и создать/обновить Case. Тяжёлая часть (Chromium)
вынесена из API в фоновую задачу; API лишь ставит задачу и отдаёт её id.
"""
from app.celery_app import celery_app
from app.courts import CaseNotFound, UnsupportedCourt, define_court_by_uid
from app.models.database import session_scope
from app.repositories import CaseRepository, SearchTaskRepository


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

    # 3. Успех: создаём/обновляем Case и помечаем задачу выполненной (одной транзакцией).
    with session_scope() as session:
        case = CaseRepository(session).upsert_by_uid(uid, data)
        repo = SearchTaskRepository(session)
        repo.mark_success(repo.get(task_id), case.id)
