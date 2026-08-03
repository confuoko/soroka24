"""Доступ к судам (Court) в БД."""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import Court, CourtLevel

# Поля суда, которые перезаписываются из JSON-справочника (code — ключ, его не трогаем).
_SYNCED_FIELDS = ("name", "level", "region", "base_url")


class CourtRepository:
    """Чтение судов-справочника. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_code(self, code: str) -> Optional[Court]:
        """Найти суд по классификационному коду (или None).

        Court.code — уникальный индекс, поэтому по коду в БД максимум одна запись:
        ситуация «несколько судов на код» невозможна на уровне схемы.
        """
        return self._session.scalar(select(Court).where(Court.code == code))

    def sync_from_entries(self, entries: list[dict]) -> tuple[int, int]:
        """Создать/обновить суды по коду из записей JSON-справочника.

        Идемпотентно: суды, которых нет в entries, не трогаются и не удаляются.
        Возвращает (создано, обновлено) — обновлённым считается только суд, у которого
        реально изменилось хотя бы одно поле, иначе счётчик равнялся бы всему справочнику.
        """
        created = 0
        updated = 0

        # Одним запросом поднимаем существующие суды в словарь {code: Court}.
        existing = {court.code: court for court in self._session.scalars(select(Court))}

        for entry in entries:
            # Значение из JSON ("mirsud") -> член enum; на неизвестном уровне упадёт ValueError.
            values = {
                "name": entry["name"],
                "level": CourtLevel(entry["level"]),
                "region": entry["region"],
                "base_url": entry.get("base_url"),
            }

            court = existing.get(entry["code"])
            if court is None:
                self._session.add(Court(code=entry["code"], **values))
                created += 1
                continue

            # Пишем только реально изменившиеся поля — так counters не врут.
            changed = [f for f in _SYNCED_FIELDS if getattr(court, f) != values[f]]
            if changed:
                for field in changed:
                    setattr(court, field, values[field])
                updated += 1

        return created, updated
