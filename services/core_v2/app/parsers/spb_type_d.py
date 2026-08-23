"""Парсер карточки дела мировых судов Санкт-Петербурга — страница типа D.

Портал один на весь регион (mirsud.spb.ru, 211 судов). Примеры страниц лежат в
html_examples/case_mirsud_78MS* — там по образцу на каждый вид производства.

Достаёт скалярные поля дела, ФИО судьи, стороны и события «Движения дела». Истории
местонахождения, судебных заседаний и документов на страницах портала нет вовсе —
эти ключи отдаём пустыми списками.

Номер дела и УИД парсер не отдаёт: их достаёт слой клиента (SpbMirCourtClient.
extract_case_code и CourtClient.extract_uid) ещё до разбора, потому что оба входят
в ключ карточки. Номер там берётся из <title>, и это осознанно: в <h1> он подписан
по-разному в зависимости от вида производства («Гражданское дело 2-2976/2026-98»,
«Дело об АП 5-1688/2026-9», «Уголовное дело 1-175/2026-208»), а <title> и хлебные
крошки у всех одинаковы — «Судебное дело №<номер>».

ВАЖНО (грабли разметки портала):
- НА СТРАНИЦЕ ДВА ПРЕДСТАВЛЕНИЯ ОДНИХ И ТЕХ ЖЕ ДАННЫХ, и это главные грабли. Экранное
  лежит в section.case-info и свёрстано дивами: <b class="table-title">метка</b> плюс
  <span class="list table-list">значение</span>, причём у сторон и событий подписи
  колонок повторяются В КАЖДОЙ записи. Печатное лежит в div.case-print и свёрстано
  настоящими таблицами с <thead>. Разбираем ТОЛЬКО печатное: там одна строка на запись
  и честная шапка. Если разбирать экранное, стороны и события придётся собирать из
  повторяющихся подписей, а любая правка вёрстки портала это ломает.
- В печатной таблице основных сведений метка — это просто первый <td> строки, без
  всякого класса. Класс table-title есть только в ЭКРАННОМ представлении, и брать
  подписи по нему нельзя: они уведут в другое дерево.
- Набор строк основных сведений различается по виду производства: «Сущность спора» и
  «Дата принятия к производству» есть только у гражданских дел, у КоАП, уголовных и
  материалов их нет вовсе (Angular не рисует строку через ng-if). Отсутствие метки —
  норма, а не поломка: такие поля отдаём None.
- Таблиц с классом case-print__table три, и порядок у них пока постоянный, но ищем их
  ПО СОСТАВУ ШАПКИ. Рядом на странице живёт ещё table.personal-data — форма ввода
  ключа доступа к документам для участников дела; к данным дела она отношения не имеет.
- Событие и его результат портал склеивает САМ, через « / » («Решение вопроса о
  принятии заявления / Заявление принято»), — в отличие от типа B, где их приходится
  сшивать из двух колонок. Кладём строку как есть.
- Колонка «Время события» разбирается и попадает в Event.event_date вместе с датой.
  Время МЕСТНОЕ для суда — портал пояса не указывает; в момент его превращает слой БД
  (app/timezones.to_utc). В identity события время не входит — только дата, см.
  докстринг event_uid.
- «Дата поступления» здесь есть у ВСЕХ видов производства, а не только у гражданских,
  как на других порталах. Метки «Дата регистрации» на портале нет, поэтому
  registration_date у дел Петербурга всегда None.
- Карточка рисуется фоновой задачей портала уже после загрузки страницы (см.
  app/courts/spb_mir_court.py). Если браузер отдал документ до отрисовки, таблиц в нём
  ещё нет — разбор такого документа обязан вернуть пустой результат, а не упасть.
"""
from datetime import date, datetime, time

from bs4 import BeautifulSoup, Tag

from app.parsers.base import CaseParser
from app.parsers.parsed_case import ParsedCase, ParsedEvent, ParsedSide
from app.parsers.text import clean, clean_or_none, parse_date, parse_local_datetime




# Печатное представление карточки — только его и разбираем (см. докстринг модуля).
PRINT_TABLE_SELECTOR = "div.case-print table.case-print__table"

# Метка основных сведений -> (поле результата, как разобрать значение).
# Метки сверяем ЦЕЛИКОМ и без учёта регистра: «Дата поступления» и «Дата принятия к
# производству» начинаются одинаково, и совпадение по префиксу свело бы их в одно поле.
CARD_FIELDS: dict[str, tuple[str, object]] = {
    "сущность спора": ("category", clean_or_none),
    "дата поступления": ("receipt_date", parse_date),
    "дата принятия к производству": ("accepted_date", parse_date),
    "статус": ("status", clean_or_none),
}

# Метка судьи стоит в той же таблице, но судья — не скалярное поле дела, а связь,
# поэтому в CARD_FIELDS его нет и обрабатывается он отдельно.
JUDGE_LABEL = "судья"

# Шапки, по которым узнаём таблицы. Достаточно первой колонки: она уникальна.
SIDES_HEADER = "ФИО / наименование"
EVENTS_HEADER = "Описание события / Результат события"

# Колонки таблицы сторон и таблицы движения дела. Порядок у портала фиксирован, а
# шапка уже опознана, так что внутри таблицы берём ячейки по индексу.
SIDE_NAME_COL, SIDE_ROLE_COL = 0, 1
EVENT_DESCRIPTION_COL, EVENT_DATE_COL, EVENT_TIME_COL, EVENT_PUBLISHED_COL = 0, 1, 2, 3


