"""Общие фикстуры тестов.

Тесты репозиториев идут на настоящем Postgres (тот же DATABASE_URL, что у сервиса).
Причина: проверять надо в том числе поведение UNIQUE-индексов на flush, а на SQLite
схему не поднять — Case.diff_history объявлен как JSONB.

Каждый тест выполняется внутри транзакции, которая в конце откатывается, поэтому
в БД после прогона ничего не остаётся.
"""
import pytest
from sqlalchemy.orm import Session

from app.models.database import engine
from app.monitoring import tasks


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    """Отвязать задачу sync_case от пула прокси: считаем, что идём напрямую.

    Без этого любой тест задачи падал бы на ProxyUnavailable (в тестовой БД пул пуст,
    а COURT_PROXY_REQUIRED=1). Сам пул проверяется отдельно, в test_proxy_pool.py.
    """
    monkeypatch.setattr(tasks, "lease_proxy", lambda: None)


@pytest.fixture
def session():
    """Сессия в внешней транзакции: всё, что тест записал, откатывается после него."""
    connection = engine.connect()
    transaction = connection.begin()
    # autoflush=False и expire_on_commit=False — как в SessionLocal сервиса, чтобы тесты
    # видели то же поведение сессии, что и рабочий код.
    db = Session(bind=connection, autoflush=False, expire_on_commit=False)
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()
