"""Тесты статусной машины задачи sync_case: задача не должна «залипать» в RUNNING.

К сайту суда не ходим: define_court_by_uid подменяется заглушкой, которая сразу бросает
ошибку получения страницы. Chromium и сеть тестам не нужны.

Работают на настоящем Postgres, потому что _sync_case открывает свои сессии через
session_scope() и коммитит их — подменить это внешней транзакцией нельзя. Созданную
строку search_task фикстура удаляет за собой.


Часть прежних тестов снята: они проверяли полный путь через заглушки СТАРОГО
контракта клиента, а сам путь теперь покрыт на настоящих сохранённых страницах:

* test_card_without_uid_gets_synthetic_key — теперь test_discovery_and_resync.py::test_page_without_uid_gets_a_synthetic_key
* test_synthetic_key_survives_uid_appearing_later — теперь test_identity_resolution.py::test_real_uid_appearing_later_does_not_rekey_the_card
* test_one_filled_field_is_enough_to_save — теперь test_parsers_golden.py и ParsedCase.is_empty
* test_all_rows_of_the_results_table_become_cards — теперь test_discovery_and_resync.py::test_discovery_by_uid_saves_every_card_of_the_search
* test_broken_row_does_not_lose_the_others — теперь test_discovery_and_resync.py::test_card_from_an_unknown_participok_is_reported_not_fatal

Здесь осталось то, что есть только здесь: состояния SearchTask, повторы и учёт
расходов на капчу.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry
from sqlalchemy import delete, select

from app.services import discovery
from app.captcha import ATTEMPT_SOLVED, CaptchaAttempt
from app.courts import CaseNotFound, FetchedCard
from app.parsers import ParsedCase
from app.models import CaptchaSolve, SearchStatus, SearchTask
from app.database import session_scope
from app import tasks
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
    court = discovery.CourtRef(id=1, code="50MS0095")
    monkeypatch.setattr(discovery, "_court_by_url", lambda url: court)
    return court


@pytest.fixture
def court_is_down(monkeypatch):
    """Портал не открывается: любой поход за страницей падает по таймауту."""

    def _boom(uid: str, proxy=None, **kwargs):
        raise TimeoutError('Page.fill: Timeout 30000ms exceeded.')

    monkeypatch.setattr(discovery, "define_court_by_uid", _boom)


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

    tasks._run(stub, task_id)

    status, last_error, _ = _status(task_id)
    assert status is SearchStatus.FAILED
    assert last_error.startswith("Исчерпаны попытки")
    assert stub.retry_called is False


def test_retries_while_attempts_left(task_id, court_is_down) -> None:
    """Попытки ещё есть → штатный ретрай, статус остаётся RUNNING."""
    stub = _StubTask(retries=0)

    with pytest.raises(Retry):
        tasks._run(stub, task_id)

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

    monkeypatch.setattr(tasks, "_run", _boom)

    result = tasks.run_search_task.apply(args=[task_id])

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

    monkeypatch.setattr(tasks, "_run", _retry)

    tasks.run_search_task.apply(args=[task_id])

    status, _, _ = _status(task_id)
    assert status is SearchStatus.PENDING  # статус не тронут: фикстура создала PENDING


# ------------------------------------------------------ дело, заведённое по ссылке
class _StopBeforeWrite(Exception):
    """Прервать задачу перед записью дела: саму запись проверяют другие тесты."""


class _StubParser:
    """Парсер-заглушка: отдаёт одно заполненное поле, чтобы разбор не считался пустым."""

    def parse(self, html):
        return ParsedCase(status="Рассмотрено")


def test_url_task_discovers_uid_and_saves_link(url_task_id, url_court, monkeypatch) -> None:
    """Задача по ссылке: открыли страницу, нашли УИД и номер дела, записали ссылку в дело.

    Это главное отличие второго входа: УИД не приходит извне, а добывается со страницы.
    Суд при этом берётся из ХОСТА ссылки и известен ещё до похода на портал — по УИД его
    не определяют.
    """
    recorded = {}

    class _Client:
        def fetch_card_by_url(self, url):
            recorded["fetched"] = url
            # УИД должен стоять В РАЗМЕТКЕ: его читают со страницы (find_uid), а не
            # спрашивают у клиента, — иначе карточку без УИД нечем было бы отличить от
            # страницы, которую портал отдал вместо дела.
            #
            # Заголовок «ДЕЛО № …» здесь тоже настоящий: номер дела достаёт из него
            # extract_msudrf_case_code, и он же служит доказательством, что открылась
            # именно карточка.
            return FetchedCard(
                html=(
                    "<html><body><h2>ДЕЛО № 2-1585/2026</h2>"
                    f"карточка, УИД {URL_CASE_UID}</body></html>"
                ),
                source_url=url,
            )

    monkeypatch.setattr(discovery, "define_court_by_url", lambda url, proxy=None, **kw: _Client()
    )
    # Разбор — не забота клиента, поэтому подменяем выбор парсера. Что именно парсеры
    # достают из страниц, проверяют tests/test_parsers_golden.py.
    monkeypatch.setattr(
        discovery, "get_parser", lambda portal, html: _StubParser()
    )
    # По УИД такое дело не ищется — если задача полезет этим путём, тест это поймает.
    monkeypatch.setattr(discovery, "define_court_by_uid",
        lambda uid, proxy=None, **kw: pytest.fail("по ссылке искать по УИД не должны"),
    )
    monkeypatch.setattr(discovery, "_take_snapshot", lambda *a, **kw: None)
    captured = {}

    def _sync_case(session, uid, parsed, court, code, source_url=None):
        captured["uid"] = uid
        captured["parsed"] = parsed
        captured["court_code"] = court.code
        captured["code"] = code
        # Адрес приходит ОТДЕЛЬНЫМ аргументом, а не внутри разбора: он не содержимое
        # карточки, а знание того, кто ходил на портал.
        captured["source_url"] = source_url
        raise _StopBeforeWrite

    monkeypatch.setattr(discovery, "sync_case", _sync_case)
    monkeypatch.setattr(discovery, "CourtRepository",
        lambda session: SimpleNamespace(
            get_by_code=lambda c: SimpleNamespace(id=1, code=c)
        ),
    )

    # Заглушка сверки прерывает работу исключением, а неизвестное исключение задача
    # трактует как ВРЕМЕННЫЙ отказ и просит повтор. Это правильная реакция, поэтому
    # ждём именно Retry: проверяем не её, а то, с чем позвали сверку.
    with pytest.raises(Retry):
        tasks._run(_StubTask(retries=0), url_task_id)

    assert recorded["fetched"] == CASE_URL
    assert captured["uid"] == URL_CASE_UID
    # Суд — из хоста ссылки, номер дела — из заголовка страницы.
    assert captured["court_code"] == url_court.code
    assert captured["code"] == "2-1585/2026"
    # Ссылку кладём в дело: по ней его будут открывать при каждом следующем обходе.
    assert captured["source_url"] == CASE_URL

    # УИД дописан в задачу сразу после похода — виден в статусе, даже если разбор упал.
    with session_scope() as session:
        assert session.get(SearchTask, url_task_id).uid == URL_CASE_UID


def _client_returning(html: str, code: str = "2-370/4520"):
    """Клиент-заглушка, отдающий заранее заданную разметку карточки."""

    class _Client:
        page_type = "B"

        def fetch_card_by_url(self, url):
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

    monkeypatch.setattr(discovery, "define_court_by_url", lambda url, proxy=None, **kw: client
    )
    monkeypatch.setattr(discovery, "_take_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(discovery, "sync_case", _update_case)

    with pytest.raises(_StopBeforeWrite):
        tasks._run(_StubTask(retries=0), task_row_id)

    return captured


def test_page_without_case_number_is_still_a_final_failure(
    url_task_id, url_court, monkeypatch
) -> None:
    """Открылась не карточка → окончательный отказ.

    Раньше эту роль играло отсутствие УИД; теперь доказательство карточки — номер дела в
    заголовке, и проверка должна остаться такой же строгой.
    """

    class _Client:
        def fetch_card_by_url(self, url):
            # Не карточка: заголовка «ДЕЛО № …» здесь нет, поэтому номер дела достать
            # неоткуда — extract_msudrf_case_code упадёт с CaseNotFound.
            return FetchedCard(
                html="<html>дело снято с публикации</html>", source_url=url
            )

    monkeypatch.setattr(discovery, "define_court_by_url", lambda url, proxy=None, **kw: _Client()
    )

    tasks._run(_StubTask(retries=0), url_task_id)

    status, last_error, _ = _status(url_task_id)
    assert status is SearchStatus.FAILED
    assert "номера дела" in last_error


def test_empty_parse_is_a_failure_and_does_not_touch_the_card(
    url_task_id, url_court, monkeypatch
) -> None:
    """Чужую разметку не сохраняем, карточку не трогаем.

    Сохранить такой разбор нельзя: страница считается источником истины, и у уже
    существующей карточки обход удалил бы все события и отвязал судей и стороны.

    Механизм отказа при переносе сменился, исход — нет. Раньше парсер выбирался по
    константе клиента, на чужой разметке отдавал пустой результат, и его отсекал охранник
    пустого разбора. Теперь вёрстку определяет сама страница, и неопознанная разметка
    даёт UnsupportedPage сразу. В обоих случаях задача FAILED, а карточка не тронута —
    это и проверяем.
    """

    class _Client:
        def fetch_card_by_url(self, url):
            # Заголовок с номером есть — значит карточка открылась. А разобрать её нечем:
            # вёрстка чужая, и парсер честно отдаст пустой результат.
            return FetchedCard(
                html=(
                    "<html><body><h2>ДЕЛО № 2-370/4520</h2>"
                    f"чужая разметка, УИД {URL_CASE_UID}</body></html>"
                ),
                source_url=url,
            )

    monkeypatch.setattr(discovery, "define_court_by_url", lambda url, proxy=None, **kw: _Client()
    )
    monkeypatch.setattr(discovery, "_take_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(discovery, "sync_case",
        lambda *a, **kw: pytest.fail("пустой разбор сохранять нельзя"),
    )

    tasks._run(_StubTask(retries=0), url_task_id)

    status, last_error, _ = _status(url_task_id)
    assert status is SearchStatus.FAILED
    # Причина названа честно: разобрать страницу нечем. Формулировка сменилась вместе с
    # механизмом (см. докстринг), поэтому проверяем суть, а не текст целиком.
    assert "разобрать" in last_error


def test_url_task_fails_when_host_is_not_in_reference(url_task_id, monkeypatch) -> None:
    """Хоста нет в справочнике → задача падает сразу, на портал не ходим.

    Поход занимает полминуты и стоит капчи, а без суда карточку всё равно не сохранить.
    """
    monkeypatch.setattr(discovery, "_court_by_url", lambda url: None)
    monkeypatch.setattr(discovery, "define_court_by_url",
        lambda url, proxy=None, **kw: pytest.fail("на портал идти не должны"),
    )

    tasks._run(_StubTask(retries=0), url_task_id)

    status, last_error, _ = _status(url_task_id)
    assert status is SearchStatus.FAILED
    assert "95.mo.msudrf.ru" in last_error


def test_unknown_participok_fails_the_task(task_id, monkeypatch) -> None:
    """Участка нет в справочнике → дело не парсим, задача падает с внятной ошибкой."""

    monkeypatch.setattr(discovery, "define_court_by_uid",
        lambda uid, proxy=None, **kw: SimpleNamespace(
            fetch_cases_by_uid=lambda _: [
                FetchedCard(case_code="02-0848/2/2026", html="<html/>", participok_no=777)
            ],
            parse=lambda html: pytest.fail("без суда разбирать нечего"),
        ),
    )
    monkeypatch.setattr(discovery, "_court_by_participok", lambda region_code, number: None)

    tasks._run(_StubTask(retries=0), task_id)

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
            def fetch_card_by_url(self, url):
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

    monkeypatch.setattr(discovery, "define_court_by_url", _court)

    with pytest.raises(Retry):
        tasks._run(_StubTask(retries=0), url_task_id)

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
