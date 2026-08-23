"""Доступ к истории местонахождения (PlaceHistory) в БД: детерминированный uid + сверка со страницей."""
import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.parsers.parsed_case import ParsedPlace
from app.models import Case, PlaceHistory

# Фиксированный namespace для uid местонахождений (задан один раз, менять нельзя —
# иначе uid всех строк «поедут» и повторный парсинг перестанет их узнавать).
# Свой, отдельный от EVENT_UID_NAMESPACE: иначе строки разных сущностей с одинаковым
# текстом и датой получили бы один и тот же uid.
PLACE_HISTORY_UID_NAMESPACE = uuid.UUID("6b1f3c02-9a4d-5e77-b8c1-2f0a7d43e915")


def place_history_uid(
    card_key: str, place_date: date, place_description: str
) -> uuid.UUID:
    """
    Детерминированный uid местонахождения из обязательных (identity) полей.

    identity = карточка + дата + местонахождение.

    КАРТОЧКА, а не дело: card_key — это «УИД | код суда | номер дела»
    (Case.card_key). По одному УИД карточек бывает несколько, а uid здесь уникален
    глобально — считай мы его от УИД, строки соседних карточек столкнулись бы.

    comment сюда НЕ входит — он изменяем (на портале дописывается позже), и его
    правка должна детектиться как UPDATE той же строки, а не как новая строка.
    """
    key = "|".join([card_key, place_date.isoformat(), place_description])
    return uuid.uuid5(PLACE_HISTORY_UID_NAMESPACE, key)


class PlaceHistoryRepository:
    """Чтение и запись истории местонахождения дела. Работает в рамках переданной сессии."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def sync_place_history(
        self, case: Case, places_data: list[ParsedPlace]
    ) -> tuple[list[PlaceHistory], list[PlaceHistory], list[PlaceHistory]]:
        """Привести историю местонахождения дела к тому, что сейчас на странице.

        Возвращает (new_places, updated_places, removed_places):
        - uid новый                      → создаём строку        → new_places;
        - uid есть, comment изменился    → обновляем поле        → updated_places;
        - uid строки больше нет на странице → удаляем            → removed_places.

        Страница — источник истины. Порядок важен: сначала одним проходом по
        places_data наполняем desired_uids и добавляем/обновляем, и только потом
        удаляем те, чей uid не встретился на странице.
        """
        existing = {p.uid: p for p in case.place_history}
        desired_uids: set[uuid.UUID] = set()

        new_places: list[PlaceHistory] = []
        updated_places: list[PlaceHistory] = []

        # 1. Проход по строкам, которые есть на актуальной странице
        for item in places_data:
            # Определяем uid строки
            uid = place_history_uid(
                case.card_key, item.place_date, item.place_description
            )
            # Портал иногда отдаёт две побайтово одинаковые строки (та же дата, то же
            # местонахождение, пустой комментарий) — считаем их одной записью. Обработать
            # вторую нельзя: uid у неё тот же, и UNIQUE ix_place_history_uid уронит commit
            # вместе со всей транзакцией дела.
            if uid in desired_uids:
                continue
            # Добавляем uid в список строк, которые мы хотим увидеть в БД
            desired_uids.add(uid)
            # пытаемся найти uid среди уже существующих
            existing_place = existing.get(uid)
            # Если строка не найдена среди существующих - создаём новую
            if existing_place is None:
                place = PlaceHistory(
                    uid=uid,
                    place_date=item.place_date,
                    place_description=item.place_description,
                    comment=item.comment,
                )
                case.place_history.append(place)
                new_places.append(place)
            # Если строка нашлась, но сменился/появился комментарий - обновляем
            elif existing_place.comment != item.comment:
                existing_place.comment = item.comment
                updated_places.append(existing_place)

        # Удаляем строки, пропавшие со страницы (cascade delete-orphan уберёт их из БД).
        removed_places = [p for p in case.place_history if p.uid not in desired_uids]
        for place in removed_places:
            case.place_history.remove(place)

        return new_places, updated_places, removed_places