def _print_tables(soup: BeautifulSoup) -> list[Tag]:
    """Таблицы печатного представления карточки."""
    return soup.select(PRINT_TABLE_SELECTOR)


def _table_with_header(tables: list[Tag], header: str) -> Tag | None:
    """Таблица, у которой в шапке есть такой заголовок (или None).

    Ищем по шапке, а не по порядковому номеру: порядок таблиц — не гарантия портала,
    а наблюдение, и переставленные местами блоки молча перепутали бы стороны с
    событиями.
    """
    for table in tables:
        if any(clean(th.get_text()) == header for th in table.select("th")):
            return table
    return None


def _card_table(tables: list[Tag]) -> Tag | None:
    """Таблица основных сведений — единственная печатная таблица без шапки."""
    for table in tables:
        if not table.select("th"):
            return table
    return None


def _data_rows(table: Tag) -> list[Tag]:
    """Строки таблицы с данными (у строк шапки ячеек td нет)."""
    return [row for row in table.find_all("tr") if row.find("td")]


def _parse_card(table: Tag) -> tuple[dict, list[str]]:
    """Разобрать таблицу основных сведений в (поля дела, список судей).

    Возвращаются ВСЕ ключи карточки: отсутствующая на странице метка приходит как None,
    и тогда пропавшее на портале значение корректно обнуляется в БД, а не остаётся
    стухшим (см. CaseRepository.upsert_by_uid_court_code).
    """
    card: dict = {field: None for field, _ in CARD_FIELDS.values()}
    judge_names: list[str] = []

    for row in _data_rows(table):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = clean(cells[0].get_text()).lower()
        value_cell = cells[1]

        if label == JUDGE_LABEL:
            judge = clean_or_none(value_cell.get_text())
            if judge:
                judge_names.append(judge)
            continue

        slot = CARD_FIELDS.get(label)
        if slot is None:
            # «№ участка», «Район», «УИД» — им в карточке дела места нет: суд уже
            # известен из ссылки, а УИД отдаёт клиент.
            continue
        field, convert = slot
        card[field] = convert(value_cell.get_text())

    return card, judge_names


def _parse_sides(table: Tag) -> list[dict]:
    """Разобрать таблицу сторон: ФИО/наименование и вид лица.

    Роль берём ровно как на портале («Истец», «Ответчик», «Привлекаемое лицо»,
    «Подсудимый») — приводить виды производства к общему словарю не наше дело.
    """
    sides: list[dict] = []
    for row in _data_rows(table):
        cells = row.find_all("td")
        if len(cells) <= SIDE_ROLE_COL:
            continue
        full_name = clean_or_none(cells[SIDE_NAME_COL].get_text())
        if not full_name:
            # Без наименования сторона неотличима от пустой строки таблицы.
            continue
        sides.append(
            ParsedSide(
                role=clean_or_none(cells[SIDE_ROLE_COL].get_text()),
                full_name=full_name,
            )
        )
    return sides


def _parse_events(table: Tag) -> list[dict]:
    """Разобрать таблицу движения дела в список событий.

    Строки без даты пропускаем: дата входит в identity события, и без неё не посчитать
    uid. Состояние дела отсюда, в отличие от типа B, НЕ выводим — на этом портале есть
    отдельная метка «Статус», и она честнее последней строки таблицы.
    """
    events: list[dict] = []
    for row in _data_rows(table):
        cells = row.find_all("td")
        if len(cells) <= EVENT_PUBLISHED_COL:
            continue

        description = clean(cells[EVENT_DESCRIPTION_COL].get_text())
        event_date = parse_local_datetime(
            cells[EVENT_DATE_COL].get_text(), cells[EVENT_TIME_COL].get_text()
        )
        if not description or event_date is None:
            continue

        events.append(
            ParsedEvent(
                event_date=event_date,
                # Портал сам склеил событие с результатом через « / » — сшивать нечего.
                state_description=description,
                # Документов-оснований на страницах портала нет — колонки под них тоже.
                document_str=None,
                published_at=parse_date(cells[EVENT_PUBLISHED_COL].get_text()),
            )
        )
    return events


class SpbTypeDParser(CaseParser):
    """Парсер страниц типа D (мировые суды Санкт-Петербурга, mirsud.spb.ru)."""

    page_type = "D"

    def parse(self, html: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        tables = _print_tables(soup)

        # === ОСНОВНЫЕ СВЕДЕНИЯ: скалярные поля дела и судья ==================
        card: dict = {field: None for field, _ in CARD_FIELDS.values()}
        judge_names: list[str] = []
        card_table = _card_table(tables)
        if card_table is not None:
            card, judge_names = _parse_card(card_table)

        # === СТОРОНЫ ПО ДЕЛУ ================================================
        sides_table = _table_with_header(tables, SIDES_HEADER)
        sides = _parse_sides(sides_table) if sides_table is not None else []

        # === ДВИЖЕНИЕ ДЕЛА ==================================================
        events_table = _table_with_header(tables, EVENTS_HEADER)
        events = _parse_events(events_table) if events_table is not None else []

        # card содержит РОВНО те поля, которые бывают на карточке этого портала: они
        # засеяны из CARD_FIELDS в начале parse. Остальные скалярные поля ParsedCase
        # остаются UNSET, и колонки в БД по ним не трогаются — в частности «Номер
        # заявления» и «Дата регистрации», которых у Петербурга нет.
        #
        # Историю местонахождения, судебные заседания и документы портал не публикует
        # вовсе — соответствующих блоков на странице не существует, списки пусты.
        return ParsedCase(
            **card,
            judge_names=judge_names,
            sides=sides,
            events=events,
        )
