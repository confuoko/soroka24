"""Чтение конфигурации из переменных окружения.

Пока — простые модульные константы (без классов и без бизнес-логики).
Позже здесь можно перейти на pydantic-settings, когда появятся настройки БД и т.п.
"""
import os

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
