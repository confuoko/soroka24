"""Доступ к делам (Case) в БД."""
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.database import Case, CaseLink

# Поля Case, которые заполняет парсер из карточки дела.
_UPDATABLE_FIELDS = (
    "url",
    "code",
    "application_number",
    "incoming_number",
    "receipt_date",
    "registration_date",
    "first_instance_date",
    "first_instance_decision",
    "decision_effective_date",
    "superior_case_number",
    "category",
    "status",
)


@dataclass(frozen=True)
class CaseFieldChange:
    """Изменение скалярного поля дела: что было и что стало."""

    field: str
    old: Any
    new: Any


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

    def upsert_by_uid(self, uid: str, data: dict) -> tuple[Case, list[CaseFieldChange]]:
        """Найти дело по УИД или создать новое; обновить поля из data.

        Возвращает (дело, список изменившихся полей). По этому списку строится дифф:
        смена «Текущего состояния», появление решения первой инстанции и т.п. должны быть
        видны пользователю, а не перезаписываться молча.

        У НОВОГО дела список всегда пустой: появление дела — само по себе событие, и
        засорять дифф переходами None → значение по каждому полю не нужно.
        """
        case = self.get_by_uid(uid)
        is_new = case is None
        if case is None:
            case = Case(uid=uid)
            self._session.add(case)

        # Обновляем только те поля, что реально пришли от парсера. Парсер отдаёт ВСЕ
        # ключи карточки (отсутствующая на странице метка приходит как None), поэтому
        # пропавшее значение корректно обнуляется, а не остаётся стухшим.
        changes: list[CaseFieldChange] = []
        for field in _UPDATABLE_FIELDS:
            if field not in data:
                continue
            new = data[field]
            old = getattr(case, field)
            if not is_new and old != new:
                changes.append(CaseFieldChange(field=field, old=old, new=new))
            setattr(case, field, new)

        self._session.flush()  # чтобы получить case.id ещё до commit
        return case, changes
