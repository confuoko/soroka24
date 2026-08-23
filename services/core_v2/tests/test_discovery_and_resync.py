"""Интеграция: два входа, один путь сохранения.

Проверяется вся цепочка от «клиент отдал страницу» до строк в PostgreSQL:

    FetchedCard → суд + УИД + номер дела → get_parser → ParsedCase → sync_case → БД

Клиенты судов подменены фейками, которые отдают СОХРАНЁННЫЕ страницы из html_examples.
Сети здесь нет намеренно: настоящий поход на портал занимает 25-35 секунд, требует
прокси и платной капчи, а проверить надо не его, а сборку — что суд определился, номер
дела нашёлся, парсер выбрался по вёрстке и всё легло в базу. Сам поход покрыт отдельно:
tests/test_msudrf_court.py, tests/test_spb_court.py.

**Почему здесь нет фикстуры `session`.** Она работает во внешней транзакции с откатом, а
discover_case открывает свои короткие session_scope и КОММИТИТ — иначе поход браузером
держал бы транзакцию открытой полминуты. Два этих мира друг друга не видят: суд,
созданный в откатываемой транзакции, для discover_case не существует. Поэтому тесты
пользуются настоящим справочником судов и убирают за собой сами.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app import main
from app.courts import CaseNotFound, FetchedCard, UnsupportedCourt
from app.database import session_scope
from app.models import Case, Court, OutboxEventType
from app.repositories import CaseRepository, CourtRepository, OutboxEventRepository
from app.services import discovery
from app.services.discovery import discover_case, is_terminal, resync_case
from app.validators import canonical_case_url, is_synthetic_uid

pytestmark = pytest.mark.db

HTML_DIR = Path(__file__).resolve().parents[1] / "html_examples"

# Живая карточка Московской области: движок msudrf, вёрстка B, УИД на странице есть.
MO_PAGE = "mo_case_5_415323702.html"
MO_URL = "https://5.mo.msudrf.ru/modules.php?name=sud_delo&op=cd&delo_id=1540005"
MO_COURT_CODE = "50MS0005"

# Живая карточка Пермского края: тот же движок, вёрстка C, УИД на странице НЕТ.
PERM_PAGE = "case_96_nouid-59MS0096-8ea6caa0770d.html"
PERM_URL = "https://96.perm.msudrf.ru/modules.php?name=sud_delo&op=cd&delo_id=1540005"
PERM_COURT_CODE = "59MS0096"

# Мировой суд Москвы: сюда попадают дела, найденные поиском по УИД. Портал у них свой
# (mos-sud.ru) и вёрстка своя — тип A, поэтому и страница нужна именно его.
MOSCOW_UID = "77MS0002-01-2026-001579-64"
MOSCOW_PARTICIPOK = 2
MOSCOW_PAGE = "case_details_page_2.html"

REQUIRED_COURTS = (MO_COURT_CODE, PERM_COURT_CODE, "77MS0002")

# «С самого начала» для чтения событий: момент заведомо раньше любого из них.
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def page(name: str) -> str:
    return (HTML_DIR / name).read_text(encoding="utf-8")


class FakeClient:
    """Клиент суда, который никуда не ходит и отдаёт заранее заданные карточки."""

    def __init__(self, cards: list[FetchedCard]) -> None:
        self._cards = cards
        self.calls: list[str] = []

    def fetch_card_by_url(self, url: str) -> FetchedCard:
        self.calls.append(url)
        return self._cards[0]

    def fetch_cases_by_uid(self, uid: str) -> list[FetchedCard]:
        self.calls.append(uid)
        return list(self._cards)


@pytest.fixture(autouse=True)
def reference_loaded() -> None:
    """Тестам нужен залитый справочник судов: суд дела определяется по нему.

    Заливается скриптом scripts/sync_courts.py из data/courts.json.
    """
    with session_scope() as session:
        repo = CourtRepository(session)
        missing = [code for code in REQUIRED_COURTS if repo.get_by_code(code) is None]
    if missing:
        pytest.skip(f"справочник судов не залит, нет судов: {missing}")


@pytest.fixture
def created_cases():
    """Собрать id заведённых делом тестом карточек и убрать их за собой.

    Уборка нужна потому, что discover_case коммитит: без неё карточка осталась бы в базе
    и следующий прогон нашёл бы её как «уже существующую».

    Дочерние строки (события, документы, адреса) уходят каскадом.
    """
    ids: list[int] = []
    yield ids
    if not ids:
        return
    with session_scope() as session:
        for case_id in ids:
            case = session.get(Case, case_id)
            if case is not None:
                session.delete(case)


@pytest.fixture
def fake_portal(monkeypatch):
    """Подменить клиента суда. Возвращает функцию: список карточек -> сам фейк."""

    def _install(cards: list[FetchedCard]) -> FakeClient:
        client = FakeClient(cards)
        monkeypatch.setattr(discovery, "define_court_by_url", lambda url, **kw: client)
        monkeypatch.setattr(discovery, "define_court_by_uid", lambda uid, **kw: client)
        return client

    return _install


def crawl_url(fake_portal, created_cases, url: str, html_name: str):
    """Сходить по ссылке фейковым клиентом и запомнить заведённые карточки."""
    fake_portal([FetchedCard(html=page(html_name), source_url=url)])
    result = discover_case(source_url=url)
    created_cases.extend(result.saved_case_ids)
    return result


# ------------------------------------------------------- discovery по ссылке
def test_discovery_by_url_saves_the_card(fake_portal, created_cases) -> None:
    """Полный путь: ссылка -> страница -> суд -> УИД -> номер -> парсер B -> база."""
    result = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)

    assert result.is_success()
    assert result.failures == []
    assert len(result.saved_case_ids) == 1
    # Суд определился по ХОСТУ ссылки, а не из УИД.
    assert result.court.code == MO_COURT_CODE
    assert result.uid


def test_saved_card_has_events_and_a_court(fake_portal, created_cases) -> None:
    """В базе лежит именно разобранная карточка, а не пустая заготовка."""
    result = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)

    with session_scope() as check:
        case = check.get(Case, result.saved_case_ids[0])
        assert case.court.code == MO_COURT_CODE
        assert case.code  # номер дела — часть ключа карточки
        assert len(case.events) > 0
        assert len(case.judges) > 0


def test_url_is_remembered_as_the_card_address(fake_portal, created_cases) -> None:
    """Ссылка, которой завели дело, ложится в список адресов карточки.

    По ней карточку будут открывать при каждом следующем обходе, поэтому потеряться она
    не должна. Адрес хранится в канонической форме.
    """
    result = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)

    with session_scope() as check:
        case = check.get(Case, result.saved_case_ids[0])
        assert [u.url for u in case.urls] == [canonical_case_url(MO_URL)]


def test_page_without_uid_gets_a_synthetic_key(fake_portal, created_cases) -> None:
    """У карточки без УИД ключ считается от адреса — иначе её нечем опознать.

    Пермский край: вёрстка C того же движка, и УИД на странице нет вовсе.
    """
    result = crawl_url(fake_portal, created_cases, PERM_URL, PERM_PAGE)

    assert result.is_success()
    assert is_synthetic_uid(result.uid)
    assert result.court.code == PERM_COURT_CODE


def test_parser_is_chosen_by_the_markup_not_by_the_court(
    fake_portal, created_cases
) -> None:
    """Тот же портал и тот же клиент — разные парсеры по разной вёрстке.

    Московская область отдаёт вёрстку B, Пермский край — C. Если бы парсер выбирался по
    порталу, один из двух разборов вышел бы пустым и карточка не сохранилась бы.
    """
    b_result = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)
    c_result = crawl_url(fake_portal, created_cases, PERM_URL, PERM_PAGE)

    assert b_result.is_success(), "вёрстка B не разобралась"
    assert c_result.is_success(), "вёрстка C не разобралась"
    assert b_result.saved_case_ids != c_result.saved_case_ids


# --------------------------------------------------------- discovery по УИД
def test_discovery_by_uid_saves_every_card_of_the_search(
    fake_portal, created_cases
) -> None:
    """По одному УИД портал отдаёт таблицу — сохраняем ВСЕ её карточки.

    Это разные производства: приказное, затем исковое. Номер дела и номер участка
    приходят из строки таблицы, то есть известны до открытия карточки.
    """
    fake_portal(
        [
            FetchedCard(
                html=page(MOSCOW_PAGE),
                case_code="02-0111/2026",
                participok_no=MOSCOW_PARTICIPOK,
            ),
            FetchedCard(
                html=page(MOSCOW_PAGE),
                case_code="02-0222/2026",
                participok_no=MOSCOW_PARTICIPOK,
            ),
        ]
    )

    result = discover_case(uid=MOSCOW_UID)
    created_cases.extend(result.saved_case_ids)

    assert len(result.saved_case_ids) == 2
    assert result.failures == []


def test_card_from_an_unknown_participok_is_reported_not_fatal(
    fake_portal, created_cases
) -> None:
    """Суда нет в справочнике — эта карточка не сохраняется, остальные сохраняются.

    Отказ на одной карточке не уносит остальные: это разные производства.
    """
    fake_portal(
        [
            FetchedCard(
                html=page(MOSCOW_PAGE),
                case_code="02-0111/2026",
                participok_no=MOSCOW_PARTICIPOK,
            ),
            FetchedCard(
                html=page(MOSCOW_PAGE), case_code="02-0999/2026", participok_no=9999
            ),
        ]
    )

    result = discover_case(uid=MOSCOW_UID)
    created_cases.extend(result.saved_case_ids)

    assert len(result.saved_case_ids) == 1
    assert len(result.failures) == 1
    assert "справочник" in result.failures[0]


def test_empty_parse_is_not_saved(fake_portal, created_cases) -> None:
    """Пустой разбор не сохраняем: он затёр бы события уже существующей карточки.

    Страница считается источником истины, поэтому сохранение пустого разбора удалило бы
    все события и отвязало судей со сторонами. Смена разметки на портале молча вымела бы
    историю дела.
    """
    fake_portal(
        [
            FetchedCard(
                html="<html><head></head><body></body></html>",
                case_code="02-0111/2026",
                participok_no=MOSCOW_PARTICIPOK,
            )
        ]
    )

    result = discover_case(uid=MOSCOW_UID)
    created_cases.extend(result.saved_case_ids)

    assert result.saved_case_ids == []
    assert result.failures and "другая разметка" in result.failures[0]


# ------------------------------------------------------------------- отказы
def test_court_missing_from_the_reference_is_terminal(fake_portal) -> None:
    """Суда с таким хостом нет в справочнике — на портал даже не идём.

    Прокси и платная капча тратились бы впустую: сохранить карточку всё равно нечем.
    """
    with pytest.raises(UnsupportedCourt) as exc:
        discover_case(source_url="https://9999.mo.msudrf.ru/modules.php?delo_id=1")

    assert is_terminal(exc.value)


def test_neither_uid_nor_url_is_a_programming_error() -> None:
    """Без источника обходить нечего — падаем сразу и внятно."""
    with pytest.raises(ValueError):
        discover_case()


# -------------------------------------------------------------- повторный обход
def test_resync_walks_the_same_path(fake_portal, created_cases) -> None:
    """Повторный обход обновляет ТУ ЖЕ карточку, а не заводит новую."""
    first = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)
    case_id = first.saved_case_ids[0]

    fake_portal([FetchedCard(html=page(MO_PAGE), source_url=MO_URL)])
    again = resync_case(case_id)

    assert again.saved_case_ids == [case_id]


def test_resync_uses_the_saved_address(fake_portal, created_cases) -> None:
    """Источник берём тот, каким дело попало в систему: сохранённую ссылку."""
    first = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)

    client = fake_portal([FetchedCard(html=page(MO_PAGE), source_url=MO_URL)])
    resync_case(first.saved_case_ids[0])

    assert client.calls == [canonical_case_url(MO_URL)]


def test_resync_of_a_case_without_urls_goes_by_uid(fake_portal, created_cases) -> None:
    """Сохранённой ссылки нет — идём по УИД (так устроены дела, найденные поиском)."""
    with session_scope() as session:
        court = CourtRepository(session).get_by_code("77MS0002")
        case = Case(uid=MOSCOW_UID, court=court, code="02-0111/2026")
        session.add(case)
        session.flush()
        case_id = case.id
    created_cases.append(case_id)

    with session_scope() as check:
        assert CaseRepository(check).primary_url(check.get(Case, case_id)) is None

    client = fake_portal(
        [
            FetchedCard(
                html=page(MOSCOW_PAGE),
                case_code="02-0111/2026",
                participok_no=MOSCOW_PARTICIPOK,
            )
        ]
    )
    result = resync_case(case_id)
    created_cases.extend(result.saved_case_ids)

    assert client.calls == [MOSCOW_UID]
    assert result.saved_case_ids == [case_id]


def test_resync_of_a_missing_case_is_terminal() -> None:
    """Дела с таким id нет — повторять бессмысленно."""
    with pytest.raises(CaseNotFound) as exc:
        resync_case(10**9)

    assert is_terminal(exc.value)


def test_second_crawl_without_changes_changes_nothing(
    fake_portal, created_cases
) -> None:
    """Обход неизменившейся страницы не создаёт ни одной новой строки.

    Ключевое свойство: сверка идемпотентна, повторный обход не выглядит как изменение.
    """
    first = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)
    case_id = first.saved_case_ids[0]

    with session_scope() as check:
        case = check.get(Case, case_id)
        before = (len(case.events), len(case.judges), len(case.sides))
        changed_at = case.last_changed_at

    fake_portal([FetchedCard(html=page(MO_PAGE), source_url=MO_URL)])
    resync_case(case_id)

    with session_scope() as check:
        case = check.get(Case, case_id)
        assert (len(case.events), len(case.judges), len(case.sides)) == before
        # Факт похода отмечен, а дата изменения осталась прежней.
        assert case.last_checked_at is not None
        assert case.last_changed_at == changed_at

# ------------------------------------------------- события об изменениях (outbox)
def test_first_crawl_emits_no_events(fake_portal, created_cases) -> None:
    """Первый обход — baseline: событий об изменениях нет.

    Проверяем на настоящем пути, а не на сверке в отрыве: подавление baseline должно
    работать именно там, где события реально выпускаются.
    """
    result = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)

    with session_scope() as check:
        assert (
            OutboxEventRepository(check).list_since(
                result.saved_case_ids[0], EPOCH, limit=10
            )
            == []
        )


def test_change_found_on_re_crawl_emits_events(fake_portal, created_cases) -> None:
    """Изменение, найденное повторным обходом, попадает в поток событий.

    Изменение устраиваем честно, со стороны БАЗЫ: удаляем одно сохранённое событие, и
    тогда на странице оказывается строка, которой в базе нет. Так не приходится
    подделывать HTML — а проверить надо именно то, что сверка нашла расхождение и выпустила
    по нему событие.
    """
    first = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)
    case_id = first.saved_case_ids[0]

    with session_scope() as session:
        case = session.get(Case, case_id)
        removed = case.events[0].state_description
        session.delete(case.events[0])

    fake_portal([FetchedCard(html=page(MO_PAGE), source_url=MO_URL)])
    resync_case(case_id)

    with session_scope() as check:
        events = OutboxEventRepository(check).list_since(case_id, EPOCH)
        types = [event.event_type for event in events]
        assert OutboxEventType.EVENT_NEW in types
        assert any(
            event.payload.get("state_description") == removed for event in events
        )


def test_events_are_readable_over_http(fake_portal, created_cases) -> None:
    """Эндпоинт отдаёт то же, что лежит в таблице. В старом core его не было вовсе."""
    first = crawl_url(fake_portal, created_cases, MO_URL, MO_PAGE)
    case_id = first.saved_case_ids[0]

    with session_scope() as session:
        case = session.get(Case, case_id)
        session.delete(case.events[0])

    fake_portal([FetchedCard(html=page(MO_PAGE), source_url=MO_URL)])
    resync_case(case_id)

    with TestClient(main.app) as client:
        answer = client.get(f"/cases/{case_id}/events")
        assert answer.status_code == 200
        body = answer.json()
        assert body, "события должны были появиться"
        assert {"id", "event_type", "payload", "created_at"} <= set(body[0])
        # Полей доставки в ответе нет: core не знает получателей.
        assert "user_id" not in body[0]["payload"]

        # Повторный запрос с моментом последнего события не отдаёт его снова.
        again = client.get(
            f"/cases/{case_id}/events", params={"since": body[-1]["created_at"]}
        )
        assert again.json() == []


def test_events_of_a_missing_case_are_404() -> None:
    """Пустой список означал бы «изменений не было» — а это другое утверждение."""
    with TestClient(main.app) as client:
        assert client.get("/cases/999999999/events").status_code == 404
