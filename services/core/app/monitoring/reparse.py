"""Перепарсинг дела из сохранённого в S3 снапшота HTML — без обращения к сайту суда.

Зачем. Разметка каждого разобранного дела лежит в S3 (см. app/storage/html_snapshots.py),
поэтому когда парсер научился доставать что-то новое, старые дела можно дозаполнить, не
ходя на портал: parse() — чистая функция, Chromium не поднимается. Это и быстрее, и не
зависит от доступности суда.

Повторяет шаг 3 sync_case (app/monitoring/tasks.py): суд резолвим первым, затем сверка
судей/сторон/событий/местонахождений/заседаний, затем запись в историю парсинга — всё
одной транзакцией. Отличие одно: HTML берётся из S3, а не из браузера.
"""
from datetime import datetime

from app.config import S3_BUCKET
from app.courts import define_court_by_uid
from app.models.database import session_scope
from app.monitoring.case_update import update_case
from app.monitoring.parse_history import (
    STATUS_CHANGED,
    STATUS_NO_CHANGES,
    append_parse_entry,
    build_entry,
    changes_to_dict,
)
from app.repositories import CourtRepository
from app.storage import read_snapshot, snapshot_sha256


def reparse_case_from_snapshot(
    uid: str, key: str, taken_at: datetime, task_id: int | None = None
) -> tuple[int, dict]:
    """Разобрать сохранённый снапшот и обновить дело. Возвращает (id дела, дифф).

    Дифф — результат changes_to_dict(): он же уходит в Case.diff_history, поэтому
    вызывающему коду не нужно ничего досчитывать самому.

    taken_at — время СНЯТИЯ снапшота (из имени объекта), а не «сейчас»: в историю
    парсинга должно попасть то время, когда страница реально была получена с портала.

    Бросает LookupError, если суда нет в справочнике (как NewCourtException в sync_case:
    заводить дело без суда не хотим), и UnsupportedCourt, если УИД не наш.
    """
    html = read_snapshot(key)
    data = define_court_by_uid(uid).parse(html)

    raw = html.encode("utf-8")
    snapshot = {
        "html_bucket": S3_BUCKET,
        "html_key": key,
        "html_sha256": snapshot_sha256(html),
        "html_size": len(raw),
    }

    with session_scope() as session:
        # Код суда для мировых судов Москвы — первые 8 символов УИД (напр. 77MS0001).
        court = CourtRepository(session).get_by_code(uid[:8])
        if court is None:
            raise LookupError(f"суда {uid[:8]} нет в справочнике")

        changes = update_case(session, uid, data, court)
        case_id = changes.case.id
        # Дифф считаем до коммита: у удалённых строк атрибуты ещё загружены в сессии.
        diff = changes_to_dict(changes)
        append_parse_entry(
            changes.case,
            build_entry(
                status=STATUS_CHANGED if changes.has_changes() else STATUS_NO_CHANGES,
                fetched_at=taken_at,
                task_id=task_id,
                snapshot=snapshot,
                diff=diff,
            ),
        )

    return case_id, diff
