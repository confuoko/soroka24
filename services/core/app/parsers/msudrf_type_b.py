"""Парсер карточки дела мировых судов на движке msudrf.ru — страница типа B.

Разметка у движка общая на все регионы; проверена на Московской области
(*.mo.msudrf.ru) и Алтайском крае (*.alt.msudrf.ru), примеры — в html_examples/mo_case_*.

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
- Набор меток различается по виду производства, и одно и то же поле подписано
  по-разному: судья — «Председательствующий судья» / «Дело находится в производстве
  судьи» / «Передано в производство судье», результат — «Результат рассмотрения» /
  «Результат рассмотрения по делу» / «Результат рассмотрения (подготовки к
  рассмотрению) дела». Метки сверяем целиком и без учёта регистра: портал пишет
  «постановления» то со строчной, то с прописной.
- «Категория» есть только у гражданских дел, «Дата вступления в законную силу» —
  вообще у единиц. Отсутствие метки — норма, а не поломка разметки.
- Шапка второй колонки «Движения дела» — «Результат события» у гражданских и
  уголовных, но просто «Результат» у КоАП. Колонок всегда шесть и порядок фиксирован,
  поэтому берём их по индексу, а не по шапке.
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
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from app.parsers.base import CaseParser


def _clean(text: str) -> str:
    """Схлопнуть любые пробелы/переводы строк в один пробел и обрезать края."""
    return " ".join(text.split())


def _clean_or_none(text: str) -> str | None:
    """Как _clean, но пустое значение → None (в БД такому полю место NULL, а не '')."""
    return _clean(text) or None


# Формат дат на портале — везде один, и в карточке, и в таблице событий.
DATE_FORMAT = "%d.%m.%Y"


def _parse_date(text: str) -> date | None:
    """Разобрать дату формата ДД.ММ.ГГГГ; пустое/некорректное значение → None."""
    text = _clean(text)
    if not text:
        return None
    try:
        return datetime.strptime(text, DATE_FORMAT).date()
    except ValueError:
        return None


# Вкладки: названия в ul#tabs, тела — в div#contentt. Сопоставляются только по порядку.
TABS_SELECTOR = "ul#tabs li"
TAB_BODIES_SELECTOR = "div#contentt > div.tab-content"


def _tab_bodies(soup: BeautifulSoup) -> dict[str, Tag]:
    """Сопоставить название вкладки с её содержимым по порядковому номеру.

    Ни id, ни href, ни data-атрибутов у вкладок нет — привязать тело к названию можно
    только позицией. Лишние названия без тела (и наоборот) молча отбрасываем: zip
    обрезается по короткому, и это ровно то поведение, которое нужно.
    """
    names = [_clean(tab.get_text()) for tab in soup.select(TABS_SELECTOR)]
    bodies = soup.select(TAB_BODIES_SELECTOR)
    return {name: body for name, body in zip(names, bodies) if name}


# Скалярные поля дела из вкладки «ДЕЛО»: метка -> (поле Case, преобразование значения).
# Ключи в нижнем регистре, метку сверяем целиком: «Результат рассмотрения» — это ДРУГОЕ
# поле, чем «Результат рассмотрения по делу», и сравнение по префиксу их бы склеило.
# Один и тот же смысл подписан по-разному в зависимости от вида производства, поэтому
# несколько меток ведут в одно поле.
CARD_FIELDS = {
    "дата поступления": ("receipt_date", _parse_date),
    "категория": ("category", _clean_or_none),
    # Дата рассмотрения: приказное / уголовное / КоАП.
    "дело рассмотрено (выдан приказ)": ("first_instance_date", _parse_date),
    "дата рассмотрения дела": ("first_instance_date", _parse_date),
    "дата вынесения постановления (определения) по делу": (
        "first_instance_date",
        _parse_date,
    ),
    # Результат рассмотрения: приказное / уголовное / КоАП.
    "результат рассмотрения": ("first_instance_decision", _clean_or_none),
    "результат рассмотрения по делу": ("first_instance_decision", _clean_or_none),
    "результат рассмотрения (подготовки к рассмотрению) дела": (
        "first_instance_decision",
        _clean_or_none,
    ),
    "дата вступления в законную силу": ("decision_effective_date", _parse_date),
}

# Метки судьи — по одной на вид производства, значение у всех одинаковое: ФИО.
JUDGE_LABELS = frozenset(
    {
        "председательствующий судья",
        "дело находится в производстве судьи",
        "передано в производство судье",
    }
)

# «Уникальный идентификатор дела» и «Номер протокола об АП» намеренно не разбираем:
# первый достаёт клиент до парсинга, под второй в модели дела поля нет.


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

        label = _clean(label_el.get_text()).casefold()
        value_el = cells[1]

        if label in JUDGE_LABELS:
            name = _clean(value_el.get_text())
            if name:
                judge_names.append(name)
        elif label in CARD_FIELDS:
            field, convert = CARD_FIELDS[label]
            card[field] = convert(value_el.get_text())

    return card, judge_names


# Колонки «Движения дела»: их всегда шесть и порядок фиксирован. Берём по индексу —
# по шапке нельзя: у КоАП вторая колонка подписана «Результат», а не «Результат события».
EVENT_NAME_COL = 0
EVENT_RESULT_COL = 1
EVENT_DATE_COL = 2
EVENT_PUBLISHED_COL = 5
# Наименование и результат события портал отдаёт разными колонками, а нам нужно одно
# описание состояния: склеиваем через дефис. Приставки («Принято решение: »,
# «Перенесено по причинам: ») оставляем как есть — толковать портал не наше дело.
EVENT_DESCRIPTION_SEPARATOR = " - "


def _parse_events(tab: Tag) -> tuple[list[dict], str | None]:
    """Разобрать вкладку «ДВИЖЕНИЕ ДЕЛА» в (список событий, состояние дела).

    Событие — {"event_date": date, "state_description": str, "document_str": None,
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

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) <= EVENT_PUBLISHED_COL:
            continue

        name = _clean(cells[EVENT_NAME_COL].get_text())
        if not name:
            continue

        # Состояние дела — наименование последней строки, поэтому перетираем на каждой.
        status = name

        event_date = _parse_date(cells[EVENT_DATE_COL].get_text())
        if event_date is None:
            continue  # без даты событие не может участвовать в детекте изменений

        result = _clean(cells[EVENT_RESULT_COL].get_text())
        events.append(
            {
                "event_date": event_date,
                "state_description": (
                    name + EVENT_DESCRIPTION_SEPARATOR + result if result else name
                ),
                # Документов-оснований на страницах движка нет — колонки под них не бывает.
                "document_str": None,
                "published_at": _parse_date(cells[EVENT_PUBLISHED_COL].get_text()),
            }
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


def _column_index(headings: list[str], wanted: frozenset) -> int | None:
    """Номер колонки, чья шапка совпала с одной из искомых (или None, если такой нет)."""
    for index, heading in enumerate(headings):
        if heading in wanted:
            return index
    return None


def _parse_sides(tab: Tag) -> list[dict]:
    """Разобрать вкладку «СТОРОНЫ» в список {"role", "full_name"}.

    Роль отдаём сырой строкой ровно как на портале («Взыскатель», «Должник»,
    «Защитник», «Лицо, в отношении которого ведется производство по делу») —
    сопоставление роли с типом стороны делает SideRepository.
    """
    sides: list[dict] = []

    table = tab.find("table")
    if table is None:
        return sides

    # Шапка таблицы свёрстана через <td>, а не <th> — <th> на этих страницах не бывает.
    headings = [_clean(cell.get_text()).casefold() for cell in table.select("thead td")]
    role_col = _column_index(headings, SIDE_ROLE_HEADINGS)
    name_col = _column_index(headings, SIDE_NAME_HEADINGS)
    if role_col is None or name_col is None:
        return sides

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) <= max(role_col, name_col):
            continue

        role = _clean(cells[role_col].get_text())
        full_name = _clean(cells[name_col].get_text())
        if role and full_name:
            sides.append({"role": role, "full_name": full_name})

    return sides


