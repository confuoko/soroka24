"""История парсингов дела: сериализация diff'а и дозапись в Case.diff_history.

Раньше CaseChanges уходил только в logger.info и терялся вместе с логами воркера.
Здесь он превращается в JSON и дозаписывается в поле дела — по записи на каждый
вызов парсинга, включая «изменений нет» и «сайт суда не открылся».
"""
from datetime import date, datetime
from typing import Optional

from app.config import DIFF_HISTORY_LIMIT
from app.models.database import (
    Case,
    CourtSession,
    Document,
    Event,
    Judge,
    PlaceHistory,
    Side,
)
from app.monitoring.case_update import CaseChanges
from app.repositories import CaseFieldChange

# Статусы записи истории (что вообще произошло за этот вызов парсинга).
STATUS_CHANGED = "changed"          # дело обновлено, есть изменения
STATUS_NO_CHANGES = "no_changes"    # страница разобрана, но всё совпало с БД
STATUS_FETCH_ERROR = "fetch_error"  # не удалось получить страницу (403/429/timeout/дело не найдено)
STATUS_PARSE_ERROR = "parse_error"  # страница получена, но разбор упал


def _iso(value: Optional[date]) -> Optional[str]:
    """Дата в ISO-строку (None остаётся None) — JSON не умеет date."""
    return value.isoformat() if value is not None else None


def _event_to_dict(event: Event) -> dict:
    """Событие в компактный вид для истории."""
    return {
        "uid": str(event.uid),
        "event_date": _iso(event.event_date),
        "state_description": event.state_description,
        "document_str": event.document_str,
    }


def _place_to_dict(place: PlaceHistory) -> dict:
    """Строку истории местонахождения — в компактный вид."""
    return {
        "uid": str(place.uid),
        "place_date": _iso(place.place_date),
        "place_description": place.place_description,
        "comment": place.comment,
    }


def _session_to_dict(session: CourtSession) -> dict:
    """Судебное заседание — в компактный вид.

    session_date отдаём с временем: оно входит в identity заседания и пользователю важно
    не меньше даты («заседание 14.08 в 10:00»).
    """
    return {
        "uid": str(session.uid),
        "session_date": session.session_date.isoformat()
        if session.session_date is not None
        else None,
        "place": session.place,
        "stage": session.stage,
        "result": session.result,
        "basis": session.basis,
    }


def _field_change_to_dict(change: CaseFieldChange) -> dict:
    """Изменение поля дела — в компактный вид.

    Даты приводим к ISO: JSON не умеет date, а в полях дела их большинство.
    """
    return {
        "field": change.field,
        "old": _iso(change.old) if isinstance(change.old, date) else change.old,
        "new": _iso(change.new) if isinstance(change.new, date) else change.new,
    }


def _document_to_dict(document: Document) -> dict:
    """Документ — в компактный вид.

    Только метаданные: ни текста документа, ни ссылки на файл мы не храним.
    """
    return {
        "uid": str(document.uid),
        "document_date": _iso(document.document_date),
        "document_type": document.document_type,
    }


def _judge_to_dict(judge: Judge) -> dict:
    return {"id": judge.id, "full_name": judge.full_name}


def _side_to_dict(side: Side) -> dict:
    return {"id": side.id, "full_name": side.full_name, "type": side.type.value}


def changes_to_dict(changes: CaseChanges) -> dict:
    """Превратить CaseChanges в JSON-совместимую структуру.

    Вызывать сразу после update_case(), до коммита: у удалённых событий и
    местонахождений атрибуты в этот момент ещё загружены в сессии.
    """
    return {
        # Скалярные поля самого дела: что изменилось со времени прошлого разбора.
        "case": {
            "changed": [_field_change_to_dict(f) for f in changes.field_changes],
        },
        "events": {
            "new": [_event_to_dict(e) for e in changes.new_events],
            "updated": [_event_to_dict(e) for e in changes.updated_events],
            "removed": [_event_to_dict(e) for e in changes.removed_events],
        },
        "places": {
            "new": [_place_to_dict(p) for p in changes.new_places],
            "updated": [_place_to_dict(p) for p in changes.updated_places],
            "removed": [_place_to_dict(p) for p in changes.removed_places],
        },
        "sessions": {
            "new": [_session_to_dict(s) for s in changes.new_sessions],
            "updated": [_session_to_dict(s) for s in changes.updated_sessions],
            "removed": [_session_to_dict(s) for s in changes.removed_sessions],
        },
        # У документов нет updated: изменяемых полей у них не осталось.
        "documents": {
            "new": [_document_to_dict(d) for d in changes.new_documents],
            "removed": [_document_to_dict(d) for d in changes.removed_documents],
        },
        "judges": {
            "added": [_judge_to_dict(j) for j in changes.added_judges],
            "removed": [_judge_to_dict(j) for j in changes.removed_judges],
        },
        "sides": {
            "added": [_side_to_dict(s) for s in changes.added_sides],
            "removed": [_side_to_dict(s) for s in changes.removed_sides],
        },
    }


def build_entry(
    status: str,
    fetched_at: datetime,
    task_id: Optional[int] = None,
    snapshot: Optional[dict] = None,
    diff: Optional[dict] = None,
    error: Optional[str] = None,
    html_unchanged: bool = False,
) -> dict:
    """Собрать одну запись истории парсинга.

    snapshot — результат save_snapshot() (html_bucket/html_key/html_sha256/html_size)
    или None, если страницу получить не удалось либо не удалось положить её в S3.
    diff — результат changes_to_dict() или None (при ошибке сверять было нечего).
    """
    entry = {
        "ts": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_id": task_id,
        "status": status,
        "html_bucket": None,
        "html_key": None,
        "html_sha256": None,
        "html_size": None,
        "html_unchanged": html_unchanged,
        "error": error,
        "diff": diff,
    }
    if snapshot is not None:
        entry.update(snapshot)
    return entry


def last_entry(case: Case) -> Optional[dict]:
    """Последняя запись истории парсинга дела (или None, если история пуста)."""
    history = case.diff_history or []
    return history[-1] if history else None


def append_parse_entry(case: Case, entry: dict, limit: int = DIFF_HISTORY_LIMIT) -> None:
    """Дозаписать запись в Case.diff_history, оставив последние limit записей.

    Именно переприсваивание списка, а не append: мутацию JSONB на месте SQLAlchemy
    не отслеживает и в UPDATE она не попадёт.
    """
    history = list(case.diff_history or [])
    history.append(entry)
    case.diff_history = history[-limit:] if limit > 0 else history
