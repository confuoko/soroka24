"""Тесты парсера карточки мировых судов Московской области на сохранённых html_examples.

Парсер — чистая функция HTML -> данные, поэтому тестам не нужны ни БД, ни Chromium.

Фикстуры подобраны по одной на каждый вид производства: у них различаются и набор
вкладок, и подписи меток, и колонки таблиц, поэтому «работает на одной» тут ничего
не доказывает.
  * mo_case_5_415323702.html   — гражданское приказное (есть «Категория»)
  * mo_case_95_429386415.html  — гражданское исковое, с вкладкой «СУДЕБНЫЙ АКТ»
  * mo_case_215_436540882.html — материал: в карточке три строки, дат в движении нет
  * mo_case_5_424820424.html   — уголовное, с вкладкой «ЛИЦА»
  * mo_case_5_422334200.html   — уголовное со вкладкой «СУДЕБНЫЙ АКТ» и вступлением в силу
  * mo_case_148_434639708.html — КоАП: шапка «Результат», переставленные колонки сторон
  * mo_case_5_436673426.html   — КоАП нерассмотренный: ни результата, ни дат в движении

Одна фикстура — не из Московской области: разметка у движка общая, а вот УИД на
карточке есть не всегда.
  * case_sakha45_nouid-14MS0054-972273874cab.html — Якутия, архивное дело 2011 года:
    УИД на карточке нет вовсе, метки те же (тип B)
  * case_3sev_nouid-57MS0035-faca1208385d.html — Орловская область: ДРУГОЙ ПОРЯДОК колонок
    в «Движении дела» («Дата события» вторая, «Результат события» четвёртый)
  * case_elec-r1_48MS0012-01-2026-001030-63.html — Липецкая область: тела вкладок лежат в
    div#cont1…div#cont3, а не в div.tab-content
  * case_bond_nouid-68MS0001-909bec32420a.html — Тамбовская область, ПРОИЗВОДСТВО ПО
    МАТЕРИАЛУ: вкладки «МАТЕРИАЛ» и «ДВИЖЕНИЕ МАТЕРИАЛА», метка судьи просто «Судья»
  * case_1_nouid-67MS0001-418a046b6977.html — Смоленская область: вкладка называется
    «ЛИЦА», а внутри обычная таблица СТОРОН
"""
from datetime import date
from pathlib import Path

import pytest

from app.parsers.msudrf_type_b import MsudrfTypeBParser

HTML_DIR = Path(__file__).resolve().parents[1] / "html_examples"


def _parse(filename: str) -> dict:
    html = (HTML_DIR / filename).read_text(encoding="utf-8")
    return MsudrfTypeBParser().parse(html)


# ------------------------------------------------------------------ скалярные поля
def test_civil_card_fields_parsed() -> None:
    """Гражданское приказное: разбираем все метки карточки, каких нет — None."""
    card = _parse("mo_case_5_415323702.html")

    assert card["receipt_date"] == date(2026, 2, 11)
    assert card["category"].startswith("Споры, связанные с жилищными отношениями")
    assert card["first_instance_date"] == date(2026, 2, 13)
    assert card["first_instance_decision"] == "Судебный приказ"
    assert card["decision_effective_date"] is None


def test_material_card_has_only_three_labels() -> None:
    """У материала на карточке три строки — отсутствующие метки дают None, а не падение."""
    card = _parse("mo_case_215_436540882.html")

    assert card["receipt_date"] == date(2026, 8, 5)
    assert card["category"] is None
    assert card["first_instance_date"] is None
    assert card["first_instance_decision"] is None


def test_decision_effective_date_parsed() -> None:
    """«Дата вступления в законную силу» есть у единиц дел — но разбираться должна."""
    assert _parse("mo_case_5_422334200.html")["decision_effective_date"] == date(
        2026, 5, 6
    )


@pytest.mark.parametrize(
    ("filename", "expected_judge"),
    [
        # «Председательствующий судья» / «Дело находится в производстве судьи» /
        # «Передано в производство судье» — три метки одного и того же поля.
        ("mo_case_5_415323702.html", "Парунова Надежда Викторовна"),
        ("mo_case_5_424820424.html", "Кравцова Ирина Викторовна"),
        ("mo_case_148_434639708.html", "Святкина Ольга Александровна"),
    ],
)
def test_judge_parsed_from_every_label(filename: str, expected_judge: str) -> None:
    """Судья подписан по-своему в каждом виде производства — иначе судьи бы не было."""
    assert _parse(filename)["judge_names"] == [expected_judge]


