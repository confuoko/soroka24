"""Дозаполнить уже сохранённые дела, перепарсив их HTML из S3.

Когда нужен. Парсер научился доставать что-то, чего раньше не брал (например судебные
заседания), — у старых дел этих данных в БД нет. Ждать следующего обхода портала не надо:
разметка каждого разобранного дела лежит снапшотом в S3, и её достаточно.

Что делает: для каждого дела берёт САМЫЙ СВЕЖИЙ снапшот карточки и прогоняет через
обычный путь обновления (app/monitoring/reparse.py). К сайту суда не обращается.

Страницы отказов (подпапка failed/ — капчи, блокировки) пропускаются: отдавать их в
парсер как карточку нельзя.

Запуск (по умолчанию только показывает, что изменится):

    python scripts/backfill_from_snapshots.py
    python scripts/backfill_from_snapshots.py --apply
    python scripts/backfill_from_snapshots.py --uid 77MS0023-01-2026-001701-40 --apply
"""
import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Скрипт запускают как `python scripts/backfill_from_snapshots.py` из /app, поэтому корень
# проекта в sys.path добавляем сами (иначе `import app...` не найдётся).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import HTML_SNAPSHOT_PREFIX  # noqa: E402
from app.courts import UnsupportedCourt  # noqa: E402
from app.monitoring.reparse import reparse_case_from_snapshot  # noqa: E402
from app.storage import is_failure_key, list_keys  # noqa: E402
from app.storage.html_snapshots import _TS_FORMAT  # noqa: E402

# Разделы диффа, по которым печатаем сводку (ключ в changes_to_dict → подпись).
DIFF_SECTIONS = (
    ("case", "поля дела"),
    ("sessions", "заседания"),
    ("documents", "документы"),
    ("events", "события"),
    ("places", "местонахождения"),
)


def _snapshot_taken_at(key: str) -> datetime | None:
    """Время снятия снапшота из имени объекта (None, если имя нестандартное).

    Ключ выглядит как html_snapshots/<уид>/<уид>_2026-08-04T15-21-21Z.html.gz — время
    лежит между последним «_» и «.html.gz» (см. html_snapshots.snapshot_key).
    """
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(".html.gz") or "_" not in name:
        return None
    stamp = name[: -len(".html.gz")].rsplit("_", 1)[-1]
    try:
        return datetime.strptime(stamp, _TS_FORMAT)
    except ValueError:
        return None


def _latest_snapshot_per_case(uid_filter: str | None) -> list[tuple[str, str, datetime]]:
    """Самый свежий снапшот карточки по каждому делу: [(уид, ключ, время снятия)]."""
    latest: dict[str, tuple[str, datetime]] = {}

    prefix = f"{HTML_SNAPSHOT_PREFIX}/{uid_filter}/" if uid_filter else f"{HTML_SNAPSHOT_PREFIX}/"
    for key in list_keys(prefix):
        if is_failure_key(key):
            continue  # страница отказа, а не карточка дела
        taken_at = _snapshot_taken_at(key)
        if taken_at is None:
            continue
        uid = key.split("/")[1]
        if uid not in latest or taken_at > latest[uid][1]:
            latest[uid] = (key, taken_at)

    return [(uid, key, taken_at) for uid, (key, taken_at) in sorted(latest.items())]


def _summarize(diff: dict) -> str:
    """Короткая сводка диффа: только непустые разделы."""
    parts = []
    for section, label in DIFF_SECTIONS:
        block = diff.get(section) or {}
        counts = Counter({k: len(v) for k, v in block.items() if v})
        if counts:
            detail = ", ".join(f"{k} {n}" for k, n in counts.items())
            parts.append(f"{label}: {detail}")
    return "; ".join(parts) if parts else "изменений нет"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uid", default=None, help="обработать одно дело по УИД")
    parser.add_argument(
        "--apply", action="store_true",
        help="применить изменения; без этого флага только показывает, что найдено",
    )
    args = parser.parse_args()

    cases = _latest_snapshot_per_case(args.uid)
    if not cases:
        print("Снапшотов не найдено.")
        return 0

    mode = "ПРИМЕНЯЮ" if args.apply else "ПРОГОН ВХОЛОСТУЮ (изменений не будет)"
    print(f"{mode}. Дел со снапшотами: {len(cases)}\n")

    touched = errors = 0

    for uid, key, taken_at in cases:
        if not args.apply:
            # Вхолостую дело не трогаем: печатаем, что именно будет перепарсено.
            print(f"{uid}  снапшот {taken_at:%Y-%m-%d %H:%M:%S}")
            continue

        try:
            case_id, diff = reparse_case_from_snapshot(uid, key, taken_at)
        except (UnsupportedCourt, LookupError) as exc:
            print(f"{uid}  пропущено: {exc}")
            errors += 1
            continue
        except Exception as exc:
            print(f"{uid}  ОШИБКА: {exc}")
            errors += 1
            continue

        summary = _summarize(diff)
        print(f"{uid}  дело id={case_id}: {summary}")
        if summary != "изменений нет":
            touched += 1

    if args.apply:
        print(f"\nИтог: дел с изменениями {touched} из {len(cases)}, ошибок {errors}")
    else:
        print("\nЭто был холостой прогон. Повтори с --apply, чтобы применить.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
