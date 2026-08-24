"""Парсер карточки дела мировых судов на движке msudrf.ru — страница типа B.

Разметка у движка общая на все регионы; список подключённых — в COURT_BY_DOMAIN
(app/courts/resolver.py). Примеры страниц лежат в html_examples/mo_case_* — они из
Московской области, разметка у остальных регионов та же.

Достаёт скалярные поля дела, ФИО судьи, стороны по делу и события «Движения дела».
Страница поделена на вкладки; всё, что нам нужно, лежит в четырёх из них:
- ДЕЛО — таблица пар «метка/значение»: <td><h2>метка</h2></td><td>значение</td>.
  Отсюда дата поступления, категория, дата и результат рассмотрения, дата вступления
  в силу и судья.
- ДВИЖЕНИЕ ДЕЛА — таблица событий из шести колонок: Наименование события / Результат
  [события] / Дата события / Время события / Судья / Дата размещения.
- СТОРОНЫ — таблица участников; колонки различаются по виду производства.
- ЛИЦА — только у уголовных дел; процессуального статуса там нет, поэтому роль
  проставляем сами («Лицо»).

Чего на страницах движка нет вовсе: истории местонахождения, таблицы судебных заседаний,
списка документов и метки «Текущее состояние». Первые три ключа отдаём пустыми списками,
а состояние (status) собираем из последней строки «Движения дела».

Номер дела и УИД парсер не отдаёт: их достаёт слой клиента (MsudrfCourtClient.
extract_case_code и CourtClient.extract_uid) ещё до разбора, потому что оба входят
в ключ карточки.

ВАЖНО (грабли разметки портала):
- Вкладки не имеют ни id, ни href, ни data-атрибутов. Единственная связь названия с
  телом — порядковый номер: i-й <li> в ul#tabs соответствует i-му div.tab-content
  внутри div#contentt. Искать тело вкладки по чему-то ещё нечем.
- <h2> — это и заголовок с номером дела, и КАЖДАЯ метка карточки, и КАЖДАЯ шапка
  таблицы. soup.find_all("h2") вернёт номер дела вперемешку с полутора десятками меток.
- Тело вкладки лежит либо в div.tab-content, либо в div#cont1…div#cont3 (Липецкая
  область). Оба варианта учитывает tab_bodies (app/parsers/msudrf_shared.py); пока искали
  только первый, разбор всего региона возвращал пустоту.
- Набор меток различается по виду производства, и одно и то же поле подписано
  по-разному: судья — «Председательствующий судья» / «Дело находится в производстве
  судьи» / «Передано в производство судье», результат — «Результат рассмотрения» /
  «Результат рассмотрения по делу» / «Результат рассмотрения (подготовки к
  рассмотрению) дела». Метки сверяем целиком и без учёта регистра: портал пишет
  «постановления» то со строчной, то с прописной.
- «Категория» есть только у гражданских дел, «Дата вступления в законную силу» —
  вообще у единиц. Отсутствие метки — норма, а не поломка разметки.
- Колонки «Движения дела» ПО ИНДЕКСУ брать нельзя, хотя раньше здесь так и было:
  порядок у движка непостоянен. У Московской области, Якутии, Кемеровской и Ивановской
  областей это «Наименование | Результат события | Дата события | Время», а у Орловской,
  Калининградской областей и Забайкальского края — «Наименование | Дата события | Время
  события | Результат события». Ищем по шапке, причём вторая колонка подписана «Результат
  события» у гражданских и уголовных, но просто «Результат» у КоАП.
- «Дата события» сплошь и рядом пустая: у только что заведённых дел — во ВСЕХ строках.
  Такие строки в события не превращаем (без даты не посчитать identity), но состояние
  дела из них берём — иначе у свежих дел не было бы ни одного признака жизни.
- В «Сторонах» у КоАП первые две колонки переставлены относительно гражданских
  («Сторона по делу» идёт ПЕРЕД процессуальным статусом). Колонки ищем по тексту
  шапки; по индексу роль и ФИО поменялись бы местами.
- Во вкладке «СУДЕБНЫЙ АКТ» лежит копия судебного акта в вордовской разметке, а в ней —
  второй УИД, разорванный на два тега: <b>УИД 50</b><b>MS0005-...</b>. Эту вкладку не
  трогаем совсем.
- Браузер иногда отдаёт документ до окончания рендера — тогда вместо страницы приходит
  пустой <html><head></head><body></body></html>. Разбор такого документа обязан вернуть
  пустой результат, а не упасть.
"""
from bs4 import BeautifulSoup, Tag

