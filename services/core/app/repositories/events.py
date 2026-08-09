"""Доступ к событиям (Event) в БД: детерминированный uid + сверка со страницей."""
import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.models.database import Case, Event

# Фиксированный namespace для uid событий (задан один раз, менять нельзя —
# иначе uid всех событий «поедут» и повторный парсинг перестанет их узнавать).
EVENT_UID_NAMESPACE = uuid.UUID("af75dcd7-7083-4294-8e05-d5f643e533c3")


def event_uid(card_key: str, event_date: date, state_description: str) -> uuid.UUID:
    """
    Детерминированный uid события из обязательных (identity) полей.

    identity = карточка + дата + описание состояния.

    КАРТОЧКА, а не дело: card_key — это «УИД | код суда | номер дела»
    (Case.card_key). По одному УИД карточек бывает несколько, а uid здесь уникален
    глобально — считай мы его от УИД, строки соседних карточек столкнулись бы.

    document_str и published_at сюда НЕ входят — они изменяемы (на портале дописываются
    позже), и их правка должна детектиться как UPDATE того же события, а не как новое
    событие.
    """
    key = "|".join([card_key, event_date.isoformat(), state_description])
    return uuid.uuid5(EVENT_UID_NAMESPACE, key)


class EventRepository:
    """Чтение и запись событий дела. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def sync_events(
        self, case: Case, events_data: list[dict]
    ) -> tuple[list[Event], list[Event], list[Event]]:
        """Привести события дела к тому, что сейчас на странице.

        Возвращает (new_events, updated_events, removed_events):
        - uid новый                          → создаём событие → new_events;
        - uid есть, изменилось изменяемое поле → обновляем      → updated_events;
        - uid события больше нет на странице  → удаляем         → removed_events.

        Страница — источник истины. Порядок важен: сначала одним проходом по
        events_data наполняем desired_uids и добавляем/обновляем, и только потом
        удаляем те, чей uid не встретился на странице.
        """
        existing = {e.uid: e for e in case.events}
        desired_uids: set[uuid.UUID] = set()

        new_events: list[Event] = []
        updated_events: list[Event] = []

        # 1. Проход по событиям, которые есть на актуальной странице
        for item in events_data:
            # Определяем uid события
            uid = event_uid(
                case.card_key, item["event_date"], item["state_description"]
            )
            # Портал может отдать две одинаковые строки (та же дата, то же состояние) —
            # считаем их одним событием. Обработать второе нельзя: uid у него тот же, и
            # UNIQUE ix_event_uid уронит commit вместе со всей транзакцией дела.
            if uid in desired_uids:
                continue
            # Добавляем событие в список собтий, которые мы хотим увидеть  в БД
            desired_uids.add(uid)
            # пытаемся найти uid среди уже существующих
            existing_event = existing.get(uid)
            # Если событие не найдено среди существующих - создаем новое
            if existing_event is None:
                event = Event(
                    uid=uid,
                    event_date=item["event_date"],
                    state_description=item["state_description"],
                    document_str=item.get("document_str"),
                    published_at=item.get("published_at"),
                )
                case.events.append(event)
                new_events.append(event)
            # Если событие нашлось — сверяем изменяемые поля. Проверяем КАЖДОЕ: раньше
            # здесь была одна ветка по document_str, и правка любого другого поля молча
            # терялась бы, а событие не попадало в updated_events.
            else:
                changed = False
                for field in ("document_str", "published_at"):
                    if getattr(existing_event, field) != item.get(field):
                        setattr(existing_event, field, item.get(field))
                        changed = True
                if changed:
                    updated_events.append(existing_event)

        # Удаляем события, пропавшие со страницы (cascade delete-orphan уберёт их из БД).
        removed_events = [e for e in case.events if e.uid not in desired_uids]
        for event in removed_events:
            case.events.remove(event)

        return new_events, updated_events, removed_events
