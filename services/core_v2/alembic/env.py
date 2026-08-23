"""Окружение alembic для core_v2.

Адрес базы берётся из app.config, а не из alembic.ini: строка в ini оставлена
заглушкой, чтобы никто не правил её руками и не разъезжался с приложением.

История миграций СВОЯ, с нуля. 27 миграций старого core сюда не копируются: core_v2
работает с отдельной базой (soroka_core_v2), а alembic_version — однострочная таблица,
поэтому две независимые истории в одной базе невозможны. Первая ревизия описывает
целевое состояние моделей сразу, без monitoring-колонок.
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from app.config import DATABASE_URL  # адрес базы core_v2
from app.database import Base  # noqa: F401 — нужен для metadata

# Импорт моделей регистрирует их в Base.metadata: без этого autogenerate увидит пустую
# схему и предложит удалить все таблицы. Пакет появляется в Phase 4; пока его нет,
# метаданные пусты, и это нормально для скелета.
try:
    import app.models  # noqa: F401
except ModuleNotFoundError:
    pass

config = context.config

# Адрес базы — из конфига приложения.
config.set_main_option("sqlalchemy.url", DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Сгенерировать SQL, не подключаясь к базе (alembic upgrade --sql)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Накатить миграции на живую базу."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
