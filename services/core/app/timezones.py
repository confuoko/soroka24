"""Часовые пояса судов: где какое местное время.

Зачем. Портал суда пишет время СВОИМ местным временем и пояса не указывает: «заседание
14.08 в 10:00» в Магадане и в Москве — это разные моменты, отличающиеся на восемь часов.
Чтобы хранить однозначный момент (в БД везде UTC) и уметь показать его обратно так, как
он написан на сайте суда, нужно знать пояс каждого суда.

Почему ключ — название региона, а не префикс кода суда. Коды идут по классификатору
sudrf, где республики пронумерованы по алфавиту: «02» — это Республика Алтай, а вовсе не
Башкортостан, как в автомобильных кодах. Ошибиться тут легко, а цена ошибки — молча
сдвинутое на часы время, которое никак себя не проявит. Название региона однозначно.

Почему не в data/courts.json. Тот файл перегенерируется скриптом build_courts_json.py из
сохранённой страницы sudrf.ru и содержит ровно пять ключей — пояс при перегенерации
просто исчез бы. Источник истины здесь.

Карта построена по данным courts.json (7747 судов, 85 регионов) и покрывает их все:
timezone_for() падает на незнакомом регионе, а не подставляет Москву молча.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

# Регион (точное значение Court.region) -> IANA-пояс.
TZ_BY_REGION = {
    # --- Europe/Astrakhan
    "Астраханская область": "Europe/Astrakhan",  # 30
    # --- Europe/Kaliningrad
    "Калининградская область": "Europe/Kaliningrad",  # 39
    # --- Europe/Kirov
    "Кировская область": "Europe/Kirov",  # 43
    # --- Europe/Moscow
    "Архангельская область": "Europe/Moscow",  # 29
    "Белгородская область": "Europe/Moscow",  # 31
    "Брянская область": "Europe/Moscow",  # 32
    "Владимирская область": "Europe/Moscow",  # 33
    "Вологодская область": "Europe/Moscow",  # 35
    "Воронежская область": "Europe/Moscow",  # 36
    "Город Москва": "Europe/Moscow",  # 77
    "Город Санкт-Петербург": "Europe/Moscow",  # 78
    "Ивановская область": "Europe/Moscow",  # 37
    "Кабардино-Балкарская Республика": "Europe/Moscow",  # 07
    "Калужская область": "Europe/Moscow",  # 40
    "Карачаево-Черкесская Республика": "Europe/Moscow",  # 09
    "Костромская область": "Europe/Moscow",  # 44
    "Краснодарский край": "Europe/Moscow",  # 23
    "Курская область": "Europe/Moscow",  # 46
    "Ленинградская область": "Europe/Moscow",  # 47
    "Липецкая область": "Europe/Moscow",  # 48
    "Московская область": "Europe/Moscow",  # 50
    "Мурманская область": "Europe/Moscow",  # 51
    "Ненецкий автономный округ": "Europe/Moscow",  # 29
    "Нижегородская область": "Europe/Moscow",  # 52
    "Новгородская область": "Europe/Moscow",  # 53
    "Орловская область": "Europe/Moscow",  # 57
    "Пензенская область": "Europe/Moscow",  # 58
    "Псковская область": "Europe/Moscow",  # 60
    "Республика Адыгея": "Europe/Moscow",  # 01
    "Республика Дагестан": "Europe/Moscow",  # 05
    "Республика Ингушетия": "Europe/Moscow",  # 06
    "Республика Калмыкия": "Europe/Moscow",  # 08
    "Республика Карелия": "Europe/Moscow",  # 10
    "Республика Коми": "Europe/Moscow",  # 11
    "Республика Марий Эл": "Europe/Moscow",  # 12
    "Республика Мордовия": "Europe/Moscow",  # 13
    "Республика Северная Осетия-Алания": "Europe/Moscow",  # 15
    "Республика Татарстан": "Europe/Moscow",  # 16
    "Ростовская область": "Europe/Moscow",  # 61
    "Рязанская область": "Europe/Moscow",  # 62
    "Смоленская область": "Europe/Moscow",  # 67
    "Ставропольский край": "Europe/Moscow",  # 26
    "Тамбовская область": "Europe/Moscow",  # 68
    "Тверская область": "Europe/Moscow",  # 69
    "Тульская область": "Europe/Moscow",  # 71
    "Чеченская Республика": "Europe/Moscow",  # 20
    "Чувашская Республика - Чувашия": "Europe/Moscow",  # 21
    "Ярославская область": "Europe/Moscow",  # 76
    # --- Europe/Samara
    "Самарская область": "Europe/Samara",  # 63
    "Удмуртская Республика": "Europe/Samara",  # 18
    # --- Europe/Saratov
    "Саратовская область": "Europe/Saratov",  # 64
    # --- Europe/Simferopol
    "Город Севастополь": "Europe/Simferopol",  # 92
    "Республика Крым": "Europe/Simferopol",  # 91
    # --- Europe/Ulyanovsk
    "Ульяновская область": "Europe/Ulyanovsk",  # 73
    # --- Europe/Volgograd
    "Волгоградская область": "Europe/Volgograd",  # 34
    # --- Asia/Anadyr
    "Чукотский автономный округ": "Asia/Anadyr",  # 87
    # --- Asia/Barnaul
    "Алтайский край": "Asia/Barnaul",  # 22
    # --- Asia/Chita
    "Забайкальский край": "Asia/Chita",  # 75
    # --- Asia/Irkutsk
    "Иркутская область": "Asia/Irkutsk",  # 38
    "Республика Бурятия": "Asia/Irkutsk",  # 04
    # --- Asia/Kamchatka
    "Камчатский край": "Asia/Kamchatka",  # 41
    # --- Asia/Krasnoyarsk
    "Красноярский край": "Asia/Krasnoyarsk",  # 24
    "Республика Алтай": "Asia/Krasnoyarsk",  # 02
    "Республика Тыва": "Asia/Krasnoyarsk",  # 17
    "Республика Хакасия": "Asia/Krasnoyarsk",  # 19
    # --- Asia/Magadan
    "Магаданская область": "Asia/Magadan",  # 49
    # --- Asia/Novokuznetsk
    "Кемеровская область - Кузбасс": "Asia/Novokuznetsk",  # 42
    # --- Asia/Novosibirsk
    "Новосибирская область": "Asia/Novosibirsk",  # 54
    # --- Asia/Omsk
    "Омская область": "Asia/Omsk",  # 55
    # --- Asia/Sakhalin
    "Сахалинская область": "Asia/Sakhalin",  # 65
    # --- Asia/Tomsk
    "Томская область": "Asia/Tomsk",  # 70
    # --- Asia/Vladivostok
    "Еврейская автономная область": "Asia/Vladivostok",  # 79
    "Приморский край": "Asia/Vladivostok",  # 25
    "Хабаровский край": "Asia/Vladivostok",  # 27
    # --- Asia/Yakutsk
    "Амурская область": "Asia/Yakutsk",  # 28
    "Республика Саха (Якутия)": "Asia/Yakutsk",  # 14
    # --- Asia/Yekaterinburg
    "Курганская область": "Asia/Yekaterinburg",  # 45
    "Оренбургская область": "Asia/Yekaterinburg",  # 56
    "Пермский край": "Asia/Yekaterinburg",  # 59
    "Республика Башкортостан": "Asia/Yekaterinburg",  # 03
    "Свердловская область": "Asia/Yekaterinburg",  # 66
    "Тюменская область": "Asia/Yekaterinburg",  # 72
    "Ханты-Мансийский автономный округ - Югра (Тюменская область)": "Asia/Yekaterinburg",  # 86
    "Челябинская область": "Asia/Yekaterinburg",  # 74
    "Ямало-Ненецкий автономный округ": "Asia/Yekaterinburg",  # 89
}

# Суды, чей пояс отличается от пояса своего региона. Два субъекта РФ не укладываются
# в один пояс:
#
# * Республика Саха (Якутия) — три пояса: основной Asia/Yakutsk (+9), восточные улусы
#   (Оймяконский, Усть-Янский, Верхоянский) живут по Asia/Vladivostok (+10), а колымские
#   (Среднеколымский, Верхнеколымский, Нижнеколымский, Абыйский, Момский, Аллаиховский) —
#   по Asia/Srednekolymsk (+11);
# * Сахалинская область — Северо-Курильск по Asia/Kamchatka (+12), остальные Asia/Sakhalin (+11).
#
# Заполняется по названию улуса/района в Court.name. Пока запись не заведена, суд получает
# пояс своего региона — ошибка ограничена десятком судов из 7747 и видна как расхождение
# времени заседания с сайтом суда.
TZ_BY_COURT_CODE: dict[str, str] = {}


def timezone_for(region: str, code: str) -> str:
    """IANA-пояс суда: сначала исключение по коду, иначе пояс региона.

    Незнакомый регион — это ошибка данных (в справочнике появился субъект, которого нет в
    карте), и молчать о ней нельзя: подстановка Москвы по умолчанию дала бы неверное время
    без единого признака неисправности.
    """
    override = TZ_BY_COURT_CODE.get(code)
    if override is not None:
        return override
    try:
        return TZ_BY_REGION[region]
    except KeyError:
        raise KeyError(
            f"Не знаю часового пояса региона «{region}» (суд {code}). "
            f"Добавьте его в TZ_BY_REGION."
        ) from None


def to_utc(local: datetime, tz_name: str) -> datetime:
    """Местное время суда (naive, как написано на странице) -> момент в UTC.

    Naive на входе намеренно: парсеры отдают ровно то, что прочитали со страницы, и про
    пояс ничего не знают. Пояс добавляется здесь, на границе с БД.
    """
    return local.replace(tzinfo=ZoneInfo(tz_name)).astimezone(ZoneInfo("UTC"))


def to_court_local(moment: datetime, tz_name: str) -> datetime:
    """Момент из БД -> то же время в поясе суда (aware).

    Обратная к to_utc: по ней получается ровно то, что написано на странице суда.
    """
    return moment.astimezone(ZoneInfo(tz_name))
