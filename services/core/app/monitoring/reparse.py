"""Перепарсинг дела из сохранённого в S3 снапшота HTML — без обращения к сайту суда.

Зачем. Разметка каждого разобранного дела лежит в S3 (см. app/storage/html_snapshots.py),
поэтому когда парсер научился доставать что-то новое, старые дела можно дозаполнить, не
ходя на портал: parse() — чистая функция, Chromium не поднимается. Это и быстрее, и не
зависит от доступности суда.

Повторяет шаг 3 sync_case (app/monitoring/tasks.py): находим карточку, затем сверка
судей/сторон/событий/местонахождений/заседаний, затем запись в историю парсинга — всё
одной транзакцией. Отличие одно: HTML берётся из S3, а не из браузера.

Карточку ищем по ключу снапшота: в нём есть и УИД, и папка карточки «<код суда>-<номер
дела>» (см. app/storage/html_snapshots.py). Новую карточку здесь НЕ заводим — перепарсинг
работает по уже сохранённой разметке, а значит дело в БД должно быть.
"""
from datetime import datetime

from app.config import S3_BUCKET
from app.courts import define_court_by_uid
from app.models.database import Case, session_scope
from app.monitoring.case_update import update_case
from app.monitoring.parse_history import (
    STATUS_CHANGED,
    STATUS_NO_CHANGES,
    append_parse_entry,
    build_entry,
    changes_to_dict,
)
from app.repositories import CaseRepository
from app.storage import read_snapshot, snapshot_sha256
from app.storage.html_snapshots import card_folder


def _find_card(session, uid: str, key: str) -> Case:
    """Найти карточку, снапшот которой лежит по этому ключу.

    Сопоставляем по имени папки: восстановить номер дела из неё нельзя (слэши заменены
    дефисами и обратно не разбираются), зато можно сложить такую же папку для каждой
    карточки этого УИД и сравнить строки.

    У старых снапшотов уровня карточки в ключе нет — тогда карточка определяется
    однозначно, только если она у этого УИД одна.
    """
    cards = CaseRepository(session).list_by_uid(uid)
    if not cards:
        raise LookupError(f"дела {uid} нет в БД — перепарсивать нечего")

    # html_snapshots/<uid>/<папка карточки>/<файл> — папка предпоследним сегментом.
    segments = key.split("/")
    folder = segments[-2] if len(segments) >= 3 else ""

    if folder and folder != uid:
        for card in cards:
            if card_folder(card.court.code, card.code) == folder:
                return card
        raise LookupError(f"карточка {folder} дела {uid} в БД не найдена")

    if len(cards) > 1:
        raise LookupError(
            f"у дела {uid} {len(cards)} карточек, а в ключе снапшота карточка не указана "
            f"— непонятно, какую обновлять"
        )
    return cards[0]


def reparse_case_from_snapshot(
    uid: str, key: str, taken_at: datetime, task_id: int | None = None
) -> tuple[int, dict]:
    """Разобрать сохранённый снапшот и обновить дело. Возвращает (id дела, дифф).

    Дифф — результат changes_to_dict(): он же уходит в Case.diff_history, поэтому
    вызывающему коду не нужно ничего досчитывать самому.

    taken_at — время СНЯТИЯ снапшота (из имени объекта), а не «сейчас»: в историю
    парсинга должно попасть то время, когда страница реально была получена с портала.

    Бросает LookupError, если карточку по ключу не удалось сопоставить с БД,
    и UnsupportedCourt, если УИД не наш.
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
        # Суд и номер берём у самой карточки: из УИД они не выводятся.
        card = _find_card(session, uid, key)

        changes = update_case(session, uid, data, card.court, card.code)
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
