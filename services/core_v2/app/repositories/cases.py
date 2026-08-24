"""Доступ к карточкам дел (Case) и их адресам (CaseUrl) в БД."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.models import Case, CaseUrl, Court
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
    "accepted_date",
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


@dataclass(frozen=True)
class MonitoringSync:
    """Итог приведения списка дел на мониторинге к присланному.

    Возвращается наружу целиком, а не одним числом: клиентскому сервису нужны обе
    величины, чтобы отличить «ничего не поменялось, всё уже так» от «запрос ничего не
    нашёл». unknown_ids — то, из-за чего этот dataclass вообще существует: id, которых в
    базе нет. Значит, у клиента есть подписка на дело, а дела у нас уже (или ещё) нет, и
    молчать об этом нельзя — иначе расхождение живёт незамеченным, а пользователь ждёт
    обновлений по делу, которое никто не обходит.
    """

    monitored: int        # сколько дел на мониторинге ПОСЛЕ операции
    added: int            # сколько дел флаг получили
    removed: int          # сколько дел флаг потеряли
    unknown_ids: list[int]  # присланные id, которых нет в базе


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
                selectinload(Case.documents),
                selectinload(Case.court_sessions),
            )
        )

    def get_with_court(self, case_id: int) -> Optional[Case]:
        """Дело с загруженным судом и БЕЗ остальных связей (или None).

        Для витрины: там нужны статус, даты и название суда, а get_full тянет все
        события, заседания и документы карточки — на список дел это лишняя работа
        и лишние сотни килобайт по сети.
        """
        return self._session.scalar(
            select(Case).where(Case.id == case_id).options(selectinload(Case.court))
        )

    def list_summaries(self, case_ids: Iterable[int]) -> list[Case]:
        """Дела по списку id, с загруженными судами и без остальных связей.

        Тот же набор данных, что у get_with_court, но за один запрос на всю страницу
        клиента вместо запроса на дело. selectinload здесь даёт ровно два SQL на любое
        число дел: один за карточками, один за судами.

        Отсутствующих id в ответе просто нет — это список, и его длина сама по себе
        осмысленный ответ.
        """
        ids = list(case_ids)
        if not ids:
            return []
        return list(
            self._session.scalars(
                select(Case)
                .where(Case.id.in_(ids))
                .options(selectinload(Case.court))
                .order_by(Case.id)
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
            case_url.last_success_at = datetime.now(timezone.utc)

    def mark_checked(self, case: Case, checked_at: datetime, changed: bool) -> None:
        """Отметить обход карточки: когда ходили и менялось ли что-нибудь.

        last_checked_at ставится на КАЖДОМ обходе, в том числе холостом: без этого
        нельзя отличить «сходили, изменений не было» от «не ходили ни разу». Кто и когда
        решит сходить снова — не забота core; он лишь честно записывает факт похода.

        last_changed_at — только когда сверка дала непустой diff. Это и есть «дата
        последнего обновления дела» для пользователя; updated_at на эту роль не
        годится, потому что строку трогает любой обход, в том числе холостой.

        У новой карточки дата проставляется при создании (см.
        upsert_by_uid_court_code): её changes пусты по построению, и сюда бы она
        пришла с changed=False.
        """
        case.last_checked_at = checked_at
        if changed:
            case.last_changed_at = checked_at

    # ------------------------------------------------------------- мониторинг
    def set_monitoring_list(self, case_ids: Iterable[int]) -> MonitoringSync:
        """Привести список дел на мониторинге к присланному.

        Семантика — ЗАМЕЩЕНИЕ, а не добавление: после вызова на мониторинге ровно эти
        дела. Такая форма выбрана потому, что клиентский сервис знает своё состояние
        целиком и не знает нашего: присылать дифф ему было бы нечем, а разошедшиеся
        состояния он бы уже не свёл.

        Идемпотентна по построению: условия обоих UPDATE отбирают только строки, которым
        флаг НАДО поменять, поэтому повторный вызов с тем же списком меняет 0 строк.
        Отсюда и осмысленные added/removed — это «сколько реально переключилось», а не
        «сколько прислали».

        Двумя UPDATE, а не выборкой в питон и циклом: список дел на мониторинге растёт с
        числом подписок, а обе операции целиком выражаются в SQL.

        synchronize_session="fetch" — чтобы уже загруженные в сессию Case увидели новое
        значение флага. В самом эндпоинте сессия свежая и синхронизировать нечего, но в
        тестах объекты живут рядом с запросом, и без этого они молча остались бы со
        старым значением.

        Коммит — на вызывающей стороне, как во всех репозиториях.
        """
        ids = sorted(set(case_ids))

        # Чего нет в базе — отдаём наверх. Отдельным запросом, потому что UPDATE про
        # ненайденные строки ничего сказать не может: rowcount не отличает «такого дела
        # нет» от «флаг уже стоял».
        known = (
            set(self._session.scalars(select(Case.id).where(Case.id.in_(ids))))
            if ids
            else set()
        )
        unknown_ids = [case_id for case_id in ids if case_id not in known]

        options = {"synchronize_session": "fetch"}

        added = 0
        if ids:
            added = self._session.execute(
                update(Case)
                .where(Case.id.in_(ids), Case.is_on_monitoring.is_(False))
                .values(is_on_monitoring=True)
                .execution_options(**options),
            ).rowcount

        # Снимаем флаг со всего, чего в списке нет. Пустой список — законный случай
        # («ни на что больше не подписаны»), и тогда снимается со всех.
        removal = update(Case).where(Case.is_on_monitoring.is_(True))
        if ids:
            removal = removal.where(Case.id.not_in(ids))
        removed = self._session.execute(
            removal.values(is_on_monitoring=False).execution_options(**options),
        ).rowcount

        monitored = self._session.scalar(
            select(func.count()).select_from(Case).where(Case.is_on_monitoring.is_(True))
        )
        return MonitoringSync(
            monitored=monitored or 0,
            added=added,
            removed=removed,
            unknown_ids=unknown_ids,
        )

    def list_monitored_ids(self, limit: Optional[int] = None) -> list[int]:
        """id дел на мониторинге; самые давно не проверявшиеся первыми.

        Порядок не косметика, а страховка от лимита. При limit каждый прогон берёт первые
        N — и если сортировать по id, эти N обходились бы каждые сутки, а хвост списка не
        обошёлся бы никогда. Сортировка по last_checked_at делает выборку честной
        очередью: обойдённое уезжает в конец само.

        NULLS FIRST — потому что NULL здесь значит «не проверяли ни разу»: такое дело
        ждёт обхода дольше всех остальных.

        id, а не объекты: планировщику нужно только поставить задачи в очередь, и тащить
        карточки со связями ради этого незачем.
        """
        query = (
            select(Case.id)
            .where(Case.is_on_monitoring.is_(True))
            .order_by(Case.last_checked_at.asc().nullsfirst(), Case.id)
        )
        if limit:
            query = query.limit(limit)
        return list(self._session.scalars(query))

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
        self, uid: str, court: Court, code: str, card_fields: dict[str, Any]
    ) -> tuple[Case, list[CaseFieldChange], bool]:
        """Найти карточку по тройке «УИД + суд + номер» или создать новую; обновить поля.

        Возвращает (дело, список изменившихся полей, признак «карточка только что
        заведена»). Признак нужен выше по стеку: на первом обходе вся карточка формально
        новая, и события об изменениях по ней не выпускаются (см. app/outbox.py).

        По списку изменившихся полей строится дифф:
        смена «Текущего состояния», появление решения первой инстанции и т.п. должны быть
        видны пользователю, а не перезаписываться молча.

        У НОВОГО дела список всегда пустой: появление дела — само по себе событие, и
        засорять дифф переходами None → значение по каждому полю не нужно.

        Номер приходит отдельным аргументом, а не в card_fields: его источник — таблица
        результатов поиска, а не карточка. Значение code, если парсер его отдал, сюда не
        попадает — см. _UPDATABLE_FIELDS.

        card_fields — это ParsedCase.card_fields(): только те поля, которые парсер
        РЕАЛЬНО прислал. Поля, которого у портала не бывает, здесь нет вовсе, и колонка
        останется нетронутой.
        """
        case = self.get_by_uid_court_code(uid, court.id, code)
        is_new = case is None
        if case is None:
            # code задаём сразу при создании: он NOT NULL, а flush() ниже не должен упасть.
            # last_changed_at тоже: появление карточки — само по себе изменение, а список
            # changes у новой карточки пустой по построению, и mark_checked ниже дату бы
            # не проставил. Пустая дата выглядела бы как «дело никогда не менялось».
            case = Case(uid=uid, court=court, code=code, last_changed_at=datetime.now(timezone.utc))
            self._session.add(case)

        # Обновляем только те поля, которые парсер реально прислал, и различаем два
        # разных случая. Это самое неочевидное место файла. Отбор делает
        # ParsedCase.card_fields(), сюда приходит уже готовый словарь.
        #
        #   ключа НЕТ в data         — у этого портала такого поля не бывает вовсе,
        #                              колонку не трогаем;
        #   ключ есть, значение None — метка со страницы пропала, колонку обнуляем.
        #
        # Наборы ключей у парсеров РАЗНЫЕ: тип A присылает 11 скаляров, msudrf B и C —
        # по 5, СПб — 4. Поэтому «досеять» отсутствующие ключи значениями None нельзя:
        # так у половины дел молча обнулились бы заполненные колонки.
        changes: list[CaseFieldChange] = []
        for field in _UPDATABLE_FIELDS:
            if field not in card_fields:
                continue
            new = card_fields[field]
            old = getattr(case, field)
            if not is_new and old != new:
                changes.append(CaseFieldChange(field=field, old=old, new=new))
            setattr(case, field, new)

        self._session.flush()  # чтобы получить case.id ещё до commit
        return case, changes, is_new