# Шапка колонки с ФИО во вкладке «ЛИЦА».
PERSON_NAME_HEADINGS = frozenset({"фио"})
# Процессуального статуса во вкладке «ЛИЦА» нет — там колонки про приговор и статьи.
# Роль берём из названия самой вкладки, иначе лицо не сохранить: роль у стороны есть
# всегда.
PERSON_ROLE = "Лицо"


def _parse_persons(tab: Tag) -> list[dict]:
    """Разобрать вкладку «ЛИЦА» (уголовные дела) в список {"role", "full_name"}.

    Колонки про приговор и перечень статей не читаем: полей под них в модели нет.
    """
    persons: list[dict] = []

    table = tab.find("table")
    if table is None:
        return persons

    headings = [_clean(cell.get_text()).casefold() for cell in table.select("thead td")]
    name_col = _column_index(headings, PERSON_NAME_HEADINGS)
    if name_col is None:
        return persons

    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) <= name_col:
            continue

        full_name = _clean(cells[name_col].get_text())
        if full_name:
            persons.append({"role": PERSON_ROLE, "full_name": full_name})

    return persons


# Названия вкладок — всегда капсом.
CARD_TAB = "ДЕЛО"
EVENTS_TAB = "ДВИЖЕНИЕ ДЕЛА"
SIDES_TAB = "СТОРОНЫ"
PERSONS_TAB = "ЛИЦА"


class MsudrfTypeBParser(CaseParser):
    """Парсер страниц типа B (мировые суды на движке msudrf.ru)."""

    page_type = "B"

    def parse(self, html: str) -> dict:
        soup = BeautifulSoup(html, "lxml")
        tabs = _tab_bodies(soup)

        # === КАРТОЧКА: скалярные поля дела и судья ==========================
        # Вкладки может не быть совсем — например, если браузер отдал пустой документ.
        card: dict = {field: None for field, _ in CARD_FIELDS.values()}
        judge_names: list[str] = []
        card_tab = tabs.get(CARD_TAB)
        if card_tab is not None:
            card, judge_names = _parse_card(card_tab)

        # === ДВИЖЕНИЕ ДЕЛА: события и состояние дела ========================
        events_tab = tabs.get(EVENTS_TAB)
        events, status = (
            _parse_events(events_tab) if events_tab is not None else ([], None)
        )

        # === СТОРОНЫ И ЛИЦА ================================================
        # Обе вкладки дают стороны по делу и складываются в один список.
        sides: list[dict] = []
        sides_tab = tabs.get(SIDES_TAB)
        if sides_tab is not None:
            sides.extend(_parse_sides(sides_tab))
        persons_tab = tabs.get(PERSONS_TAB)
        if persons_tab is not None:
            sides.extend(_parse_persons(persons_tab))

        return {
            **card,
            "status": status,
            "judge_names": judge_names,
            "sides": sides,
            "events": events,
            # Истории местонахождения, судебных заседаний и документов на страницах
            # типа B нет — соответствующих вкладок не существует.
            "place_history": [],
            "court_sessions": [],
            "documents": [],
        }
