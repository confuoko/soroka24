"""Парсер карточки дела движка msudrf.ru — ВТОРАЯ вёрстка карточки, страница типа C.

Тот же движок, что и тип B (app/parsers/msudrf_type_b.py), и те же данные: набор меток
карточки, метки судьи и правило состояния дела общие — они лежат в
app/parsers/msudrf_shared.py и переиспользуются целиком. Различается только вёрстка, и
различается сильно:

* метка карточки свёрстана `<b>`, а не `<h2>`. Над таблицей стоит секционный заголовок
  `<td colspan=N><h2>ОСНОВНЫЕ СВЕДЕНИЯ</h2></td>` (у КоАП — «СВЕДЕНИЯ О ПРИВЛЕКАЕМОМ
  ЛИЦЕ») — это НЕ метка, строку с colspan пропускаем;
* `<thead>` нет вовсе. Шапка «Движения дела» — первая строка тела, ячейки которой свёрстаны
  теми же `<b>`; данные идут после неё;
* число колонок «Движения дела» зависит от региона: 5 в Пермском крае (Наименование, Дата,
  Время, Результат, Судья) и 4 в Адыгее (без «Времени события»). Колонки поэтому ищем по
  тексту шапки, а не по номеру. Колонки «Дата размещения» здесь нет ни в одном регионе,
  так что published_at у событий всегда None;
* таблица сторон ТРАНСПОНИРОВАНА: строки — это поля, колонки — сами стороны. То есть роли
  всех сторон стоят в одной строке, а их ФИО — в следующей:

      | Вид лица, участвующего в деле            | Взыскатель      | Должник           |
      | Лицо, участвующее в деле (ФИО, наимен.)  | ООО ПКО «…»     | Иванова И. И.     |

  У КоАП та же форма с одной колонкой-стороной и другими подписями строк («Вид участника
  производства», «Сторона по делу (ФИО, наименование)»). У уголовных дел вместо сторон
  вкладка «ЛИЦА», и она тоже транспонирована: ФИО подсудимого стоит в строке «ФИО
  подсудимого», а рядом — статья обвинения и приговор, которых мы не храним.

Где встречается: Пермский край (*.perm.msudrf.ru, 146 судов, код 59MS) и Республика Адыгея
(*.adg.msudrf.ru, 24 суда, код 01MS) — примеры страниц в html_examples/case_96_* и
html_examples/case_adg1_*, case_maikop1_*. Ожидается ещё в Брянской и Магаданской областях
(они пока не подключены — карточек для проверки не было).

Тип страницы у карточки НЕ выводится из домена: его определяет detect_page_type
(app/parsers/msudrf_shared.py) по самой разметке, а домен задаёт лишь ожидание — см.
MsudrfCourtClient.parse.

Номер дела и УИД парсер не отдаёт: их достаёт слой клиента ещё до разбора, потому что оба
входят в ключ карточки. УИД у обоих регионов на карточках отсутствует вовсе — карточка
получает самодельный ключ от ссылки (synthetic_uid в app/validators.py).
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

# Названия вкладок и их синонимы — в app/parsers/msudrf_shared.py (CARD_TABS и далее).

# Метка карточки и шапка таблиц свёрстаны этим тегом.
LABEL_TAG = "b"


def _label(row: Tag) -> str | None:
    """Метка строки: текст <b> в первой ячейке, приведённый к нижнему регистру.

    None — если строки-метки тут нет: это либо секционный заголовок (ячейка с colspan и
    <h2> внутри), либо строка данных.
    """
    cells = row.find_all("td")
    if not cells or cells[0].has_attr("colspan"):
        return None
    label_el = cells[0].find(LABEL_TAG)
    return clean(label_el.get_text()).casefold() if label_el is not None else None


def _parse_card(tab: Tag) -> tuple[dict, list[str]]:
    """Разобрать вкладку «ДЕЛО» в (скалярные поля, список ФИО судей).

    Все ключи скалярных полей заведены заранее: страница — источник истины, поэтому
    пропавшую на ней метку отдаём как None (поле в БД обнулится), а не опускаем ключ.
    """
    card: dict = {field: None for field, _ in CARD_FIELDS.values()}
    judge_names: list[str] = []

    for row in tab.select("tr"):
        label = _label(row)
        cells = row.find_all("td")
        if label is None or len(cells) < 2:
            continue

        value_el = cells[1]
        if label in JUDGE_LABELS:
            name = clean(value_el.get_text())
            if name:
                judge_names.append(name)
        elif label in CARD_FIELDS:
            field, convert = CARD_FIELDS[label]
            card[field] = convert(value_el.get_text())

    return card, judge_names


def _event_columns(rows: list[Tag]) -> tuple[dict[str, int], int] | None:
    """Найти строку-шапку «Движения дела» и вернуть (колонки по смыслу, её номер).

    Шапки в <thead> здесь нет: ею служит первая строка, у которой ячейки свёрстаны <b>.
    Ищем именно её, а не берём строку по номеру: сверху таблицы стоит ещё и секционный
    заголовок, а у части дел — пустая строка.

    None — шапки в таблице нет вовсе, разбирать нечего: без неё непонятно даже, в какой
    колонке дата, а колонок бывает и 4, и 5.
    """
    for index, row in enumerate(rows):
        cells = row.find_all("td")
        headings = [
            clean(cell.get_text()).casefold() if cell.find(LABEL_TAG) is not None else ""
            for cell in cells
        ]
        name_col = column_index(headings, EVENT_NAME_HEADINGS)
        if name_col is None:
            continue
        return (
            {
                "name": name_col,
                "date": column_index(headings, EVENT_DATE_HEADINGS),
                # Колонка времени у этой вёрстки непостоянна: Пермь и Тыва (17MS0019) её
                # отдают, Адыгея нет, а в Рязанской области она есть у одного дела и нет
                # у другого. Поэтому None здесь — норма, а не признак поломки.
                "time": column_index(headings, EVENT_TIME_HEADINGS),
                "result": column_index(headings, EVENT_RESULT_HEADINGS),
            },
            index,
        )
    return None


def _cell(cells: list, index: int | None) -> str:
    """Текст колонки index или пустая строка, если такой колонки на странице нет."""
    if index is None or index >= len(cells):
        return ""
    return clean(cells[index].get_text())


def _parse_events(tab: Tag) -> tuple[list[dict], str | None]:
    """Разобрать вкладку «ДВИЖЕНИЕ ДЕЛА» в (список событий, состояние дела).

    Событие — {"event_date": date, "state_description": str, "document_str": None,
    "published_at": None}. Строки без даты события пропускаем: дата входит в identity
    события, и без неё uid не посчитать. Состояние дела — наименование ПОСЛЕДНЕЙ строки
    таблицы, в том числе пропущенной: у дел, где ни одна строка ещё не получила даты, иначе
    не осталось бы вообще ничего. Правило то же, что в типе B.
    """
    events: list[dict] = []
    status: str | None = None

    table = tab.find("table")
    if table is None:
        return events, status

    rows = table.select("tbody tr") or table.select("tr")
    found = _event_columns(rows)
    if found is None:
        return events, status
    columns, header_index = found

    for row in rows[header_index + 1 :]:
        cells = row.find_all("td")
        name = _cell(cells, columns["name"])
        if not name:
            continue

        # Состояние дела — наименование последней строки, поэтому перетираем на каждой.
        status = name

        # Время — местное для суда; колонки может не быть, тогда местная полночь.
        event_date = parse_local_datetime(
            _cell(cells, columns["date"]), _cell(cells, columns["time"])
        )
        if event_date is None:
            continue  # без даты событие не может участвовать в детекте изменений

        result = _cell(cells, columns["result"])
        events.append(
            ParsedEvent(
                event_date=event_date,
                state_description=(
                    name + EVENT_DESCRIPTION_SEPARATOR + result if result else name
                ),
                # Документов-оснований на страницах движка нет — колонки под них не бывает.
                document_str=None,
                # Колонки «Дата размещения» у этой вёрстки нет ни в одном регионе.
                published_at=None,
            )
        )

    return events, status


# Подписи СТРОК транспонированной таблицы сторон: роль и ФИО. У гражданских дел одни, у
# КоАП другие — сверяем целиком, набор открытый.
SIDE_ROLE_LABELS = frozenset(
    {"вид лица, участвующего в деле", "вид участника производства"}
)
SIDE_NAME_LABELS = frozenset(
    {
        "лицо, участвующее в деле (фио, наименование)",
        "сторона по делу (фио, наименование)",
    }
)


def _parse_sides(tab: Tag) -> list[dict]:
    """Разобрать транспонированную вкладку сторон в список ParsedSide.

    Строки здесь — поля, колонки — стороны, поэтому роль и ФИО каждой стороны лежат в
    РАЗНЫХ строках, в одной и той же по счёту ячейке. Собираем две нужные строки и
    склеиваем их поэлементно.

    Роль отдаём сырой строкой ровно как на портале («Взыскатель», «Должник», «Лицо, в
    отношении которого ведется производство по делу») — сопоставление роли с типом стороны
    делает SideRepository. Остальные строки таблицы («Главная статья (КоАП, ТК ...)»,
    «Наименование вида правонарушения») не читаем: полей под них в модели дела нет.
    """
    table = tab.find("table")
    if table is None:
        return []

    roles: list[str] = []
    names: list[str] = []
    for row in table.select("tr"):
        label = _label(row)
        if label is None:
            continue
        values = [clean(cell.get_text()) for cell in row.find_all("td")[1:]]
        if label in SIDE_ROLE_LABELS:
            roles = values
        elif label in SIDE_NAME_LABELS:
            names = values

    return [
        ParsedSide(role=role, full_name=full_name)
        for role, full_name in zip(roles, names)
        if role and full_name
    ]


# Подписи строк вкладки «ЛИЦА» (уголовные дела), где лежит ФИО. Таблица там тоже
# транспонирована, но сторона одна, а строк много — кроме ФИО там статья обвинения и
# приговор, полей под которые в модели дела нет.
PERSON_NAME_LABELS = frozenset({"фио подсудимого", "фио", "фио лица"})
# Процессуального статуса во вкладке «ЛИЦА» нет — роль проставляем сами, иначе лицо не
# сохранить: роль у стороны есть всегда. Так же поступает парсер типа B.
PERSON_ROLE = "Лицо"


def _parse_persons(tab: Tag) -> list[dict]:
    """Разобрать вкладку «ЛИЦА» (уголовные дела) в список ParsedSide.

    Строки-секции («СВЕДЕНИЯ О ЛИЦЕ», «СВЕДЕНИЯ О ПРЕСТУПЛЕНИИ», «РЕШЕНИЕ») свёрстаны
    ячейкой с colspan и отбрасываются вместе с прочими метками — берём только ФИО.
    """
    table = tab.find("table")
    if table is None:
        return []

    persons: list[dict] = []
    for row in table.select("tr"):
        if _label(row) not in PERSON_NAME_LABELS:
            continue
        for cell in row.find_all("td")[1:]:
            full_name = clean(cell.get_text())
            if full_name:
                persons.append(ParsedSide(role=PERSON_ROLE, full_name=full_name))
    return persons


class MsudrfTypeCParser(CaseParser):
    """Парсер страниц типа C (движок msudrf.ru, вторая вёрстка карточки)."""

    page_type = "C"

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
        # Разбор выбираем по ПОДПИСЯМ СТРОК, а не по названию вкладки: название о форме
        # таблицы не говорит (см. _parse_participants в app/parsers/msudrf_type_b.py).
        sides: list[dict] = []
        for tab in (find_tab(tabs, *SIDES_TABS), find_tab(tabs, *PERSONS_TABS)):
            if tab is not None:
                sides.extend(_parse_sides(tab) or _parse_persons(tab))

        # card содержит РОВНО те поля, которые бывают на страницах движка: они засеяны
        # из CARD_FIELDS в начале parse. Остальные скалярные поля ParsedCase остаются
        # UNSET, и колонки в БД по ним не трогаются — см. app/parsers/parsed_case.py.
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