from app.parsers.base import CaseParser
from app.parsers.parsed_case import (
    ParsedCase,
    ParsedEvent,
    ParsedSide,
)
from app.parsers.text import clean, parse_date, parse_local_datetime
from app.parsers.msudrf_shared import (
    CARD_FIELDS,
    CARD_TABS,
    EVENT_DATE_HEADINGS,
    EVENT_DESCRIPTION_SEPARATOR,
    EVENT_NAME_HEADINGS,
    EVENT_PUBLISHED_HEADINGS,
    EVENT_RESULT_HEADINGS,
    EVENT_TIME_HEADINGS,
    EVENTS_TABS,
    JUDGE_LABELS,
    PERSONS_TABS,
    SIDES_TABS,
    column_index,
    find_tab,
    tab_bodies,
)

def _parse_card(tab: Tag) -> tuple[dict, list[str]]:
    """Разобрать вкладку «ДЕЛО» в (скалярные поля, список ФИО судей).

    Все ключи скалярных полей заведены заранее: страница — источник истины, поэтому
    пропавшую на ней метку отдаём как None (поле в БД обнулится), а не опускаем ключ.
    """
    card: dict = {field: None for field, _ in CARD_FIELDS.values()}
    judge_names: list[str] = []

    for row in tab.select("tr"):
        # Метка лежит в <h2> первой ячейки, значение — текст второй.
        label_el = row.find("h2")
        cells = row.find_all("td")
        if label_el is None or len(cells) < 2:
            continue

        label = clean(label_el.get_text()).casefold()
        value_el = cells[1]

        if label in JUDGE_LABELS:
            name = clean(value_el.get_text())
            if name:
                judge_names.append(name)
        elif label in CARD_FIELDS:
            field, convert = CARD_FIELDS[label]
            card[field] = convert(value_el.get_text())

    return card, judge_names


# Колонки «Движения дела» ищем ПО ШАПКЕ (списки названий — в msudrf_shared).
#
# По индексу их брать НЕЛЬЗЯ, хотя раньше здесь было именно так: порядок колонок у движка
# не постоянен. У Московской области, Якутии, Кемеровской и Ивановской областей идёт
# «Наименование | Результат события | Дата события | Время», а у Орловской, Калининградской
# областей и Забайкальского края — «Наименование | Дата события | Время события | Результат
# события». На вторых разбор по индексу читал дату из колонки со ВРЕМЕНЕМ («10:00» →
# None) и отбрасывал все события молча: карточка при этом оставалась непустой, так что
# ошибка ничем не проявлялась, кроме пустого «Движения дела».
#
# Ниже — те же индексы как ОТКАТ на случай, когда шапки в таблице нет вовсе: такой
# страницы мы не видели, но поведение на ней тогда останется прежним.
FALLBACK_EVENT_NAME_COL = 0
FALLBACK_EVENT_RESULT_COL = 1
FALLBACK_EVENT_DATE_COL = 2
FALLBACK_EVENT_TIME_COL = 3
FALLBACK_EVENT_PUBLISHED_COL = 5


def _cell(cells: list, index: int | None) -> str:
    """Текст колонки index или пустая строка, если такой колонки на странице нет."""
    if index is None or index >= len(cells):
        return ""
    return clean(cells[index].get_text())


