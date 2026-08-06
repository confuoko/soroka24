"""Тесты статусной машины задачи sync_case: задача не должна «залипать» в RUNNING.

К сайту суда не ходим: define_court_by_uid подменяется заглушкой, которая сразу бросает
ошибку получения страницы. Chromium и сеть тестам не нужны.

Работают на настоящем Postgres, потому что _sync_case открывает свои сессии через
session_scope() и коммитит их — подменить это внешней транзакцией нельзя. Созданную
строку search_task фикстура удаляет за собой.
"""
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry

from app.models.database import SearchStatus, SearchTask, session_scope
from app.monitoring import tasks
from app.repositories import SearchTaskRepository

CASE_UID = "77MS0002-01-2026-000003-33"


class _StubTask:
    """Мини-подмена Celery-таска: нужны только счётчик попыток, лимит и retry()."""

    max_retries = 3

    def __init__(self, retries: int) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.retry_called = False

    def retry(self, exc=None, countdown=None):
        self.retry_called = True
        return Retry()


@pytest.fixture
def task_id():
    """Реальная строка search_task в PENDING; после теста удаляется."""
    with session_scope() as session:
        created = SearchTaskRepository(session).create(CASE_UID)
        created_id = created.id

    yield created_id

    with session_scope() as session:
        row = session.get(SearchTask, created_id)
        if row is not None:
            session.delete(row)


@pytest.fixture
def court_is_down(monkeypatch):
    """Портал не открывается: любой поход за страницей падает по таймауту."""

    def _boom(uid: str, proxy=None):
        raise TimeoutError('Page.fill: Timeout 30000ms exceeded.')

    monkeypatch.setattr(tasks, "define_court_by_uid", _boom)


def _status(task_id: int) -> tuple[SearchStatus, str | None, int]:
    with session_scope() as session:
        row = session.get(SearchTask, task_id)
        return row.status, row.last_error, row.attempts


def test_failed_when_retries_exhausted(task_id, court_is_down) -> None:
    """Попытки исчерпаны → FAILED.

    Регресс на причину A: Celery при исчерпании попыток пробрасывает исходный exc, а не
    MaxRetriesExceededError, поэтому прежний `except self.MaxRetriesExceededError` не
    срабатывал и задача навсегда оставалась в RUNNING.
    """
    stub = _StubTask(retries=_StubTask.max_retries)

    tasks._sync_case(stub, task_id)

    status, last_error, _ = _status(task_id)
    assert status is SearchStatus.FAILED
    assert last_error.startswith("Исчерпаны попытки")
    assert stub.retry_called is False


def test_retries_while_attempts_left(task_id, court_is_down) -> None:
    """Попытки ещё есть → штатный ретрай, статус остаётся RUNNING."""
    stub = _StubTask(retries=0)

    with pytest.raises(Retry):
        tasks._sync_case(stub, task_id)

    status, last_error, attempts = _status(task_id)
    assert status is SearchStatus.RUNNING
    assert "Timeout" in last_error
    assert attempts == 1
    assert stub.retry_called is True


def test_unexpected_error_closes_task(task_id, monkeypatch) -> None:
    """Непредвиденная ошибка в теле задачи → FAILED, а не вечный RUNNING.

    Регресс на причину B: IntegrityError на коммите дела вылетал наружу, минуя все
    перечисленные обработчики, и задача оставалась в RUNNING, блокируя УИД в API.
    """

    def _boom(celery_task, task_id_arg):
        raise ValueError("что-то неучтённое")

    monkeypatch.setattr(tasks, "_sync_case", _boom)

    result = tasks.sync_case.apply(args=[task_id])

    assert result.failed()
    status, last_error, _ = _status(task_id)
    assert status is SearchStatus.FAILED
    assert last_error.startswith("Непредвиденная ошибка")


def test_retry_is_not_swallowed_by_the_guard(task_id, monkeypatch) -> None:
    """Обёртка не должна принимать штатный ретрай за провал.

    Retry — наследник Exception, поэтому без отдельной ветки `except Retry: raise`
    каждая повторная попытка помечала бы задачу FAILED.
    """

    def _retry(celery_task, task_id_arg):
        raise Retry()

    monkeypatch.setattr(tasks, "_sync_case", _retry)

    tasks.sync_case.apply(args=[task_id])

    status, _, _ = _status(task_id)
    assert status is SearchStatus.PENDING  # статус не тронут: фикстура создала PENDING
