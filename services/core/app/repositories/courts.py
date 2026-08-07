"""Доступ к судам (Court) в БД."""
import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.database import Court, CourtLevel

logger = logging.getLogger(__name__)

# Поля суда, которые перезаписываются из JSON-справочника (code — ключ, его не трогаем).
_SYNCED_FIELDS = ("name", "level", "region", "base_url")

# Номер судебного участка в названии суда: «Судебный участок № 235 ...».
_PARTICIPOK_RE = re.compile(r"участок\s*№\s*(\d+)")


def participok_no(name: str) -> Optional[int]:
    """Номер судебного участка из названия суда (или None, если его там нет)."""
    match = _PARTICIPOK_RE.search(name)
    return int(match.group(1)) if match else None


def host_of(base_url: Optional[str]) -> Optional[str]:
    """Хост из адреса сайта суда, в нижнем регистре (или None)."""
    return (urlsplit(base_url or "").hostname or "").lower() or None


def _codes_for_log(courts: list[Court], limit: int = 5) -> str:
    """Коды судов для сообщения в лог — не больше limit штук.

    Перечислять все нельзя: на общем портале вроде mos-sud.ru совпадений сотни, и такая
    строка забивает лог целиком, ничего не объясняя сверх первых нескольких.
    """
    codes = [court.code for court in courts]
    shown = ", ".join(codes[:limit])
    return shown if len(codes) <= limit else f"{shown} и ещё {len(codes) - limit}"


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

    def get_by_participok(self, region_code: str, number: int) -> Optional[Court]:
        """Суд по номеру участка в пределах региона (или None).

        Так определяется суд дела, найденного поиском по УИД: номер участка приходит в
        строке таблицы результатов. Номер берётся из НАЗВАНИЯ суда, а не из его кода —
        совпадают они не всегда (участок № 463 — это код 77MS0466, а 77MS0463 совсем
        другой суд, так что арифметика по коду молча привязала бы дело не туда).

        Фильтр по region_code обязателен: номер участка уникален внутри Москвы и
        Московской области, но по справочнику в целом нет — участок № 1 существует в
        каждом судебном районе десятков регионов.

        Если совпадений несколько, суд НЕ выбираем: взять первый попавшийся значит молча
        привязать дело к чужому суду. В Москве и МО такого быть не должно, поэтому это
        сигнал о поехавшем справочнике — пишем в лог.
        """
        # Кандидатов отбираем по префиксу кода, номер сверяем уже в Python: он выводится
        # регуляркой из названия, и держать это правило в двух видах (здесь и в SQL)
        # значило бы дать им разъехаться. Регион — это несколько сотен строк, дешевле
        # прочитать их, чем дублировать разбор.
        candidates = self._session.scalars(
            select(Court).where(Court.code.startswith(region_code))
        )
        courts = [c for c in candidates if participok_no(c.name) == number]

        if len(courts) > 1:
            logger.warning(
                "Участок № %s в регионе %s есть у нескольких судов (%s) — суд не определён",
                number,
                region_code,
                _codes_for_log(courts),
            )
            return None
        return courts[0] if courts else None

    def get_by_host(self, host: str) -> Optional[Court]:
        """Суд по хосту его сайта (или None).

        Так определяется суд дела, пришедшего ссылкой: на msudrf.ru у каждого участка свой
        поддомен. Как и в get_by_participok, при нескольких совпадениях суд не выбираем —
        общие порталы вроде mos-sud.ru делят хост на сотни судов, и по нему там ничего
        определить нельзя.
        """
        normalized = (host or "").lower()
        if not normalized:
            return None

        # Грубый отбор по подстроке в адресе, точная сверка — по разобранному хосту:
        # LIKE поймал бы и «22.mo.msudrf.ru» в ссылке на «122.mo.msudrf.ru».
        # Спецсимволы LIKE в хосте экранируем, чтобы точка-подчёркивание не стали шаблоном.
        pattern = normalized.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
        candidates = self._session.scalars(
            select(Court).where(Court.base_url.ilike(f"%{pattern}%", escape="\\"))
        )
        courts = [c for c in candidates if host_of(c.base_url) == normalized]

        if len(courts) > 1:
            logger.warning(
                "Хост %s принадлежит нескольким судам (%s) — суд не определён",
                normalized,
                _codes_for_log(courts),
            )
            return None
        return courts[0] if courts else None

    def sync_from_entries(self, entries: list[dict]) -> tuple[int, int]:
        """Создать/обновить суды по коду из записей JSON-справочника.

        Идемпотентно: суды, которых нет в entries, не трогаются и не удаляются.
        Возвращает (создано, обновлено) — обновлённым считается только суд, у которого
        реально изменилось хотя бы одно поле, иначе счётчик равнялся бы всему справочнику.
        """
        created = 0
        updated = 0

        # Одним запросом поднимаем существующие суды в словарь {code: Court}.
        existing = {court.code: court for court in self._session.scalars(select(Court))}

        for entry in entries:
            # Значение из JSON ("mirsud") -> член enum; на неизвестном уровне упадёт ValueError.
            values = {
                "name": entry["name"],
                "level": CourtLevel(entry["level"]),
                "region": entry["region"],
                "base_url": entry.get("base_url"),
            }

            court = existing.get(entry["code"])
            if court is None:
                self._session.add(Court(code=entry["code"], **values))
                created += 1
                continue

            # Пишем только реально изменившиеся поля — так counters не врут.
            changed = [f for f in _SYNCED_FIELDS if getattr(court, f) != values[f]]
            if changed:
                for field in changed:
                    setattr(court, field, values[field])
                updated += 1

        return created, updated