@pytest.mark.parametrize(
    ("filename", "expected_decision"),
    [
        ("mo_case_5_415323702.html", "Судебный приказ"),
        ("mo_case_5_422334200.html", "Постановление о прекращении уголовного дела"),
        ("mo_case_95_429386415.html", "Иск (заявление) удовлетворен"),
    ],
)
def test_decision_parsed_from_every_label(filename: str, expected_decision: str) -> None:
    """«Результат рассмотрения», «…по делу», «…(подготовки к рассмотрению) дела» — одно поле.

    Метки сверяются целиком: по префиксу «Результат рассмотрения» перехватил бы обе
    длинные метки, и в поле уехало бы значение не того поля.
    """
    assert _parse(filename)["first_instance_decision"] == expected_decision


# ------------------------------------------------------------------------ события
def test_event_joins_name_and_result() -> None:
    """Наименование и результат склеиваются через дефис, приставка портала сохраняется."""
    events = _parse("mo_case_5_415323702.html")["events"]

    assert events[0] == {
        "event_date": date(2026, 2, 13),
        "state_description": (
            "Регистрация судебного приказа"
            " - Принято решение: Определение об отмене судебного приказа"
        ),
        "document_str": None,
        "published_at": date(2026, 2, 13),
    }


def test_event_without_result_keeps_only_name() -> None:
    """Пустой результат не должен оставлять висящий дефис в описании состояния."""
    events = _parse("mo_case_5_415323702.html")["events"]

    assert events[-1]["state_description"] == "Окончание производства"


def test_row_without_event_date_is_skipped_but_gives_status() -> None:
    """Строка без «Даты события» не событие (нет identity), но состояние из неё берём.

    Первая строка этого дела — «Ознакомление с материалами» с пустой датой; она не
    должна попасть в события, а последняя строка задаёт состояние дела.
    """
    result = _parse("mo_case_5_415323702.html")

    assert "Ознакомление с материалами" not in [
        event["state_description"] for event in result["events"]
    ]
    assert result["status"] == "Сдача в архив"


def test_fresh_case_has_no_events_but_has_status() -> None:
    """У только что заведённого дела дат нет ни в одной строке — остаётся состояние.

    Ради этого случая состояние и берётся из строки без даты: иначе о свежем деле в БД
    не осталось бы ни одного признака жизни.
    """
    result = _parse("mo_case_5_436673426.html")

    assert result["events"] == []
    assert result["status"] == "Подготовка к рассмотрению"


def test_koap_result_column_heading_differs() -> None:
    """У КоАП вторая колонка подписана «Результат», а не «Результат события».

    Колонки берутся по индексу — если начать искать их по шапке, у КоАП результат
    события потеряется.
    """
    events = _parse("mo_case_148_434639708.html")["events"]

    assert events[0]["state_description"] == (
        "Подготовка к рассмотрению"
        " - Принято решение: Определение о назначении времени и места рассмотрения дела"
    )


def test_published_at_parsed() -> None:
    """«Дата размещения» — отдельное поле: портал публикует событие позже, чем оно было."""
    events = _parse("mo_case_5_415323702.html")["events"]

    assert events[1]["event_date"] == date(2026, 3, 5)
    assert events[1]["published_at"] == date(2026, 3, 17)


# ------------------------------------------------------------------------- стороны
def test_civil_sides_parsed() -> None:
    """Роль отдаём сырой строкой как на портале — классификация не дело парсера."""
    assert _parse("mo_case_5_415323702.html")["sides"] == [
        {"role": "Взыскатель", "full_name": 'МУП "Балашихинские коммунальные системы"'},
        {"role": "Должник", "full_name": "Улогов Юрий Григорьевич"},
    ]


