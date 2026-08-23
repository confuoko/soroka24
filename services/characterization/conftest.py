"""Общая обвязка characterization-тестов.

Эти тесты живут ОТДЕЛЬНО от services/core и импортируют его как reference: правило
миграции №1 запрещает менять services/core, а добавление файлов в его tests/ — это тоже
изменение. Поэтому каталог свой, а пакет `app` подтягивается через sys.path.

Запуск (из корня репозитория):

    .venv310\\Scripts\\python.exe -m pytest services/characterization -q

Тесты, которым нужна БД, помечены маркером `db` (см. pytest.ini). Пропустить их:

    ... -m "not db"
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

CHARACTERIZATION_DIR = Path(__file__).resolve().parent
CORE_DIR = CHARACTERIZATION_DIR.parent / "core"
HTML_DIR = CORE_DIR / "html_examples"
GOLDEN_DIR = CHARACTERIZATION_DIR / "golden"

# Пакет `app` старого core. Вставляем в начало пути: в core_v2 появится пакет с тем же
# именем, и характеризационные тесты обязаны видеть именно старую реализацию.
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


def html_files() -> list[Path]:
    """Все сохранённые страницы судов, в стабильном (алфавитном) порядке."""
    return sorted(HTML_DIR.glob("*.html"))


def read_html(name: str) -> str:
    """Прочитать фикстуру по имени файла."""
    return (HTML_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def html_dir() -> Path:
    return HTML_DIR


@pytest.fixture(scope="session")
def golden_dir() -> Path:
    return GOLDEN_DIR


@pytest.fixture
def session():
    """Сессия БД во внешней транзакции с откатом — как в tests/conftest.py старого core.

    Своя копия, а не импорт: та фикстура живёт внутри services/core, а мы этот каталог
    не трогаем. Флаги сессии повторяют SessionLocal (autoflush=False,
    expire_on_commit=False) — иначе поведение сверки поедет.
    """
    from sqlalchemy.orm import Session

    from app.models.database import engine

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
    """Суд для тестов сверки: Москва, участок № 2, пояс Europe/Moscow.

    Берём существующую строку справочника, если она есть, иначе создаём свою. Пояс
    задаём явно: он участвует в переводе локального времени в UTC.
    """
    from app.models.database import Court, CourtLevel

    existing = session.query(Court).filter(Court.code == "77MS0002").one_or_none()
    if existing is not None:
        return existing

    created = Court(
        code="77MS0002",
        # Номер участка в названии и хост в адресе — то, по чему определяется суд дела.
        name="Судебный участок № 2",
        level=CourtLevel.MIRSUD,
        # Именно "Город Москва": это ключ в TZ_BY_REGION (app/timezones.py).
        region="Город Москва",
        timezone="Europe/Moscow",
        base_url="http://mos-sud.ru/ms/2",
    )
    session.add(created)
    session.flush()
    return created
