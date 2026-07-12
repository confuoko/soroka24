"""Доступ к делам (Case) в БД."""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import Case

# Поля Case, которые сможет заполнять парсер (пока data пустой).
_UPDATABLE_FIELDS = (
    "url",
    "code",
    "application_number",
    "incoming_number",
    "receipt_date",
    "category",
    "status",
)


class CaseRepository:
    """Чтение и запись дел. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_uid(self, uid: str) -> Optional[Case]:
        """Найти дело по УИД (или None)."""
        return self._session.scalar(select(Case).where(Case.uid == uid))

    def upsert_by_uid(self, uid: str, data: dict) -> Case:
        """Найти дело по УИД или создать новое; обновить известные поля из data."""
        case = self.get_by_uid(uid)
        if case is None:
            case = Case(uid=uid)
            self._session.add(case)

        # Обновляем только те поля, что реально пришли (когда появится парсер).
        for field in _UPDATABLE_FIELDS:
            if field in data:
                setattr(case, field, data[field])

        self._session.flush()  # чтобы получить case.id ещё до commit
        return case