def _event_columns(table: Tag) -> tuple[int, int | None, int, int | None, int | None]:
    """Номера колонок (наименование, результат, дата, время, дата размещения) по шапке.

    Шапка свёрстана через <td> внутри <thead> — <th> на этих страницах не бывает. Если
    шапки нет, возвращаем прежние фиксированные индексы.
    """
    headings = [clean(cell.get_text()).casefold() for cell in table.select("thead td")]
    if not headings:
        return (
            FALLBACK_EVENT_NAME_COL,
            FALLBACK_EVENT_RESULT_COL,
            FALLBACK_EVENT_DATE_COL,
            FALLBACK_EVENT_TIME_COL,
            FALLBACK_EVENT_PUBLISHED_COL,
        )

    name_col = column_index(headings, EVENT_NAME_HEADINGS)
    date_col = column_index(headings, EVENT_DATE_HEADINGS)
    return (
        FALLBACK_EVENT_NAME_COL if name_col is None else name_col,
        column_index(headings, EVENT_RESULT_HEADINGS),
        FALLBACK_EVENT_DATE_COL if date_col is None else date_col,
        # Время ищем ТОЛЬКО по шапке, без запасного номера: у страниц с шапкой порядок
        # колонок непостоянен, и угаданный индекс подставил бы в время чужую колонку.
        column_index(headings, EVENT_TIME_HEADINGS),
        column_index(headings, EVENT_PUBLISHED_HEADINGS),
    )


def _parse_events(tab: Tag) -> tuple[list[dict], str | None]:
    """Разобрать вкладку «ДВИЖЕНИЕ ДЕЛА» в (список событий, состояние дела).

    Событие — {"event_date": datetime, "state_description": str, "document_str": None,
    "published_at": date | None}. Строки без даты события пропускаем: дата входит в
    identity события, и без неё uid не посчитать. Состояние дела — наименование
    ПОСЛЕДНЕЙ строки таблицы, в том числе пропущенной: у только что заведённых дел даты
    нет ни в одной строке, и иначе о деле не осталось бы вообще ничего.
    """
    events: list[dict] = []
    status: str | None = None

    table = tab.find("table")
    if table is None:
        return events, status

    name_col, result_col, date_col, time_col, published_col = _event_columns(table)

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) <= max(name_col, date_col):
            continue

        name = clean(cells[name_col].get_text())
        if not name:
            continue
        if name.casefold() in EVENT_NAME_HEADINGS:
            # Это строка-ШАПКА, попавшая в тело таблицы (так свёрстан тип C, где <thead>
            # нет вовсе). Пропускаем: иначе её текст уехал бы в состояние дела, и карточка
            # с чужой разметкой выглядела бы разобранной — guard пустого разбора в
            # обход в старом core перестал бы её отсекать.
            continue

        # Состояние дела — наименование последней строки, поэтому перетираем на каждой.
        status = name

        # Время — местное для суда; колонка есть не всегда, тогда будет местная полночь.
        event_date = parse_local_datetime(
            cells[date_col].get_text(), _cell(cells, time_col)
        )
        if event_date is None:
            continue  # без даты событие не может участвовать в детекте изменений

        # Колонки может не быть на странице вовсе — тогда и поля у события нет.
        result = _cell(cells, result_col)
        events.append(
            ParsedEvent(
                event_date=event_date,
                state_description=(
                    name + EVENT_DESCRIPTION_SEPARATOR + result if result else name
                ),
                # Документов-оснований на страницах движка нет — колонки под них не бывает.
                document_str=None,
                published_at=parse_date(_cell(cells, published_col)),
            )
        )

    return events, status


# Шапки колонок вкладки «СТОРОНЫ». Набор колонок зависит от вида производства, а у КоАП
# ФИО ещё и стоит ПЕРЕД статусом — поэтому ищем обе колонки по тексту шапки.
SIDE_ROLE_HEADINGS = frozenset({"процессуальный статус лица, участвующего в деле"})
SIDE_NAME_HEADINGS = frozenset(
    {
        "лицо, участвующее в деле (фио, наименование)",
        "сторона по делу (фио, наименование)",
    }
)


def _parse_sides(tab: Tag) -> list[dict]:
    """Разобрать вкладку «СТОРОНЫ» в список ParsedSide.

    Роль отдаём сырой строкой ровно как на портале («Взыскатель», «Должник»,
    «Защитник», «Лицо, в отношении которого ведется производство по делу») —
    сопоставление роли с типом стороны делает SideRepository.
    """
    sides: list[dict] = []

    table = tab.find("table")
    if table is None:
        return sides

    # Шапка таблицы свёрстана через <td>, а не <th> — <th> на этих страницах не бывает.
    headings = [clean(cell.get_text()).casefold() for cell in table.select("thead td")]
    role_col = column_index(headings, SIDE_ROLE_HEADINGS)
    name_col = column_index(headings, SIDE_NAME_HEADINGS)
    if role_col is None or name_col is None:
        return sides

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) <= max(role_col, name_col):
            continue

        role = clean(cells[role_col].get_text())
        full_name = clean(cells[name_col].get_text())
        if role and full_name:
            sides.append(ParsedSide(role=role, full_name=full_name))

    return sides


