"""Парсер карточки дела мировых судов Москвы (mos-sud.ru) — страница типа A.

Достаёт скалярные поля дела, ФИО судьи, стороны по делу, события «Истории состояний»,
строки «Истории местонахождения» и судебные заседания.
- Скалярные поля (номер заявления, номер входящего, дата поступления, категория,
  текущее состояние), судья и стороны лежат в «карточке» — блоке пар «метка/значение»:
  div.detail-cart div.row_card > (.left = метка, .right = значение).
- События — в таблице под заголовком <h3>История состояний</h3>
  (3 колонки: Дата / Состояние / Документ-основание).
- Местонахождения — в таблице под <h3>История местонахождения</h3>
  (3 колонки: Дата / Местонахождение / Комментарий), она идёт ниже в том же контейнере.
- Судебные заседания — в отдельной вкладке div#sessions (внутри #tabs-2), 6 колонок:
  Дата и время / Зал / Стадия / Результат / Основание / Проводилась видеозапись.

ВАЖНО (грабли разметки портала):
- В метке «Cудья» первая буква — ЛАТИНСКАЯ «C» (U+0043), а не кириллическая «С».
  Поэтому матчим устойчиво по regex [СсCc]удья, иначе селектор молча не сработает.
  Для «Стороны» на всякий случай матчим так же терпимо к латинской «C».
- Значения обильно обложены пробелами/переводами строк — везде чистим текст.
- Стороны лежат одной строкой в .right в виде «<strong>Роль: </strong>ФИО<br>» —
  ролей может быть несколько (истец, ответчик, привлекаемое лицо и т.п.).
- Обе таблицы историй лежат в одном контейнере, у них нет id, а классы (mainTable
  и т.п.) совпадают, причём порядок токенов класса плавает. Поэтому таблицу ищем
  по тексту заголовка <h3>, а не по классу.
- В конце страницы те же таблицы продублированы скрытыми клонами внутри
  div#modalTable (мобильные модалки). У клонов НЕТ <h3>, поэтому якорь по заголовку
  спасает от дублей — уходить с него на поиск по классу нельзя. Таблицу заседаний это
  тоже касается: её клон лежит там же, но id="sessions" клон НЕ несёт, поэтому анкор по
  id защищает от удвоения так же, как <h3> защищает таблицы историй.
- Набор меток карточки РАЗЛИЧАЕТСЯ по типам дел: у гражданского есть «Номер заявления»,
  «Номер входящего документа», «Дата поступления», «Категория дела», а у дела по КоАП
  вместо них «Номер дела», «Дата регистрации», «Статья КоАП РФ». Отсутствующая метка
  даёт None, а не падение (см. CARD_FIELDS).
- Метки-синонимы из разных шаблонов сводим в одно поле: «Дата поступления» и
  «Дата регистрации» → receipt_date.
- «Номер заявления» (гражданское) и «Номер дела» (КоАП) — ОДИН И ТОТ ЖЕ слот шаблона.
  Поэтому метку матчим целиком («Номер заявления», «Номер входящего документа»), а не
  по префиксу «Номер»: иначе в application_number уедет номер дела, чьё место в code.
"""
import re
from datetime import date, datetime, time

from bs4 import BeautifulSoup, Tag

from app.parsers.base import CaseParser

# Пары «метка/значение» карточки.
ROW_SELECTOR = "div.detail-cart div.row_card"
LABEL_SELECTOR = "div.left"
VALUE_SELECTOR = "div.right"

# Метка судьи: латинская или кириллическая «С», затем «удья» (кириллица).
JUDGE_LABEL_RE = re.compile(r"^[СсCc]удья\b")
# Метка блока сторон: латинская или кириллическая «С», затем «тороны».
SIDES_LABEL_RE = re.compile(r"^[СсCc]тороны\b")

# Заголовок таблицы событий (движение дела).
STATE_HISTORY_HEADING = "История состояний"
# Заголовок таблицы истории местонахождения дела.
PLACE_HISTORY_HEADING = "История местонахождения"
# Вкладка «Судебные заседания»: <div id="sessions"> внутри #tabs-2. У неё, в отличие от
# таблиц историй, НЕТ заголовка <h3> — поэтому анкор здесь id, а не текст заголовка.
SESSIONS_CONTAINER = "#sessions"
# Формат дат на портале.
DATE_FORMAT = "%d.%m.%Y"
# Формат «Дата и время» в таблице заседаний: «30.07.2026 16:50».
DATETIME_FORMAT = "%d.%m.%Y %H:%M"


def _clean(text: str) -> str:
    """Схлопнуть любые пробелы/переводы строк в один пробел и обрезать края."""
    return " ".join(text.split())


def _parse_date(text: str) -> date | None:
    """Разобрать дату формата ДД.ММ.ГГГГ; пустое/некорректное значение → None."""
    text = _clean(text)
    if not text:
        return None
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        return None


