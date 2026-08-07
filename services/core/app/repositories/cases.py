"""Доступ к карточкам дел (Case) и их адресам (CaseUrl) в БД."""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.database import Case, CaseLink, CaseUrl, Court
from app.validators import canonical_case_url

# Поля Case, которые заполняет парсер из карточки дела.
# url здесь намеренно нет: адресов у карточки несколько, они лежат в CaseUrl. Пока url
# был полем, каждая новая ссылка перезаписывала предыдущую и попадала в историю дела
# как «изменение» — пользователю такое видеть незачем.
# code здесь тоже нет, и это важно: номер дела входит в ключ карточки, поэтому изменение
# номера означает ДРУГУЮ карточку, а не изменение этой. Если вернуть его сюда, обход будет
# переименовывать существующую карточку вместо того, чтобы завести новую.
_UPDATABLE_FIELDS = (
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

    def get_by_uid_court_code(
        self, uid: str, court_id: int, code: str
    ) -> Optional[Case]:
        """Найти карточку дела по тройке «УИД + суд + номер дела» (или None).

        Основной способ найти карточку: ни УИД сам по себе, ни пара с судом не уникальны —
        тот же УИД в другом суде это другая инстанция, а тот же УИД в том же суде с другим
        номером — другое производство (приказное, затем исковое).
        """
        return self._session.scalar(
            select(Case).where(
                Case.uid == uid, Case.court_id == court_id, Case.code == code
            )
        )

    def list_by_uid(self, uid: str) -> list[Case]:
        """Все карточки с этим УИД — по всем судам и производствам."""
        return list(
            self._session.scalars(select(Case).where(Case.uid == uid).order_by(Case.id))
        )

    def list_by_uid_and_court(self, uid: str, court_id: int) -> list[Case]:
        """Карточки этого УИД в этом суде — по одной на производство.

        Нужно там, где номер дела неизвестен: в ветках ошибок задача успевает узнать суд,
        но не номер (страница не открылась или не разобралась).
        """
        return list(
            self._session.scalars(
                select(Case)
                .where(Case.uid == uid, Case.court_id == court_id)
                .order_by(Case.id)
            )
        )

    def get_full(self, case_id: int) -> Optional[Case]:
        """Дело по id со всеми связями, загруженными сразу (или None).

        selectinload нужен, чтобы собрать ответ API до закрытия сессии: без него
        обращение к case.events за пределами session_scope упало бы с ленивой загрузкой.
        """
        return self._session.scalar(
            select(Case)
            .where(Case.id == case_id)
            .options(
                selectinload(Case.court),
                selectinload(Case.urls),
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

    def get_by_url(self, url: str) -> Optional[Case]:
        """Карточка по ссылке на неё (адрес уникален глобально).

        Нужно для порталов без поиска по УИД: там ссылка — единственное, чем карточку
        можно опознать до похода на страницу. Адрес приводим к канонической форме, иначе
        та же карточка с http вместо https не нашлась бы.
        """
        return self._session.scalar(
            select(Case).join(Case.urls).where(CaseUrl.url == canonical_case_url(url))
        )

    def add_url(self, case: Case, url: str) -> CaseUrl:
        """Запомнить ещё один адрес карточки. Повторный вызов ничего не меняет.

        Если адрес уже закреплён за ДРУГОЙ карточкой — это не дубль, а ошибка в данных
        (один адрес не может вести в две карточки), поэтому падаем, а не переписываем
        чужую привязку молча.
        """
        canonical = canonical_case_url(url)
        existing = self._session.scalar(
            select(CaseUrl).where(CaseUrl.url == canonical)
        )
        if existing is not None:
            if existing.case_id != case.id:
                raise ValueError(
                    f"Адрес {canonical} уже закреплён за карточкой id={existing.case_id}"
                )
            return existing

        case_url = CaseUrl(case_id=case.id, url=canonical)
        self._session.add(case_url)
        self._session.flush()
        return case_url

    def mark_url_success(self, url: str) -> None:
        """Отметить, что по этому адресу страницу удалось получить."""
        case_url = self._session.scalar(
            select(CaseUrl).where(CaseUrl.url == canonical_case_url(url))
        )
        if case_url is not None:
            case_url.last_success_at = datetime.utcnow()

    @staticmethod
    def primary_url(case: Case) -> Optional[str]:
        """Каким адресом ходить за карточкой при повторном обходе.

        Берём тот, по которому последний раз получилось. Если не получалось ещё ни по
        одному — самый свежий из добавленных: рабочая ссылка важнее просто известной,
        а из нерабочих больше шансов у той, которую прислали последней.
        """
        if not case.urls:
            return None
        best = max(
            case.urls,
            key=lambda u: (
                u.last_success_at is not None,
                u.last_success_at or u.created_at,
                -u.id,
            ),
        )
        return best.url

    def upsert_by_uid_court_code(
        self, uid: str, court: Court, code: str, data: dict
    ) -> tuple[Case, list[CaseFieldChange]]:
        """Найти карточку по тройке «УИД + суд + номер» или создать новую; обновить поля.

        Возвращает (дело, список изменившихся полей). По этому списку строится дифф:
        смена «Текущего состояния», появление решения первой инстанции и т.п. должны быть
        видны пользователю, а не перезаписываться молча.

        У НОВОГО дела список всегда пустой: появление дела — само по себе событие, и
        засорять дифф переходами None → значение по каждому полю не нужно.

        Номер приходит отдельным аргументом, а не в data: его источник — таблица
        результатов поиска, а не карточка. Значение code из data (если парсер его отдал)
        сюда не попадает, см. _UPDATABLE_FIELDS.
        """
        case = self.get_by_uid_court_code(uid, court.id, code)
        is_new = case is None
        if case is None:
            # code задаём сразу при создании: он NOT NULL, а flush() ниже не должен упасть.
            case = Case(uid=uid, court=court, code=code)
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
