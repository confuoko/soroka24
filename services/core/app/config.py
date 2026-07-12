"""Чтение конфигурации из переменных окружения.

Пока — простые модульные константы (без классов и без бизнес-логики).
Позже здесь можно перейти на pydantic-settings, когда появятся настройки БД и т.п.
"""
import os

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

# Подключение к PostgreSQL (SQLAlchemy). В docker хост — postgres, локально — localhost.
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg2://soroka:soroka@localhost:5432/soroka"
)

# Доступ к админке SQLAdmin (логин/пароль и секрет для сессии-cookie).
# Дефолты — только для локальной разработки, в проде задать через env!
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "dev-admin-secret-change-me")
