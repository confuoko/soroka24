"""Доступ к судебным заседаниям (CourtSession) в БД: детерминированный uid + сверка со страницей."""
import uuid
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.timezones import to_utc
from app.parsers.parsed_case import ParsedSession
from app.models import Case, Court, CourtSession

# Фиксированный namespace для uid заседаний (задан один раз, менять нельзя — иначе uid
# всех заседаний «поедут» и повторный парсинг перестанет их узнавать).
# Свой, отдельный от EVENT_UID_NAMESPACE и PLACE_HISTORY_UID_NAMESPACE: иначе строки
# разных сущностей с одинаковым текстом и датой получили бы один и тот же uid.
COURT_SESSION_UID_NAMESPACE = uuid.UUID("9c4e7a10-2f83-5b6d-a1c7-4e0d9f5b3a26")


def court_session_uid(card_key: str, session_at: datetime, stage: str) -> uuid.UUID:
    """
    Детерминированный uid заседания из обязательных (identity) полей.

    identity = карточка + дата-время заседания + стадия.

    КАРТОЧКА, а не дело: card_key — это «УИД | код суда | номер дела»
    (Case.card_key). По одному УИД карточек бывает несколько, а uid здесь уникален
    глобально — считай мы его от УИД, строки соседних карточек столкнулись бы.

    Время входит в identity намеренно: у одного дела бывает несколько заседаний одной
    стадии, и без времени два заседания в один день дали бы один uid — то есть второе
    просто не сохранилось бы. Перенос заседания портал оформляет НОВОЙ строкой (у старой
    появляется результат «Отложено»), поэтому время в рамках строки стабильно.

    session_at — МЕСТНОЕ время суда (naive), ровно как на странице. Ключ считается именно
    от него, а не от хранимого момента в UTC: иначе один и тот же «14:05» в Москве и во
    Владивостоке давал бы разные ключи, а перевод базы на timestamptz переписал бы uid
    всех уже сохранённых заседаний и породил бы в outbox волну «удалено/создано».

    place/result/basis сюда НЕ входят — они изменяемые (у будущего заседания результата
    ещё нет, потом он появляется), и их правка должна детектиться как UPDATE той же
    строки, а не как новое заседание.
    """
    key = "|".join([card_key, session_at.isoformat(), stage])
    return uuid.uuid5(COURT_SESSION_UID_NAMESPACE, key)