def test_koap_swapped_columns_do_not_swap_role_and_name() -> None:
    """У КоАП ФИО стоит ПЕРЕД статусом — по индексу роль и ФИО поменялись бы местами."""
    assert _parse("mo_case_148_434639708.html")["sides"] == [
        {
            "role": "Лицо, в отношении которого ведется производство по делу",
            "full_name": "Земенков Владимир Геннадьевич",
        }
    ]


def test_persons_tab_adds_sides_with_fixed_role() -> None:
    """Вкладка «ЛИЦА» есть только у уголовных, статуса там нет — роль проставляем сами."""
    sides = _parse("mo_case_5_424820424.html")["sides"]

    assert {"role": "Лицо", "full_name": "Писарев Илья Вячеславович"} in sides
    # Стороны из вкладки «СТОРОНЫ» при этом никуда не делись.
    assert {"role": "Защитник", "full_name": "Гербст Иван Иванович"} in sides


# ----------------------------------------------------------------------- живучесть
def test_judicial_act_tab_does_not_leak_into_sides() -> None:
    """Во вкладке «СУДЕБНЫЙ АКТ» лежит копия акта с ФИО и вторым УИД — берём её за ноль.

    Вкладка свёрстана вордом и таблицы в ней встречаются: если разбирать все таблицы
    страницы подряд, в стороны приедет мусор из текста постановления.
    """
    sides = _parse("mo_case_5_422334200.html")["sides"]

    assert sides == [
        {"role": "Защитник", "full_name": "Гордымов Роман Анатольевич"},
        {"role": "Лицо", "full_name": "Розанова Анжелика Ивановна"},
    ]


def test_empty_document_parses_to_empty_result() -> None:
    """Браузер иногда отдаёт документ до рендера — это пустой разбор, а не исключение."""
    result = MsudrfTypeBParser().parse("<html><head></head><body></body></html>")

    assert result["receipt_date"] is None
    assert result["status"] is None
    assert result["judge_names"] == []
    assert result["sides"] == []
    assert result["events"] == []


def test_absent_sections_are_always_empty_lists() -> None:
    """Местонахождений, заседаний и документов на страницах МО нет — ключи всё равно есть.

    update_case читает их через data.get(..., []), но пустой список честнее отсутствия
    ключа: он означает «на странице пусто», и лишние строки в БД будут удалены.
    """
    result = _parse("mo_case_5_415323702.html")

    assert result["place_history"] == []
    assert result["court_sessions"] == []
    assert result["documents"] == []


def test_archive_card_without_uid_parses_fully() -> None:
    """Карточка без УИД разбирается штатным парсером — не хватает только УИД.

    Дело 2011 года на sakha45.yak.msudrf.ru: УИД начали присваивать позже, и на странице
    его нет ни строкой, ни меткой. Разметка при этом обычная, типа B, поэтому парсер
    трогать не пришлось — идентификатор такой карточке считает сервис
    (synthetic_uid в app/validators.py), а не парсер.
    """
    card = _parse("case_sakha45_nouid-14MS0054-972273874cab.html")

    assert card["category"].startswith("О защите прав потребителей")
    assert card["judge_names"] == ["Ступина Надежда Викторовна"]
    assert card["first_instance_date"] == date(2011, 5, 13)
    assert card["first_instance_decision"] == "Иск (заявление) удовлетворен частично"
    assert card["status"] == "Решение по существу"
    assert card["sides"] == [
        {"role": "Истец", "full_name": "Егоров Александр Николаевич"},
        {"role": "Ответчик", "full_name": 'ОАО АК "Якутия"'},
    ]
    assert [event["state_description"] for event in card["events"]] == [
        "Ознакомление с материалами",
        "Подготовка к судебному разбирательству",
        "Судебное заседание",
        "Судебное заседание",
        "Решение по существу",
    ]
    assert card["events"][0]["event_date"] == date(2011, 4, 29)


