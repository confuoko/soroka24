"""Доступ к судебным заседаниям (CourtSession) в БД: детерминированный uid + сверка со страницей."""
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from app.timezones import to_utc
from app.models.database import Case, CourtSession

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

    def sync_court_sessions(
        self, case: Case, sessions_data: list[dict]
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
            local_at = item["session_date"]
            session_at = to_utc(local_at, court_tz)
            uid = court_session_uid(case.card_key, local_at, item["stage"])
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
                    place=item.get("place"),
                    stage=item["stage"],
                    result=item.get("result"),
                    basis=item.get("basis"),
                )
                case.court_sessions.append(session)
                new_sessions.append(session)
            # Если заседание нашлось, но изменилось изменяемое поле - обновляем
            elif (
                existing_session.place != item.get("place")
                or existing_session.result != item.get("result")
                or existing_session.basis != item.get("basis")
            ):
                existing_session.place = item.get("place")
                existing_session.result = item.get("result")
                existing_session.basis = item.get("basis")
                updated_sessions.append(existing_session)

        # Удаляем заседания, пропавшие со страницы (cascade delete-orphan уберёт их из БД).
        removed_sessions = [
            s for s in case.court_sessions if s.uid not in desired_uids
        ]
        for session in removed_sessions:
            case.court_sessions.remove(session)

        return new_sessions, updated_sessions, removed_sessions
