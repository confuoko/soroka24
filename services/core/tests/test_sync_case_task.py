"""Тесты статусной машины задачи sync_case: задача не должна «залипать» в RUNNING.

К сайту суда не ходим: define_court_by_uid подменяется заглушкой, которая сразу бросает
ошибку получения страницы. Chromium и сеть тестам не нужны.

Работают на настоящем Postgres, потому что _sync_case открывает свои сессии через
session_scope() и коммитит их — подменить это внешней транзакцией нельзя. Созданную
строку search_task фикстура удаляет за собой.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry
from sqlalchemy import delete, select

from app.captcha import ATTEMPT_SOLVED, CaptchaAttempt
from app.courts import CaseNotFound
from app.models.database import CaptchaSolve, SearchStatus, SearchTask, session_scope
from app.monitoring import tasks
from app.repositories import SearchTaskRepository
from app.validators import is_synthetic_uid, synthetic_uid

CASE_UID = "77MS0002-01-2026-000003-33"
CASE_URL = (
    "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs"
    "&case_id=429386415&delo_id=1540005"
)
# УИД, который «найдётся» на странице, открытой по ссылке.
URL_CASE_UID = "50MS0095-01-2026-002990-16"


class _StubTask:
    """Мини-подмена Celery-таска: нужны только счётчик попыток, лимит и retry()."""

    max_retries = 3

    def __init__(self, retries: int) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.retry_called = False

    def retry(self, exc=None, countdown=None):
        self.retry_called = True
        return Retry()


def _cleanup(task_row_id: int) -> None:
    """Убрать за тестом задачу и записанные ею расходы на капчу.

    Расходы удаляем явно: у captcha_solve.search_task_id стоит SET NULL, поэтому вместе
    с задачей строки не уходят — так и задумано (деньги потрачены), но в тестовой базе
    им оставаться незачем.
    """
    with session_scope() as session:
        session.execute(
            delete(CaptchaSolve).where(CaptchaSolve.search_task_id == task_row_id)
        )
        row = session.get(SearchTask, task_row_id)
        if row is not None:
            session.delete(row)


@pytest.fixture
def task_id():
    """Реальная строка search_task в PENDING; после теста удаляется."""
    with session_scope() as session:
        created = SearchTaskRepository(session).create(CASE_UID)
        created_id = created.id

    yield created_id

    _cleanup(created_id)


@pytest.fixture
def url_task_id():
    """Задача, заведённая ССЫЛКОЙ: УИД у неё пока пуст, как и бывает в жизни."""
    with session_scope() as session:
        created = SearchTaskRepository(session).create(source_url=CASE_URL)
        created_id = created.id

    yield created_id

    _cleanup(created_id)


@pytest.fixture
def url_court(monkeypatch):
    """Суд для дела, пришедшего ссылкой: определяется по хосту ещё до похода на портал.

    Подменяем сам резолвер, а не заливаем справочник: эти тесты про статусную машину
    задачи, а сопоставление хоста с судом проверяется отдельно (test_court_lookup.py).
    """
    court = tasks.CourtRef(id=1, code="50MS0095")
    monkeypatch.setattr(tasks, "_court_by_url", lambda url: court)
    return court


@pytest.fixture
def court_is_down(monkeypatch):
    """Портал не открывается: любой поход за страницей падает по таймауту."""

    def _boom(uid: str, proxy=None, **kwargs):
        raise TimeoutError('Page.fill: Timeout 30000ms exceeded.')

    monkeypatch.setattr(tasks, "define_court_by_uid", _boom)


def _status(task_id: int) -> tuple[SearchStatus, str | None, int]:
    with session_scope() as session:
        row = session.get(SearchTask, task_id)
        return row.status, row.last_error, row.attempts


def test_failed_when_retries_exhausted(task_id, court_is_down) -> None:
    """Попытки исчерпаны → FAILED.

    Регресс на причину A: Celery при исчерпании попыток пробрасывает исходный exc, а не
    MaxRetriesExceededError, поэтому прежний `except self.MaxRetriesExceededError` не
    срабатывал и задача навсегда оставалась в RUNNING.
    """
    stub = _StubTask(retries=_StubTask.max_retries)

    tasks._sync_case(stub, task_id)

    status, last_error, _ = _status(task_id)
    assert status is SearchStatus.FAILED
    assert last_error.startswith("Исчерпаны попытки")
    assert stub.retry_called is False


def test_retries_while_attempts_left(task_id, court_is_down) -> None:
    """Попытки ещё есть → штатный ретрай, статус остаётся RUNNING."""
    stub = _StubTask(retries=0)

    with pytest.raises(Retry):
        tasks._sync_case(stub, task_id)

    status, last_error, attempts = _status(task_id)
    assert status is SearchStatus.RUNNING
    assert "Timeout" in last_error
    assert attempts == 1
    assert stub.retry_called is True


def test_unexpected_error_closes_task(task_id, monkeypatch) -> None:
    """Непредвиденная ошибка в теле задачи → FAILED, а не вечный RUNNING.

    Регресс на причину B: IntegrityError на коммите дела вылетал наружу, минуя все
    перечисленные обработчики, и задача оставалась в RUNNING, блокируя УИД в API.
    """

    def _boom(celery_task, task_id_arg):
        raise ValueError("что-то неучтённое")

    monkeypatch.setattr(tasks, "_sync_case", _boom)

    result = tasks.sync_case.apply(args=[task_id])

    assert result.failed()
    status, last_error, _ = _status(task_id)
    assert status is SearchStatus.FAILED
    assert last_error.startswith("Непредвиденная ошибка")


def test_retry_is_not_swallowed_by_the_guard(task_id, monkeypatch) -> None:
    """Обёртка не должна принимать штатный ретрай за провал.

    Retry — наследник Exception, поэтому без отдельной ветки `except Retry: raise`
    каждая повторная попытка помечала бы задачу FAILED.
    """

    def _retry(celery_task, task_id_arg):
        raise Retry()

    monkeypatch.setattr(tasks, "_sync_case", _retry)

    tasks.sync_case.apply(args=[task_id])

    status, _, _ = _status(task_id)
    assert status is SearchStatus.PENDING  # статус не тронут: фикстура создала PENDING


# ------------------------------------------------------ дело, заведённое по ссылке
class _StopBeforeWrite(Exception):
    """Прервать задачу перед записью дела: саму запись проверяют другие тесты."""


def test_url_task_discovers_uid_and_saves_link(url_task_id, url_court, monkeypatch) -> None:
    """Задача по ссылке: открыли страницу, нашли УИД и номер дела, записали ссылку в дело.

    Это главное отличие второго входа: УИД не приходит извне, а добывается со страницы.
    Суд при этом берётся из ХОСТА ссылки и известен ещё до похода на портал — по УИД его
    не определяют.
    """
    recorded = {}

    class _Client:
        page_type = "B"

        def fetch_case_html_by_url(self, url):
            recorded["fetched"] = url
            # УИД должен стоять В РАЗМЕТКЕ: задача читает его со страницы (find_uid), а
            # не спрашивает у клиента, — иначе карточку без УИД нечем было бы отличить
            # от страницы, которую портал отдал вместо дела.
            return f"<html>карточка, УИД {URL_CASE_UID}</html>"

        def extract_case_code(self, html):
            return "2-1585/2026"

        def parse(self, html):
            return {"status": "Рассмотрено"}

    monkeypatch.setattr(
        tasks, "define_court_by_url", lambda url, proxy=None, **kw: _Client()
    )
    # По УИД такое дело не ищется — если задача полезет этим путём, тест это поймает.
    monkeypatch.setattr(
        tasks,
        "define_court_by_uid",
        lambda uid, proxy=None, **kw: pytest.fail("по ссылке искать по УИД не должны"),
    )
    monkeypatch.setattr(tasks, "_take_snapshot", lambda *a, **kw: (None, False))
    captured = {}

    def _update_case(session, uid, data, court, code):
        captured["uid"] = uid
        captured["data"] = data
        captured["court_code"] = court.code
        captured["code"] = code
        raise _StopBeforeWrite

    monkeypatch.setattr(tasks, "update_case", _update_case)
    monkeypatch.setattr(
        tasks,
        "CourtRepository",
        lambda session: SimpleNamespace(
            get_by_code=lambda c: SimpleNamespace(id=1, code=c)
        ),
    )

    with pytest.raises(_StopBeforeWrite):
        tasks._sync_case(_StubTask(retries=0), url_task_id)

    assert recorded["fetched"] == CASE_URL
    assert captured["uid"] == URL_CASE_UID
    # Суд — из хоста ссылки, номер дела — из заголовка страницы.
    assert captured["court_code"] == url_court.code
    assert captured["code"] == "2-1585/2026"
    # Ссылку кладём в дело: по ней его будут открывать при каждом следующем обходе.
    assert captured["data"]["url"] == CASE_URL

    # УИД дописан в задачу сразу после похода — виден в статусе, даже если разбор упал.
    with session_scope() as session:
        assert session.get(SearchTask, url_task_id).uid == URL_CASE_UID


def _client_returning(html: str, code: str = "2-370/4520"):
    """Клиент-заглушка, отдающий заранее заданную разметку карточки."""

    class _Client:
        page_type = "B"

        def fetch_case_html_by_url(self, url):
            return html

        def extract_case_code(self, html_arg):
            return code

        def parse(self, html_arg):
            return {"status": "Рассмотрено"}

    return _Client()


def _run_url_task_capturing_uid(task_row_id: int, monkeypatch, client) -> dict:
    """Прогнать задачу по ссылке до записи дела и вернуть то, с чем её позвали."""
    captured = {}

    def _update_case(session, uid, data, court, code):
        captured["uid"] = uid
        captured["code"] = code
        raise _StopBeforeWrite

    monkeypatch.setattr(
        tasks, "define_court_by_url", lambda url, proxy=None, **kw: client
    )
    monkeypatch.setattr(tasks, "_take_snapshot", lambda *a, **kw: (None, False))
    monkeypatch.setattr(tasks, "update_case", _update_case)

    with pytest.raises(_StopBeforeWrite):
        tasks._sync_case(_StubTask(retries=0), task_row_id)

    return captured


def test_card_without_uid_gets_synthetic_key(url_task_id, url_court, monkeypatch) -> None:
    """УИД на карточке нет вовсе → ключ считаем сами от ссылки, дело сохраняется.

    Так устроены архивные дела движка msudrf.ru (УИД начали присваивать примерно с 2021
    года) и целые регионы вроде Магаданской области. Раньше такая карточка отсекалась
    окончательной ошибкой «На странице нет уникального идентификатора дела».
    """
    client = _client_returning("<html>Дело № 2-370/4520, УИД тут нет</html>")

    captured = _run_url_task_capturing_uid(url_task_id, monkeypatch, client)

    assert captured["uid"] == synthetic_uid(url_court.code, CASE_URL)
    assert is_synthetic_uid(captured["uid"])
    # Ключ детерминирован: та же ссылка в другом виде даёт то же значение.
    assert captured["uid"] == synthetic_uid(
        url_court.code, CASE_URL.replace("https://", "http://")
    )


def test_synthetic_key_survives_uid_appearing_later(
    url_task_id, url_court, monkeypatch
) -> None:
    """Портал дозаполнил УИД у уже сохранённой карточки → ключ карточки НЕ меняем.

    Иначе поехали бы uid событий, документов и заседаний (они считаются от Case.card_key)
    и путь снапшотов в S3: дочерние строки перестали бы узнаваться и продублировались.
    """
    known_uid = synthetic_uid(url_court.code, CASE_URL)
    monkeypatch.setattr(
        tasks,
        "CaseRepository",
        lambda session: SimpleNamespace(get_by_url=lambda url: SimpleNamespace(uid=known_uid)),
    )
    client = _client_returning(f"<html>Дело, теперь с УИД {URL_CASE_UID}</html>")

    captured = _run_url_task_capturing_uid(url_task_id, monkeypatch, client)

    assert captured["uid"] == known_uid


def test_page_without_case_number_is_still_a_final_failure(
    url_task_id, url_court, monkeypatch
) -> None:
    """Открылась не карточка → окончательный отказ.

    Раньше эту роль играло отсутствие УИД; теперь доказательство карточки — номер дела в
    заголовке, и проверка должна остаться такой же строгой.
    """

    class _Client:
        page_type = "B"

        def fetch_case_html_by_url(self, url):
            return "<html>дело снято с публикации</html>"

        def extract_case_code(self, html):
            raise CaseNotFound("На странице нет номера дела")

    monkeypatch.setattr(
        tasks, "define_court_by_url", lambda url, proxy=None, **kw: _Client()
    )

    tasks._sync_case(_StubTask(retries=0), url_task_id)

    status, last_error, _ = _status(url_task_id)
    assert status is SearchStatus.FAILED
    assert "номера дела" in last_error


def test_empty_parse_is_a_failure_and_does_not_touch_the_card(
    url_task_id, url_court, monkeypatch
) -> None:
    """Разбор не дал ничего → ошибка разбора, карточку не трогаем.

    Так выглядит чужая разметка: парсер на незнакомой странице не падает, а возвращает
    пустой результат. Сохранить его нельзя — страница считается источником истины, и у уже
    существующей карточки обход удалил бы все события и отвязал судей и стороны.
    """

    class _Client:
        page_type = "B"

        def fetch_case_html_by_url(self, url):
            return f"<html>чужая разметка, УИД {URL_CASE_UID}</html>"

        def extract_case_code(self, html):
            return "2-370/4520"

        def parse(self, html):
            # Ровно то, что отдаёт парсер типа B на разметке типа C с другими вкладками.
            return {
                "receipt_date": None,
                "category": None,
                "status": None,
                "judge_names": [],
                "sides": [],
                "events": [],
                "place_history": [],
                "court_sessions": [],
                "documents": [],
            }

    monkeypatch.setattr(
        tasks, "define_court_by_url", lambda url, proxy=None, **kw: _Client()
    )
    monkeypatch.setattr(tasks, "_take_snapshot", lambda *a, **kw: (None, False))
    monkeypatch.setattr(
        tasks,
        "update_case",
        lambda *a, **kw: pytest.fail("пустой разбор сохранять нельзя"),
    )

    tasks._sync_case(_StubTask(retries=0), url_task_id)

    status, last_error, _ = _status(url_task_id)
    assert status is SearchStatus.FAILED
    assert "другая разметка" in last_error


def test_one_filled_field_is_enough_to_save(url_task_id, url_court, monkeypatch) -> None:
    """У свежего дела карточка почти пуста — это НЕ ошибка разметки, дело сохраняем.

    Граница проверки: пустым должно быть всё сразу. У только что заведённого дела нет ни
    результата, ни дат в движении, но состояние с портала есть — этого достаточно.
    """
    client = _client_returning(
        f"<html>Дело № 2-370/4520, УИД {URL_CASE_UID}</html>"
    )
    client.parse = lambda html: {
        "status": "Назначено судебное заседание",
        "judge_names": [],
        "sides": [],
        "events": [],
    }

    captured = _run_url_task_capturing_uid(url_task_id, monkeypatch, client)

    assert captured["uid"] == URL_CASE_UID


def test_url_task_fails_when_host_is_not_in_reference(url_task_id, monkeypatch) -> None:
    """Хоста нет в справочнике → задача падает сразу, на портал не ходим.

    Поход занимает полминуты и стоит капчи, а без суда карточку всё равно не сохранить.
    """
    monkeypatch.setattr(tasks, "_court_by_url", lambda url: None)
    monkeypatch.setattr(
        tasks,
        "define_court_by_url",
        lambda url, proxy=None, **kw: pytest.fail("на портал идти не должны"),
    )

    tasks._sync_case(_StubTask(retries=0), url_task_id)

    status, last_error, _ = _status(url_task_id)
    assert status is SearchStatus.FAILED
    assert "95.mo.msudrf.ru" in last_error


# ------------------------------------------------- несколько карточек на один УИД
def test_all_rows_of_the_results_table_become_cards(task_id, monkeypatch) -> None:
    """По одному УИД портал показал два производства → сохраняем ОБА.

    Раньше клиент брал только первую строку таблицы, и второе производство (а иногда и
    другой участок) молча терялось.
    """
    from app.courts import FetchedCard

    cards = [
        FetchedCard(code="02-0848/2/2026", html="<html>первое</html>", participok_no=2),
        FetchedCard(code="05-0445/23/2026", html="<html>второе</html>", participok_no=23),
    ]
    courts = {2: tasks.CourtRef(id=1, code="77MS0002"), 23: tasks.CourtRef(id=2, code="77MS0023")}

    monkeypatch.setattr(
        tasks,
        "define_court_by_uid",
        lambda uid, proxy=None, **kw: SimpleNamespace(
            fetch_cases_by_uid=lambda _: cards,
            parse=lambda html: {"status": html},
        ),
    )
    monkeypatch.setattr(
        tasks, "_court_by_participok", lambda region_code, number: courts[number]
    )
    monkeypatch.setattr(tasks, "_take_snapshot", lambda *a, **kw: (None, False))
    monkeypatch.setattr(tasks, "_attach_captcha_costs_to_case", lambda *a, **kw: None)
    # Карточки поддельные, в БД их нет — настоящий mark_success упал бы на внешнем ключе.
    succeeded = []
    monkeypatch.setattr(
        tasks, "_mark_success", lambda tid, case_id: succeeded.append(case_id)
    )

    saved = []
    ids = iter([101, 102])

    def _update_case(session, uid, data, court, code):
        saved.append((court.code, code, data["status"]))
        return SimpleNamespace(
            case=SimpleNamespace(id=next(ids)),
            has_changes=lambda: False,
            field_changes=[], new_events=[], updated_events=[], removed_events=[],
            new_places=[], updated_places=[], removed_places=[],
            new_sessions=[], updated_sessions=[], removed_sessions=[],
            new_documents=[], removed_documents=[],
            added_judges=[], removed_judges=[], added_sides=[], removed_sides=[],
        )

    monkeypatch.setattr(tasks, "update_case", _update_case)
    monkeypatch.setattr(tasks, "append_parse_entry", lambda case, entry: None)
    monkeypatch.setattr(tasks, "changes_to_dict", lambda changes: {})
    monkeypatch.setattr(
        tasks,
        "CourtRepository",
        lambda session: SimpleNamespace(
            get_by_code=lambda c: SimpleNamespace(id=1, code=c)
        ),
    )

    tasks._sync_case(_StubTask(retries=0), task_id)

    # Каждая строка таблицы дала свою карточку — со своим судом и своим номером.
    assert saved == [
        ("77MS0002", "02-0848/2/2026", "<html>первое</html>"),
        ("77MS0023", "05-0445/23/2026", "<html>второе</html>"),
    ]
    # В самой задаче остаётся первая карточка: поле одно на задачу.
    assert succeeded == [101]


def test_broken_row_does_not_lose_the_others(task_id, monkeypatch) -> None:
    """Разбор одной карточки упал → вторая всё равно сохраняется, задача успешна.

    Это разные производства: то, что у одного поехала разметка, к другому отношения
    не имеет.
    """
    from app.courts import FetchedCard

    cards = [
        FetchedCard(code="02-0848/2/2026", html="<html>битая</html>", participok_no=2),
        FetchedCard(code="05-0445/2/2026", html="<html>целая</html>", participok_no=2),
    ]

    def _parse(html):
        if "битая" in html:
            raise ValueError("не нашёл карточку в разметке")
        return {"status": "ok"}

    monkeypatch.setattr(
        tasks,
        "define_court_by_uid",
        lambda uid, proxy=None, **kw: SimpleNamespace(
            fetch_cases_by_uid=lambda _: cards, parse=_parse
        ),
    )
    monkeypatch.setattr(
        tasks, "_court_by_participok", lambda region_code, number: tasks.CourtRef(id=1, code="77MS0002")
    )
    monkeypatch.setattr(tasks, "_take_snapshot", lambda *a, **kw: (None, False))
    monkeypatch.setattr(tasks, "_record_parse_entry", lambda *a, **kw: None)
    monkeypatch.setattr(tasks, "_attach_captcha_costs_to_case", lambda *a, **kw: None)
    succeeded = []
    monkeypatch.setattr(
        tasks, "_mark_success", lambda tid, case_id: succeeded.append(case_id)
    )

    saved = []

    def _update_case(session, uid, data, court, code):
        saved.append(code)
        return SimpleNamespace(
            case=SimpleNamespace(id=201),
            has_changes=lambda: False,
            field_changes=[], new_events=[], updated_events=[], removed_events=[],
            new_places=[], updated_places=[], removed_places=[],
            new_sessions=[], updated_sessions=[], removed_sessions=[],
            new_documents=[], removed_documents=[],
            added_judges=[], removed_judges=[], added_sides=[], removed_sides=[],
        )

    monkeypatch.setattr(tasks, "update_case", _update_case)
    monkeypatch.setattr(tasks, "append_parse_entry", lambda case, entry: None)
    monkeypatch.setattr(tasks, "changes_to_dict", lambda changes: {})
    monkeypatch.setattr(
        tasks,
        "CourtRepository",
        lambda session: SimpleNamespace(
            get_by_code=lambda c: SimpleNamespace(id=1, code=c)
        ),
    )

    tasks._sync_case(_StubTask(retries=0), task_id)

    assert saved == ["05-0445/2/2026"]
    assert succeeded == [201]  # задача успешна, хотя одна карточка и не далась


def test_unknown_participok_fails_the_task(task_id, monkeypatch) -> None:
    """Участка нет в справочнике → дело не парсим, задача падает с внятной ошибкой."""
    from app.courts import FetchedCard

    monkeypatch.setattr(
        tasks,
        "define_court_by_uid",
        lambda uid, proxy=None, **kw: SimpleNamespace(
            fetch_cases_by_uid=lambda _: [
                FetchedCard(code="02-0848/2/2026", html="<html/>", participok_no=777)
            ],
            parse=lambda html: pytest.fail("без суда разбирать нечего"),
        ),
    )
    monkeypatch.setattr(tasks, "_court_by_participok", lambda region_code, number: None)

    tasks._sync_case(_StubTask(retries=0), task_id)

    status, last_error, _ = _status(task_id)
    assert status is SearchStatus.FAILED
    assert "777" in last_error


# ----------------------------------------------------------------- учёт расходов
def test_captcha_cost_is_recorded_even_when_the_task_fails(
    url_task_id, url_court, monkeypatch
) -> None:
    """Расход на капчу записывается, даже если до карточки мы так и не добрались.

    Именно этот случай и теряется без учёта: портал показал проверку, мы за неё
    заплатили, а потом упали по таймауту — деньги ушли, а следа бы не осталось.
    """

    def _court(url, proxy=None, on_captcha_attempt=None, **kwargs):
        class _Client:
            page_type = "B"

            def fetch_case_html_by_url(self, url):
                # Так ведёт себя настоящий клиент: сначала платит за проверку, а падает
                # уже потом.
                on_captcha_attempt(
                    CaptchaAttempt(
                        task_id=987654,
                        status=ATTEMPT_SOLVED,
                        text="ответ",
                        cost=Decimal("0.031"),
                        attempt_no=1,
                        captcha_key="captcha/95.mo.msudrf.ru/429386415/x.png",
                    )
                )
                raise TimeoutError("Page.fill: Timeout 30000ms exceeded.")

        return _Client()

    monkeypatch.setattr(tasks, "define_court_by_url", _court)

    with pytest.raises(Retry):
        tasks._sync_case(_StubTask(retries=0), url_task_id)

    with session_scope() as session:
        row = session.scalar(
            select(CaptchaSolve).where(CaptchaSolve.search_task_id == url_task_id)
        )
        assert row is not None
        assert row.cost == Decimal("0.03100")
        assert row.provider_task_id == 987654
        assert row.attempt_no == 1
        # Хост берём из ссылки задачи: в УИД номера участка нет.
        assert row.host == "95.mo.msudrf.ru"
        # Дела в БД ещё нет, поэтому расход пока висит только на задаче.
        assert row.case_id is None
