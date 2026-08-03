"""Скрипт-команда: залить/обновить справочник судов в БД из data/courts.json.

Идемпотентно: перебирает все суды из JSON, ищет каждый по классификационному коду и
создаёт новый Court либо обновляет существующий (name/level/region/base_url).
Суды, которых нет в списке, не трогаются и не удаляются.

Сама логика живёт в CourtRepository.sync_from_entries — её же использует кнопка
«Залить суды из courts.json» в админке (через таск app.courts.tasks).

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

from app.config import COURTS_JSON_PATH  # noqa: E402
from app.models.database import session_scope  # noqa: E402
from app.repositories import CourtRepository  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Залить/обновить суды в БД из JSON-справочника.")
    parser.add_argument("--src", type=Path, default=COURTS_JSON_PATH, help="путь к JSON со списком судов")
    args = parser.parse_args()

    courts = json.loads(args.src.read_text(encoding="utf-8"))
    with session_scope() as session:
        created, updated = CourtRepository(session).sync_from_entries(courts)

    print(f"Источник: {args.src}")
    print(f"создано: {created}")
    print(f"обновлено: {updated}")
    print(f"всего: {len(courts)}")


if __name__ == "__main__":
    main()
