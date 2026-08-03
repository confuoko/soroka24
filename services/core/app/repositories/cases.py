"""Доступ к делам (Case) в БД."""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.database import Case, CaseLink

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

    def get_full(self, case_id: int) -> Optional[Case]:
        """Дело по id со всеми связями, загруженными сразу (или None).

        selectinload нужен, чтобы собрать ответ API до закрытия сессии: без него
        обращение к case.events за пределами session_scope упало бы с ленивой загрузкой.
        """
        return self._session.scalar(
            select(Case)
            .where(Case.id == case_id)
            .options(
                selectinload(Case.courts),
                selectinload(Case.judges),
                selectinload(Case.sides),
                selectinload(Case.events),
                selectinload(Case.place_history),
                selectinload(Case.instances),
                selectinload(Case.documents),
                selectinload(Case.court_sessions),
                # Дела из той же группы — для Case.related_cases.
                selectinload(Case.case_link).selectinload(CaseLink.cases),
            )
        )

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