def _parse_datetime(text: str) -> datetime | None:
    """Разобрать «ДД.ММ.ГГГГ ЧЧ:ММ»; пустое/некорректное значение → None.

    Если времени в ячейке нет, откатываемся на одну дату и полночь: время входит в
    identity заседания, поэтому подстановка должна быть детерминированной — иначе uid
    той же строки менялся бы между парсингами.
    """
    text = _clean(text)
    if not text:
        return None
    try:
        return datetime.strptime(text, DATETIME_FORMAT)
    except ValueError:
        only_date = _parse_date(text)
        return datetime.combine(only_date, time.min) if only_date is not None else None


def _clean_or_none(text: str) -> str | None:
    """Как _clean, но пустое значение → None (в БД такому полю место NULL, а не '')."""
    return _clean(text) or None


def _cell_or_none(cells: list, index: int) -> str | None:
    """Значение ячейки по индексу через _clean_or_none; нет такой колонки → None.

    Нужен для хвостовых колонок таблицы заседаний: их может не оказаться в разметке,
    и падать из-за этого нельзя.
    """
    return _clean_or_none(cells[index].get_text()) if index < len(cells) else None


# Скалярные поля дела из карточки: (метка, поле Case, преобразование значения).
# Первую букву метки, как и в JUDGE_LABEL_RE, матчим терпимо к латинскому двойнику
# (Н/H, Д/D, К/K, Т/T) — портал уже подкладывал латинскую «C» в «Cудья».
# Метку сверяем целиком (см. граблю про «Номер заявления» / «Номер дела» в docstring).
CARD_FIELDS = (
    (re.compile(r"^[НнHh]омер заявления\b"), "application_number", _clean_or_none),
    (
        re.compile(r"^[НнHh]омер входящего документа\b"),
        "incoming_number",
        _clean_or_none,
    ),
    # «Дата поступления» (гражданское) и «Дата регистрации» (КоАП) — один и тот же
    # смысл в разных шаблонах, кладём в одно поле. Матчим именно эти две метки, чтобы
    # не поймать «Дата рассмотрения дела в первой инстанции» — у неё своё поле.
    (
        re.compile(r"^[ДдDd]ата (поступления|регистрации)\b"),
        "receipt_date",
        _parse_date,
    ),
    (re.compile(r"^[КкKk]атегория дела\b"), "category", _clean_or_none),
    (re.compile(r"^[ТтTt]екущее состояние\b"), "status", _clean_or_none),
)


def _parse_state_history(soup: BeautifulSoup) -> list[dict]:
    """Разобрать таблицу «История состояний» в список событий.

    Возвращает {"event_date": date, "state_description": str, "document_str": str|None}.
    Обязательны дата и описание состояния (образуют identity события) — строки без
    любого из них пропускаем. Пустое «Документ-основание» → document_str = None.
    """
    heading = soup.find(
        "h3", string=lambda s: s is not None and _clean(s) == STATE_HISTORY_HEADING
    )
    if heading is None:
        return []
    table = heading.find_next("table")
    if table is None:
        return []

    events: list[dict] = []
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        event_date = _parse_date(cells[0].get_text())
        state_description = _clean(cells[1].get_text())
        document_str = _clean(cells[2].get_text()) or None

        # Дата + описание обязательны — иначе событие не может участвовать в детекте.
        if event_date is None or not state_description:
            continue

        events.append(
            {
                "event_date": event_date,
                "state_description": state_description,
                "document_str": document_str,
            }
        )
    return events


def _parse_place_history(soup: BeautifulSoup) -> list[dict]:
    """Разобрать таблицу «История местонахождения» в список местонахождений.

    Возвращает {"place_date": date, "place_description": str, "comment": str|None}.
    Обязательны дата и местонахождение (образуют identity строки) — строки без
    любого из них пропускаем. Пустой «Комментарий» → comment = None.
    """
    heading = soup.find(
        "h3", string=lambda s: s is not None and _clean(s) == PLACE_HISTORY_HEADING
    )
    if heading is None:
        return []
    table = heading.find_next("table")
    if table is None:
        return []

    places: list[dict] = []
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        place_date = _parse_date(cells[0].get_text())
        place_description = _clean(cells[1].get_text())
        comment = _clean(cells[2].get_text()) or None

        # Дата + местонахождение обязательны — иначе строка не может участвовать в детекте.
        if place_date is None or not place_description:
            continue

        places.append(
            {
                "place_date": place_date,
                "place_description": place_description,
                "comment": comment,
            }
        )
    return places


