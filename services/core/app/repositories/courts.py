"""Доступ к судам (Court) в БД."""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import Court


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
