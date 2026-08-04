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
