"""Мелкие текстовые хелперы, общие для всех парсеров.

Собраны здесь потому, что в старом core существовали в ЧЕТЫРЁХ идентичных копиях —
по одной приватной в каждом парсере плюс общая в msudrf_shared.py. Это не «может
пригодиться», а буквально один и тот же код, скопированный четыре раза.

Что сюда НЕ переехало и почему: разбор даты со временем у парсера Москвы принимает ОДНУ
строку («21.08.2026 15:30» одной ячейкой), а у msudrf и Петербурга — ДВЕ (отдельные
колонки даты и времени). Это разные функции с разным входом, и сливать их значило бы
придумывать общий интерфейс там, где его нет. Поэтому parse_local_datetime здесь — для
двухколоночных порталов, а у Москвы остался свой _parse_datetime.
"""
from datetime import date, datetime, time


def clean(text: str) -> str:
    """Схлопнуть любые пробелы/переводы строк в один пробел и обрезать края."""
    return " ".join(text.split())


def clean_or_none(text: str) -> str | None:
    """Как clean, но пустое значение → None (в БД такому полю место NULL, а не '')."""
    return clean(text) or None



# Формат дат на портале — везде один, и в карточке, и в таблице событий.
DATE_FORMAT = "%d.%m.%Y"


# Формат времени события — «14:30». Колонка есть не у всех: у типа B она всегда, у типа C
# то есть, то нет (Пермь отдаёт, Адыгея нет), причём различается даже внутри одного региона.
TIME_FORMAT = "%H:%M"


def parse_date(text: str) -> date | None:
    """Разобрать дату формата ДД.ММ.ГГГГ; пустое/некорректное значение → None."""
    text = clean(text)
    if not text:
        return None
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        return None


def parse_local_datetime(date_text: str, time_text: str = "") -> datetime | None:
    """Дата и (необязательное) время одной строкой — в МЕСТНОЕ время суда.

    Возвращает naive datetime: портал пишет своим местным временем и пояса не указывает,
    а превращает его в момент уже слой БД (см. app/timezones.to_utc). Отдавать
    отсюда aware-значение нельзя — парсер про суд ничего не знает.

    Времени нет (колонки нет, ячейка пуста или мусор) → местная полночь. Отличить её от
    события, которое действительно в 00:00, потом нельзя; это осознанный размен, ровно
    так же устроен разбор заседаний Москвы (_parse_datetime в moscow_type_a.py).

    Нет даты → None: без неё событие не идентифицировать.
    """
    day = parse_date(date_text)
    if day is None:
        return None
    moment = clean(time_text)
    if moment:
        try:
            parsed = datetime.strptime(moment, TIME_FORMAT).time()
            return datetime.combine(day, parsed)
        except ValueError:
            # Мусор в колонке времени не должен ронять разбор всего события.
            pass
    return datetime.combine(day, time.min)
