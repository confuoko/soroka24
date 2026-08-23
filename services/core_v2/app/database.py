"""Подключение к БД: engine, сессии, базовый класс моделей.

Здесь только инфраструктура. Сами модели лежат в app/models/ — так видно, где кончается
«как мы говорим с базой» и начинается «что мы в ней храним». В старом core и то и другое
жило в одном файле на 857 строк.

Транзакциями управляет ТОЛЬКО session_scope. Репозитории принимают готовую сессию и
никогда не коммитят сами: иначе нельзя было бы записать изменения дела и события outbox
одной транзакцией, а это ключевое свойство (см. app/outbox.py).
"""
from contextlib import contextmanager

from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

# engine — единая точка соединения с PostgreSQL, внутри держит пул соединений.
# pool_pre_ping: проверять соединение перед выдачей. Воркеры живут часами, а сервер
# рвёт простаивающие соединения — без пинга первая задача после простоя падала бы.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# autoflush=False: сверка сама решает, когда отправлять запросы в базу; неявный flush
#   посреди сравнения «что на странице против того, что в базе» ломал бы это сравнение.
# expire_on_commit=False: после коммита объекты остаются читаемыми. Иначе обращение к
#   удалённой строке после коммита ушло бы в базу за уже несуществующей записью.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope():
    """Сессия БД как контекст-менеджер: commit при успехе, rollback при ошибке.

    Единственное место, где происходит commit. Всё, что должно попасть в базу вместе,
    делается внутри ОДНОГО session_scope.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Тип для ВСЕХ моментов времени: timestamptz, значение всегда в UTC.
#
# Naive timestamp тут не годится. Момент пишут двое — питон (datetime.now(timezone.utc))
# и сам Postgres (func.now()), — и у naive-колонки они совпадают лишь до тех пор, пока у
# контейнера БД не задан TZ: стоит его выставить, и колонки молча разъедутся на часы.
#
# ВАЖНО: это про МОМЕНТЫ. Календарные даты (дата поступления дела, дата документа) —
# по-прежнему Date: у них нет времени, и приписывать им полночь значит выдумывать момент,
# которого не было. См. app/timezones.py.
UTC_DATETIME = DateTime(timezone=True)


class Base(DeclarativeBase):
    """Базовый класс всех моделей. По его metadata alembic строит миграции."""
