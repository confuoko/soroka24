"""Пакет настроек client.

Celery импортируем здесь, чтобы @shared_task в apps/*/tasks.py привязывались к
нашему приложению, а не к дефолтному.
"""
from config.celery_app import celery_app

__all__ = ["celery_app"]