def _parse_court_sessions(soup: BeautifulSoup) -> list[dict]:
    """Разобрать вкладку «Судебные заседания» в список заседаний.

    Возвращает {"session_date": datetime, "place": str|None, "stage": str,
    "result": str|None, "basis": str|None}. Обязательны дата-время и стадия (образуют
    identity заседания) — строки без любого из них пропускаем.

    Колонок на портале шесть: Дата и время / Зал / Стадия / Результат / Основание /
    Проводилась видеозапись. Последнюю не сохраняем: она пуста во всех виденных делах.
    """
    box = soup.select_one(SESSIONS_CONTAINER)
    if box is None:
        return []  # у приказных дел вкладки заседаний нет совсем — это норма

    sessions: list[dict] = []
    for row in box.select("table tbody tr"):
        cells = row.find_all("td")
        # Минимум — дата, зал, стадия: из них берётся identity. Хвостовых колонок
        # («Результат», «Основание») в разметке может не оказаться.
        if len(cells) < 3:
            continue

        session_at = _parse_datetime(cells[0].get_text())
        stage = _clean(cells[2].get_text())

        # Дата-время и стадия обязательны — иначе заседание не может участвовать в детекте.
        if session_at is None or not stage:
            continue

        sessions.append(
            {
                "session_date": session_at,
                "place": _clean_or_none(cells[1].get_text()),
                "stage": stage,
                "result": _cell_or_none(cells, 3),
                "basis": _cell_or_none(cells, 4),
            }
        )
    return sessions


def _parse_sides(value_el: Tag) -> list[dict]:
    """Разобрать .right блока «Стороны» в список {"role", "full_name"}.

    Формат значения: <strong>Роль: </strong>ФИО<br> — повторяется для каждой стороны.
    Роль берём из <strong> (без хвостового двоеточия), ФИО — текст между этим <strong>
    и следующим <br>/<strong>. Порядок сохраняем; сопоставление роли с типом стороны —
    задача слоя БД (SideRepository), парсер отдаёт сырую роль строкой.
    """
    sides: list[dict] = []
    for strong in value_el.find_all("strong"):
        role = _clean(strong.get_text()).rstrip(":").strip()

        parts: list[str] = []
        for sib in strong.next_siblings:
            if isinstance(sib, Tag):
                if sib.name in ("strong", "br"):
                    break  # конец значения текущей роли
                parts.append(sib.get_text())
            else:  # NavigableString
                parts.append(str(sib))
        full_name = _clean(" ".join(parts))

        if role and full_name:
            sides.append({"role": role, "full_name": full_name})
    return sides


class MoscowTypeAParser(CaseParser):
    """Парсер страниц типа A (мировые суды Москвы)."""

    page_type = "A"

    def parse(self, html: str) -> dict:
        """Разобрать карточку: поля дела, судьи, стороны, «История состояний» и «История местонахождения»."""
        soup = BeautifulSoup(html, "lxml")

        # === КАРТОЧКА: скалярные поля дела, судьи и стороны =================
        # Идём по строкам «метка/значение» и раскладываем по типу метки:
        # «Cудья» → judge_names, «Cтороны» → sides (роль + ФИО), остальное — по CARD_FIELDS.
        # Все ключи card заведены заранее: страница — источник истины, поэтому пропавшую
        # на ней метку отдаём как None (поле в БД обнулится), а не молча опускаем ключ.
        card: dict = {field: None for _, field, _ in CARD_FIELDS}
        judge_names: list[str] = []
        sides: list[dict] = []
        for row in soup.select(ROW_SELECTOR):
            label_el = row.select_one(LABEL_SELECTOR)
            value_el = row.select_one(VALUE_SELECTOR)
            if label_el is None or value_el is None:
                continue

            # Метку сверяем только после _clean: значения и метки обложены пробелами
            # и переводами строк, причём у каждого поля по-своему.
            label = _clean(label_el.get_text())
            if JUDGE_LABEL_RE.match(label):
                name = _clean(value_el.get_text())
                if name:
                    judge_names.append(name)
            elif SIDES_LABEL_RE.match(label):
                sides.extend(_parse_sides(value_el))
            else:
                for label_re, field, convert in CARD_FIELDS:
                    if label_re.match(label):
                        card[field] = convert(value_el.get_text())
                        break

        # === ИСТОРИЯ СОСТОЯНИЙ: события =====================================
        # Отдельная таблица под <h3>История состояний</h3> — разбираем в события.
        events = _parse_state_history(soup)

        # === ИСТОРИЯ МЕСТОНАХОЖДЕНИЯ =======================================
        # Соседняя таблица под <h3>История местонахождения</h3>.
        place_history = _parse_place_history(soup)

        # === СУДЕБНЫЕ ЗАСЕДАНИЯ ============================================
        # Отдельная вкладка div#sessions (см. _parse_court_sessions).
        court_sessions = _parse_court_sessions(soup)

        return {
            **card,
            "judge_names": judge_names,
            "sides": sides,
            "events": events,
            "place_history": place_history,
            "court_sessions": court_sessions,
        }
