"""Общие фикстуры тестов core_v2.

Тесты с БД идут на настоящий PostgreSQL: SQLite не подойдёт, потому что payload событий
outbox это JSONB, а пул прокси берёт строку через FOR UPDATE ... SKIP LOCKED.

Фикстура session работает во внешней транзакции с откатом: тест видит свои записи, но
после него в базе не остаётся ничего. Флаги сессии повторяют SessionLocal — иначе
поведение сверки поедет (см. app/database.py).
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.database import engine


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection, autoflush=False, expire_on_commit=False)
    try:
        yield db
    finally:
        db.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def court(session):
    """Суд для тестов: Москва, участок № 2, пояс Europe/Moscow.

    Берём существующую строку справочника, если она есть, иначе создаём свою — тесты не
    должны зависеть от того, накатывали ли справочник судов.

    Пояс задаётся явно и не случайно: по нему локальное время со страницы переводится в
    UTC при сохранении, а «Город Москва» — это ключ в TZ_BY_REGION (app/timezones.py).
    """
    from app.models import Court, CourtLevel

    existing = session.query(Court).filter(Court.code == "77MS0002").one_or_none()
    if existing is not None:
        return existing

    created = Court(
        code="77MS0002",
        # Номер участка в названии и хост в адресе — то, по чему определяется суд дела.
        name="Судебный участок № 2",
        level=CourtLevel.MIRSUD,
        region="Город Москва",
        timezone="Europe/Moscow",
        base_url="http://mos-sud.ru/ms/2",
    )
    session.add(created)
    session.flush()
    return created


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    """В тестах прокси не арендуем.

    Автоматически на весь набор: почти любой тест обхода иначе упёрся бы в
    ProxyUnavailable (при COURT_PROXY_REQUIRED=1 пустой пул — это отказ), и вместо
    проверяемого поведения тест сообщал бы про прокси.

    Патчим имя именно в app.services.discovery: обход зовёт его оттуда. Сама аренда
    (app/services/proxy_pool.py) при этом остаётся настоящей — её проверяет
    tests/test_proxy_pool.py.
    """
    from app.services import discovery

    monkeypatch.setattr(discovery, "lease_proxy", lambda **kwargs: None)