# Шапка колонки с ФИО во вкладке «ЛИЦА».
PERSON_NAME_HEADINGS = frozenset({"фио"})
# Процессуального статуса во вкладке «ЛИЦА» нет — там колонки про приговор и статьи.
# Роль берём из названия самой вкладки, иначе лицо не сохранить: роль у стороны есть
# всегда.
PERSON_ROLE = "Лицо"


def _parse_persons(tab: Tag) -> list[dict]:
    """Разобрать вкладку «ЛИЦА» (уголовные дела) в список ParsedSide.

    Колонки про приговор и перечень статей не читаем: полей под них в модели нет.
    """
    persons: list[dict] = []

    table = tab.find("table")
    if table is None:
        return persons

    headings = [clean(cell.get_text()).casefold() for cell in table.select("thead td")]
    name_col = column_index(headings, PERSON_NAME_HEADINGS)
    if name_col is None:
        return persons

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) <= name_col:
            continue

        full_name = clean(cells[name_col].get_text())
        if full_name:
            persons.append(ParsedSide(role=PERSON_ROLE, full_name=full_name))

    return persons


# Названия вкладок и их синонимы — в app/parsers/msudrf_shared.py (CARD_TABS и далее).


def _parse_participants(tab: Tag) -> list[dict]:
    """Разобрать вкладку участников, выбрав разбор ПО ШАПКЕ таблицы, а не по названию.

    Название вкладки о форме таблицы не говорит: в Смоленской области вкладка называется
    «ЛИЦА», а внутри лежит обычная таблица сторон («Процессуальный статус лица…» +
    «Лицо, участвующее в деле…»). Пока разбор выбирался по названию, стороны таких дел
    терялись целиком — колонки «ФИО» в такой таблице нет.

    Поэтому сначала пробуем разобрать как стороны (там есть и роль, и ФИО), а если не
    вышло — как «ЛИЦА» уголовного дела, где процессуального статуса нет и роль мы
    проставляем сами.
    """
    return _parse_sides(tab) or _parse_persons(tab)


class MsudrfTypeBParser(CaseParser):
    """Парсер страниц типа B (мировые суды на движке msudrf.ru)."""

    page_type = "B"

    def parse(self, html: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        tabs = tab_bodies(soup)

        # === КАРТОЧКА: скалярные поля дела и судья ==========================
        # Вкладки может не быть совсем — например, если браузер отдал пустой документ.
        card: dict = {field: None for field, _ in CARD_FIELDS.values()}
        judge_names: list[str] = []
        # «ДЕЛО» или «МАТЕРИАЛ»; с «ДВИЖЕНИЕМ ДЕЛА» не спутать — сверка по началу названия.
        card_tab = find_tab(tabs, *CARD_TABS)
        if card_tab is not None:
            card, judge_names = _parse_card(card_tab)

        # === ДВИЖЕНИЕ ДЕЛА: события и состояние дела ========================
        events_tab = find_tab(tabs, *EVENTS_TABS)
        events, status = (
            _parse_events(events_tab) if events_tab is not None else ([], None)
        )

        # === СТОРОНЫ И ЛИЦА ================================================
        # Обе вкладки дают стороны по делу и складываются в один список.
        sides: list[dict] = []
        for tab in (find_tab(tabs, *SIDES_TABS), find_tab(tabs, *PERSONS_TABS)):
            if tab is not None:
                sides.extend(_parse_participants(tab))

        # card содержит РОВНО те поля, которые бывают на страницах типа B: они
        # засеяны из CARD_FIELDS в начале parse. Остальные скалярные поля ParsedCase
        # остаются UNSET, и колонки в БД по ним не трогаются — см. app/parsers/parsed_case.py.
        #
        # Историй местонахождения, судебных заседаний и документов на этих страницах нет
        # вовсе — соответствующих вкладок не существует, поэтому списки пусты.
        return ParsedCase(
            **card,
            status=status,
            judge_names=judge_names,
            sides=sides,
            events=events,
        )
