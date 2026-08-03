"""Celery-таски справочника судов.

sync_courts_from_json — залить/обновить суды из JSON-справочника (data/courts.json).
Вынесено в фоновую задачу, потому что справочник большой (~7700 записей) и синхронный
проход по нему из админки заблокировал бы event loop uvicorn на несколько секунд.
"""
import json
from pathlib import Path

from celery.utils.log import get_task_logger

from app.celery_app import celery_app
from app.config import COURTS_JSON_PATH
from app.models.database import session_scope
from app.repositories import CourtRepository

logger = get_task_logger(__name__)


@celery_app.task
def sync_courts_from_json(src: str | None = None) -> dict:
    """Создать/обновить суды в БД по JSON-справочнику.

    src — путь к файлу; по умолчанию COURTS_JSON_PATH (data/courts.json).
    Возвращает {"src", "created", "updated", "total"} — попадёт в результат задачи.
    """
    path = Path(src) if src else COURTS_JSON_PATH
    entries = json.loads(path.read_text(encoding="utf-8"))

    with session_scope() as session:
        created, updated = CourtRepository(session).sync_from_entries(entries)

    logger.info(
        "Справочник судов из %s: создано %s, обновлено %s, всего в файле %s",
        path, created, updated, len(entries),
    )
    return {"src": str(path), "created": created, "updated": updated, "total": len(entries)}