class CourtSessionRepository:
    """Чтение и запись судебных заседаний дела. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_cases(
        self,
        case_ids: Iterable[int],
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> list[Row]:
        """Заседания сразу нескольких дел — одним запросом, с номером дела и судом.

        Ради календаря клиентского сервиса. Заседания у него разбросаны по всем подпискам
        сразу, а получить их можно было только полной карточкой каждого дела — сотни
        килобайт на дело и N запросов. Тот же N+1 по сети, ради устранения которого уже
        существует CaseRepository.list_summaries.

        Возвращает не CourtSession, а строки `(заседание, номер дела, название суда, пояс
        суда)`. Джойн здесь, а не дозапрос на стороне клиента, ровно потому же: иначе
        календарь на тридцать заседаний сходил бы в core тридцать один раз.

        Границы date_from/date_to — по МЕСТНОМУ времени суда, а не по UTC, и обе
        включительно. Иначе заседание во Владивостоке в 09:00 первого числа попало бы в
        предыдущий месяц: в UTC этот момент приходится на 23:00 предыдущих суток. Дата на
        странице суда — местная, и фильтр обязан совпадать с тем, что видит человек.

        Отсутствующих id в ответе просто нет — как и у list_summaries: это список, и его
        длина сама по себе осмысленный ответ.
        """
        ids = list(case_ids)
        if not ids:
            return []

        # timestamptz → местное время суда (timestamp without time zone), ровно как на
        # странице. Пояс берём из строки суда: у каждого дела он свой.
        local_at = func.timezone(Court.timezone, CourtSession.session_date)

        statement = (
            select(CourtSession, Case.code, Court.name, Court.timezone)
            .join(Case, CourtSession.case_id == Case.id)
            .join(Court, Case.court_id == Court.id)
            .where(CourtSession.case_id.in_(ids))
        )
        if date_from is not None:
            statement = statement.where(local_at >= date_from)
        if date_to is not None:
            # Правая граница включительна: date_to это «по такое-то число», а сравнение
            # идёт с моментом, а не с датой. Без сдвига на сутки заседание в 10:00
            # последнего дня диапазона в ответ не попало бы.
            statement = statement.where(local_at < date_to + timedelta(days=1))

        # По моменту, а не по местному времени: календарь показывает одну ленту по всем
        # судам сразу, и порядок в ней должен быть хронологическим. id — чтобы порядок был
        # устойчив у заседаний, назначенных на одну минуту.
        statement = statement.order_by(CourtSession.session_date, CourtSession.id)
        return list(self._session.execute(statement))

    def sync_court_sessions(
        self, case: Case, sessions_data: list[ParsedSession]
    ) -> tuple[list[CourtSession], list[CourtSession], list[CourtSession]]:
        """Привести заседания дела к тому, что сейчас на странице.

        Возвращает (new_sessions, updated_sessions, removed_sessions):
        - uid новый                             → создаём заседание   → new_sessions;
        - uid есть, изменилось place/result/basis → обновляем поля     → updated_sessions;
        - uid заседания больше нет на странице   → удаляем             → removed_sessions.

        Страница — источник истины. Порядок важен: сначала одним проходом по sessions_data
        наполняем desired_uids и добавляем/обновляем, и только потом удаляем те, чей uid
        не встретился на странице.
        """
        court_tz = case.court.timezone
        existing = {s.uid: s for s in case.court_sessions}
        desired_uids: set[uuid.UUID] = set()

        new_sessions: list[CourtSession] = []
        updated_sessions: list[CourtSession] = []

        # 1. Проход по заседаниям, которые есть на актуальной странице
        for item in sessions_data:
            # Парсер отдаёт МЕСТНОЕ время суда (naive) — ровно то, что на странице.
            # В ключ идёт оно, в БД — тот же момент, переведённый в UTC.
            local_at = item.session_date
            session_at = to_utc(local_at, court_tz)
            uid = court_session_uid(case.card_key, local_at, item.stage)
            # Портал может отдать две одинаковые строки — считаем их одним заседанием.
            # Обработать вторую нельзя: uid у неё тот же, и UNIQUE ix_court_session_uid
            # уронит commit вместе со всей транзакцией дела.
            if uid in desired_uids:
                continue
            # Добавляем uid в список заседаний, которые мы хотим увидеть в БД
            desired_uids.add(uid)
            # пытаемся найти uid среди уже существующих
            existing_session = existing.get(uid)
            # Если заседание не найдено среди существующих - создаём новое
            if existing_session is None:
                session = CourtSession(
                    uid=uid,
                    session_date=session_at,
                    place=item.place,
                    stage=item.stage,
                    result=item.result,
                    basis=item.basis,
                )
                case.court_sessions.append(session)
                new_sessions.append(session)
            # Если заседание нашлось, но изменилось изменяемое поле - обновляем
            elif (
                existing_session.place != item.place
                or existing_session.result != item.result
                or existing_session.basis != item.basis
            ):
                existing_session.place = item.place
                existing_session.result = item.result
                existing_session.basis = item.basis
                updated_sessions.append(existing_session)

        # Удаляем заседания, пропавшие со страницы (cascade delete-orphan уберёт их из БД).
        removed_sessions = [
            s for s in case.court_sessions if s.uid not in desired_uids
        ]
        for session in removed_sessions:
            case.court_sessions.remove(session)

        return new_sessions, updated_sessions, removed_sessions