# ------------------------------------------------- вёрстки одного и того же типа B
def test_swapped_movement_columns_are_read_by_heading() -> None:
    """Орловская область: колонки «Движения дела» переставлены — события всё равно наши.

    Регресс. Пока колонки брались по номеру, дата события читалась из колонки со ВРЕМЕНЕМ
    («10:00» → None), и все события такой страницы отбрасывались молча: карточка при этом
    оставалась непустой, так что ошибка ничем не проявлялась. Так свёрстаны Орловская и
    Калининградская области и Забайкальский край — то есть и уже подключённые регионы.
    """
    card = _parse("case_3sev_nouid-57MS0035-faca1208385d.html")

    assert card["receipt_date"] == date(2026, 7, 16)
    assert card["judge_names"] == ["Хворостянова Кристина Зауриевна"]
    # Три строки движения, но у первой нет даты события — в события она не попадает.
    assert [(e["event_date"], e["state_description"]) for e in card["events"]] == [
        (
            date(2026, 7, 16),
            "Подготовка к судебному разбирательству - Принято решение: Определение о "
            "назначении дела к разбирательству в судебном заседании",
        ),
        (date(2026, 8, 18), "Судебное заседание"),
    ]
    # «Дата размещения» — последняя колонка, и она тоже найдена по шапке, а не по номеру.
    assert card["events"][0]["published_at"] == date(2026, 7, 20)
    assert card["status"] == "Судебное заседание"


def test_tab_bodies_may_live_in_numbered_divs() -> None:
    """Липецкая область: тела вкладок в div#cont1…3, а не в div.tab-content.

    Регресс. Регион подключён, но разбор возвращал пустоту целиком: тел вкладок находилось
    ноль, потому что искали только div.tab-content.
    """
    card = _parse("case_elec-r1_48MS0012-01-2026-001030-63.html")

    assert card["category"].startswith("Споры, связанные с имущественными правами")
    assert card["judge_names"] == ["Свиридова Маргарита Вячеславовна"]
    assert card["first_instance_decision"] == "Судебный приказ"
    assert len(card["events"]) == 1
    assert {side["role"] for side in card["sides"]} == {"Взыскатель", "Должник"}


def test_header_row_inside_tbody_is_not_a_case_state() -> None:
    """Строка-шапка в теле таблицы не должна становиться состоянием дела.

    Так свёрстан тип C (там <thead> нет вовсе). Если принять её за событие, карточка с
    чужой разметкой выглядела бы разобранной, и проверка пустого разбора в
    app/monitoring/tasks.py перестала бы её отсекать.
    """
    html = (
        '<div class="lawcase-content"><ul id="tabs"><li>ДВИЖЕНИЕ ДЕЛА</li></ul>'
        '<div id="contentt"><div class="tab-content"><table><tbody>'
        "<tr><td><b>Наименование события</b></td><td><b>Дата события</b></td>"
        "<td><b>Результат события</b></td></tr>"
        "</tbody></table></div></div></div>"
    )

    result = MsudrfTypeBParser().parse(html)

    assert result["events"] == []
    assert result["status"] is None


def test_material_proceeding_is_parsed_like_a_case() -> None:
    """Производство по материалу разбирается как дело — вкладки просто названы иначе.

    Регресс. У материалов (delo_id=1610001 в ссылке) вкладки называются «МАТЕРИАЛ» и
    «ДВИЖЕНИЕ МАТЕРИАЛА», а судья подписан коротко — «Судья». Пока эти названия не знали,
    от карточки оставались только стороны: ни полей, ни событий, ни судьи.
    """
    card = _parse("case_bond_nouid-68MS0001-909bec32420a.html")

    assert card["judge_names"] == ["Агзаметдинова Венера Ринатовна"]
    assert len(card["events"]) == 1
    assert card["status"] == "Рассмотрение материала"
    assert {side["role"] for side in card["sides"]} == {"Взыскатель", "Должник", "Заявитель"}


def test_participants_tab_named_persons_may_hold_a_sides_table() -> None:
    """Смоленская область: вкладка «ЛИЦА», а таблица внутри — обычные стороны.

    Регресс. Разбор выбирался по НАЗВАНИЮ вкладки, а «ЛИЦА» уголовного дела и «ЛИЦА»
    Смоленской области — разные таблицы: в первой есть колонка «ФИО», во второй
    процессуальный статус и наименование лица. Из-за этого стороны терялись целиком.
    """
    sides = _parse("case_1_nouid-67MS0001-418a046b6977.html")["sides"]

    assert sides == [
        {"role": "Взыскатель", "full_name": 'ООО "Региональная Служба Взыскания"'},
        {"role": "Должник", "full_name": "Орлов Алексей Владимирович"},
    ]
