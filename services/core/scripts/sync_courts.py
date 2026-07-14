"""Скрипт-команда: залить/обновить справочник судов в БД из data/courts.json.

Идемпотентно: перебирает все суды из JSON, ищет каждый по классификационному коду и
создаёт новый Court либо обновляет существующий (name/level/region/base_url).
Суды, которых нет в списке, не трогаются и не удаляются.

Запуск (из папки services/core, чтобы резолвился пакет app; нужна поднятая БД):
    python scripts/sync_courts.py
    python scripts/sync_courts.py --src data/courts.json

Адрес БД берётся из app.config.DATABASE_URL (env DATABASE_URL).
"""
import argparse
import json
import sys
from pathlib import Path

# Добавляем корень core в sys.path, чтобы `import app...` работал при запуске из любой папки.
CORE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_ROOT))

from app.models.database import Court, CourtLevel, session_scope  # noqa: E402

DEFAULT_SRC = CORE_ROOT / "data" / "courts.json"


def sync_courts(courts: list[dict]) -> tuple[int, int]:
    """Создать/обновить суды по коду. Возвращает (создано, обновлено)."""
    created = 0
    updated = 0
    with session_scope() as session:
        # Одним запросом поднимаем существующие суды в словарь {code: Court}.
        existing = {court.code: court for court in session.query(Court).all()}

        for entry in courts:
            level = CourtLevel(entry["level"])  # значение JSON ("mirsud") -> член enum
            court = existing.get(entry["code"])
            if court is None:
                session.add(Court(
                    code=entry["code"],
                    name=entry["name"],
                    level=level,
                    region=entry["region"],
                    base_url=entry.get("base_url"),
                ))
                created += 1
            else:
                court.name = entry["name"]
                court.level = level
                court.region = entry["region"]
                court.base_url = entry.get("base_url")
                updated += 1

    return created, updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Залить/обновить суды в БД из JSON-справочника.")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="путь к JSON со списком судов")
    args = parser.parse_args()

    courts = json.loads(args.src.read_text(encoding="utf-8"))
    created, updated = sync_courts(courts)

    print(f"Источник: {args.src}")
    print(f"создано: {created}")
    print(f"обновлено: {updated}")
    print(f"всего: {len(courts)}")


if __name__ == "__main__":
    main()
