"""Общие фикстуры тестов.

Тесты репозиториев идут на настоящем Postgres (тот же DATABASE_URL, что у сервиса).
Причина: проверять надо в том числе поведение UNIQUE-индексов на flush, а на SQLite
схему не поднять — OutboxEvent.payload объявлен как JSONB.

Каждый тест выполняется внутри транзакции, которая в конце откатывается, поэтому
в БД после прогона ничего не остаётся.
"""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import Court, CourtLevel, engine
from app.monitoring import tasks


@pytest.fixture
def court(session) -> Court:
    """Суд для карточки дела: без него дело завести нельзя (court_id NOT NULL).

    Берём из справочника, если он уже залит (в рабочей базе там ~7700 судов), иначе
    заводим свой — тесты не должны зависеть от того, накатывали ли справочник.
    """
    code = "77MS0002"
    existing = session.scalar(select(Court).where(Court.code == code))
    if existing is not None:
        return existing

    row = Court(
        code=code,
        # Номер участка в названии и хост в адресе — то, по чему определяется суд дела
        # (см. CourtRepository.get_by_participok / get_by_host).
        name="Судебный участок № 2",
        level=CourtLevel.MIRSUD,
        region="Город Москва",
        timezone="Europe/Moscow",
        base_url="http://mos-sud.ru/ms/2",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    """Отвязать задачу sync_case от пула прокси: считаем, что идём напрямую.

    Без этого любой тест задачи падал бы на ProxyUnavailable (в тестовой БД пул пуст,
    а COURT_PROXY_REQUIRED=1). Сам пул проверяется отдельно, в test_proxy_pool.py.
    """
    monkeypatch.setattr(tasks, "lease_proxy", lambda portal=None: None)


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
