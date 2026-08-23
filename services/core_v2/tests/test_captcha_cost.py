"""Учёт расходов на капчу: запись, идемпотентность, привязка к делу, суммы.

Идут на настоящем Postgres (фикстура session из conftest.py): проверяется в том числе
поведение UNIQUE-индекса на ON CONFLICT, а его на заглушке не подделать.

Главное, что здесь защищается, — честность денег: расход нельзя ни удвоить (ретрай
воркера), ни занизить (капча с неизвестной ценой не должна превратиться в ноль).
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.captcha import ATTEMPT_SOLVED, ATTEMPT_TIMEOUT, CaptchaAttempt
from app.models import CaptchaSolve, Case, SearchStatus, SearchTask
from app.repositories import CaptchaSolveRepository

# Нужен настоящий PostgreSQL: JSONB у payload событий и FOR UPDATE ... SKIP
# LOCKED у пула прокси. Пропустить весь такой набор: pytest -m "not db".
pytestmark = pytest.mark.db

CASE_URL = (
    "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs"
    "&case_id=429386415&delo_id=1540005"
)


def _attempt(task_id: int, cost: str | None = "0.03", **kwargs) -> CaptchaAttempt:
    """Разгаданная капча с заданной ценой (None — цена неизвестна)."""
    return CaptchaAttempt(
        task_id=task_id,
        status=ATTEMPT_SOLVED,
        text="ответ",
        cost=Decimal(cost) if cost is not None else None,
        **kwargs,
    )


@pytest.fixture
def task(session) -> SearchTask:
    """Задача синхронизации: расход всегда пишется в её рамках."""
    row = SearchTask(source_url=CASE_URL, status=SearchStatus.RUNNING)
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def case(session, court) -> Case:
    """Карточка дела, к которой потом привязываются расходы."""
    row = Case(uid="50MS0095-01-2026-002990-16", court_id=court.id, code="2-1585/2026")
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def costs(session) -> CaptchaSolveRepository:
    return CaptchaSolveRepository(session)


# ------------------------------------------------------------------------- запись
def test_solve_is_recorded_with_its_price(costs, task) -> None:
    """Цена, пришедшая от сервиса, ложится в БД как есть — по ней и считаем расход."""
    costs.record(
        _attempt(1001, attempt_no=1, captcha_key="captcha/x.png"),
        search_task_id=task.id,
        source_url=CASE_URL,
        captcha_bucket="soroka",
    )

    assert costs.total_cost_by_task(task.id) == Decimal("0.03")


def test_host_is_taken_from_the_link(costs, task, session) -> None:
    """Хост участка берём из ссылки: в УИД номера участка нет, а в отчёте он нужен."""
    costs.record(_attempt(1002), search_task_id=task.id, source_url=CASE_URL)

    row = session.scalar(
        select(CaptchaSolve).where(CaptchaSolve.provider_task_id == 1002)
    )
    assert row.host == "95.mo.msudrf.ru"


def test_repeated_record_does_not_double_the_cost(costs, task) -> None:
    """Повторная запись того же решения расход НЕ удваивает.

    Так и происходит в жизни: задача ретраится, колбэк учёта может сработать снова, а
    сервис за ту же капчу списал деньги ровно один раз.
    """
    costs.record(_attempt(1003), search_task_id=task.id)
    costs.record(_attempt(1003), search_task_id=task.id)

    assert costs.total_cost_by_task(task.id) == Decimal("0.03")


def test_costs_add_up_over_one_task(costs, task) -> None:
    """Один поход решает несколько капч — в задаче они складываются."""
    costs.record(_attempt(1004, attempt_no=1), search_task_id=task.id)
    costs.record(_attempt(1005, attempt_no=2), search_task_id=task.id)
    costs.record(_attempt(1006, attempt_no=3), search_task_id=task.id)

    assert costs.total_cost_by_task(task.id) == Decimal("0.09")


# ------------------------------------------------------------- привязка к делу
def test_attach_case_links_costs_of_the_task(costs, task, case) -> None:
    """Расход привязывается к делу задним числом.

    В момент разгадки дела ещё нет: задачу завели ссылкой, а УИД читается с той самой
    страницы, до которой мы через капчу и пробиваемся.
    """
    costs.record(_attempt(1007), search_task_id=task.id)
    costs.record(_attempt(1008), search_task_id=task.id)

    assert costs.attach_case(task.id, case.id) == 2
    assert costs.total_cost_by_case(case.id) == Decimal("0.06")


def test_attach_case_does_not_move_already_linked_costs(costs, session, task, case) -> None:
    """Уже привязанный расход не переклеиваем на другое дело.

    Одна задача — одна карточка; переносить деньги на чужое дело было бы порчей отчёта.
    """
    other = Case(uid="50MS0095-01-2026-000777-11", court_id=case.court_id, code="2-0777/2026")
    session.add(other)
    session.flush()
    costs.record(_attempt(1009), search_task_id=task.id, case_id=case.id)

    assert costs.attach_case(task.id, other.id) == 0
    assert costs.total_cost_by_case(case.id) == Decimal("0.03")
    assert costs.total_cost_by_case(other.id) == Decimal("0")


def test_case_without_captcha_costs_nothing(costs, case) -> None:
    """Дело, за которое не платили, стоит ноль, а не падает на None."""
    assert costs.total_cost_by_case(case.id) == Decimal("0")


# ------------------------------------------------------ неизвестная цена (таймаут)
def test_unknown_price_is_counted_separately(costs, task, case) -> None:
    """Капча с неизвестной ценой не превращается в ноль, а видна отдельным числом.

    Иначе сумма по делу выглядела бы полной, хотя часть расхода в неё не вошла.
    """
    costs.record(_attempt(1010, cost="0.04"), search_task_id=task.id)
    costs.record(
        CaptchaAttempt(task_id=1011, status=ATTEMPT_TIMEOUT), search_task_id=task.id
    )
    costs.attach_case(task.id, case.id)

    assert costs.total_cost_by_case(case.id) == Decimal("0.04")
    assert costs.unknown_cost_count(case.id) == 1


def test_timeout_row_has_no_solved_at(costs, session, task) -> None:
    """У таймаута нет времени разгадки: мы сдались, а не получили ответ."""
    costs.record(
        CaptchaAttempt(task_id=1012, status=ATTEMPT_TIMEOUT), search_task_id=task.id
    )

    row = session.scalar(
        select(CaptchaSolve).where(CaptchaSolve.provider_task_id == 1012)
    )
    assert row.status == ATTEMPT_TIMEOUT
    assert row.solved_at is None
    assert row.cost is None
