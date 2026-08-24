"""Мониторинг: состав регулярного обхода и ночной прогон.

Сценарии из ТЗ §11 «Основные tests», backend-часть: список синхронизируется идемпотентно,
дело получает верный флаг, планировщик берёт только помеченные дела и ставит каждое ровно
один раз.

Главные здесь два. test_repeated_sync_changes_nothing проверяет свойство, ради которого
ручка сделана замещающей: клиент присылает полное состояние, и повторная отправка того же
не должна ничего трогать — иначе каждый его запрос гонял бы UPDATE по всей таблице дел.
test_case_with_two_subscribers_is_crawled_once проверяет, что флаг живёт на карточке, а не
на подписке: поход на портал стоит прокси и оплаченной капчи, и он не должен множиться на
число заинтересованных.

## Почему часть тестов коммитит по-настоящему

Фикстура session (tests/conftest.py) работает во ВНЕШНЕЙ транзакции с откатом: её строки
не видны другим соединениям. Репозиторию этого хватает — он принимает сессию снаружи. Но
HTTP-эндпоинт и Celery-задача открывают СВОЙ session_scope, то есть другое соединение, и
незакоммиченных дел не увидят вовсе. Поэтому такие тесты заводят дело настоящим коммитом
через committed_cases и сами за собой убирают.

Побочный эффект, о котором стоит знать: замещающая семантика означает, что эти тесты
снимают мониторинг со ВСЕХ дел в базе, в которую их запустили. На дев-базе это безвредно
(клиентский сервис вернёт состояние следующим PUT), но по живой базе их гонять нельзя.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, tasks
from app.database import session_scope
from app.main import app
from app.models import Case, Court, CourtLevel
from app.repositories import CaseRepository

pytestmark = pytest.mark.db

UID = "77MS0002-01-2026-000001-01"
COURT_CODE = "77MS0002"


def make_case(session, court, code: str) -> Case:
    """Минимальная карточка в переданной сессии: мониторингу нужны только id и флаг."""
    case = Case(uid=UID, court=court, code=code)
    session.add(case)
    session.flush()
    return case


@contextmanager
def committed_cases(*codes: str):
    """Карточки, реально закоммиченные в базу, и уборка за собой.

    Нужны там, где проверяемый код открывает свою сессию: эндпоинт и ночная задача.
    Суд заводим тем же коммитом и НЕ удаляем — это справочник, и фикстура court
    рассчитывает найти его готовым.
    """
    with session_scope() as session:
        court = session.scalar(select(Court).where(Court.code == COURT_CODE))
        if court is None:
            court = Court(
                code=COURT_CODE,
                name="Судебный участок № 2",
                level=CourtLevel.MIRSUD,
                region="Город Москва",
                timezone="Europe/Moscow",
                base_url="http://mos-sud.ru/ms/2",
            )
            session.add(court)
            session.flush()
        cases = [Case(uid=UID, court=court, code=code) for code in codes]
        session.add_all(cases)
        session.flush()
        ids = [case.id for case in cases]

    try:
        yield ids
    finally:
        with session_scope() as session:
            session.query(Case).filter(Case.id.in_(ids)).delete(
                synchronize_session=False
            )


# ------------------------------------------------ замещение списка мониторинга
def test_ids_from_the_list_get_the_flag(session, court) -> None:
    a = make_case(session, court, "01-0001/2026")
    b = make_case(session, court, "01-0002/2026")

    CaseRepository(session).set_monitoring_list([a.id])

    assert a.is_on_monitoring is True
    assert b.is_on_monitoring is False


def test_ids_outside_the_list_lose_the_flag(session, court) -> None:
    """Замещение, а не добавление: чего в списке нет, то с мониторинга снимается."""
    a = make_case(session, court, "01-0001/2026")
    b = make_case(session, court, "01-0002/2026")
    repo = CaseRepository(session)

    repo.set_monitoring_list([a.id, b.id])
    repo.set_monitoring_list([b.id])

    assert a.is_on_monitoring is False
    assert b.is_on_monitoring is True


def test_repeated_sync_changes_nothing(session, court) -> None:
    """Идемпотентность: тот же список второй раз не переключает ни одной строки.

    Ради этого ручка и сделана PUT с полным состоянием. Ненулевые added/removed на
    повторном вызове означали бы, что каждый запрос клиента гоняет UPDATE по всей
    таблице дел.
    """
    a = make_case(session, court, "01-0001/2026")
    b = make_case(session, court, "01-0002/2026")
    repo = CaseRepository(session)

    first = repo.set_monitoring_list([a.id, b.id])
    second = repo.set_monitoring_list([a.id, b.id])

    assert (first.added, first.removed) == (2, 0)
    assert (second.added, second.removed) == (0, 0)


def test_duplicates_in_the_list_are_collapsed(session, court) -> None:
    """Один id трижды — одно дело на мониторинге.

    Клиент строит список из подписок, и на дело с тремя подписчиками id придёт трижды,
    если он забудет distinct. Считать это тремя делами нельзя.
    """
    case = make_case(session, court, "01-0001/2026")

    result = CaseRepository(session).set_monitoring_list([case.id, case.id, case.id])

    assert result.added == 1


def test_unknown_ids_are_reported_but_do_not_fail_the_sync(session, court) -> None:
    """Дела нет в базе — сообщаем, но остальные всё равно обновляем.

    Одно исчезнувшее дело не повод не поставить на мониторинг остальные двадцать девять;
    промолчать тоже нельзя — у клиента подписка на дело, которого у нас нет.
    """
    case = make_case(session, court, "01-0001/2026")

    result = CaseRepository(session).set_monitoring_list([case.id, 10**9])

    assert result.unknown_ids == [10**9]
    assert case.is_on_monitoring is True


def test_empty_list_unmonitors_everything(session, court) -> None:
    """Пустой список — законное состояние «ни на что больше не подписаны»."""
    case = make_case(session, court, "01-0001/2026")
    repo = CaseRepository(session)
    repo.set_monitoring_list([case.id])

    result = repo.set_monitoring_list([])

    assert case.is_on_monitoring is False
    assert result.monitored == 0


# ------------------------------------------------------------ выборка для обхода
def test_only_monitored_cases_are_selected(session, court) -> None:
    on = make_case(session, court, "01-0001/2026")
    off = make_case(session, court, "01-0002/2026")
    repo = CaseRepository(session)
    repo.set_monitoring_list([on.id])

    selected = repo.list_monitored_ids()

    assert on.id in selected
    assert off.id not in selected


def test_never_checked_cases_come_first(session, court) -> None:
    """Порядок выборки — очередь по давности проверки, а не по id.

    Это страховка от лимита: с сортировкой по id урезанная выборка брала бы одни и те же
    первые N каждые сутки, а хвост списка не обошёлся бы никогда.
    """
    fresh = make_case(session, court, "01-0001/2026")
    stale = make_case(session, court, "01-0002/2026")
    never = make_case(session, court, "01-0003/2026")
    fresh.last_checked_at = dt.datetime(2026, 8, 23, tzinfo=dt.timezone.utc)
    stale.last_checked_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    never.last_checked_at = None
    session.flush()

    repo = CaseRepository(session)
    repo.set_monitoring_list([fresh.id, stale.id, never.id])

    assert repo.list_monitored_ids() == [never.id, stale.id, fresh.id]
    assert repo.list_monitored_ids(limit=1) == [never.id]


# --------------------------------------------------------------- ночной прогон
def test_scheduler_enqueues_each_case_once(monkeypatch) -> None:
    """Каждое дело уходит на обход ровно один раз, с разносом по времени.

    Подменяем постановку в очередь, а не брокер: проверяем решение планировщика, а не
    работу Celery.
    """
    with committed_cases("01-0001/2026", "01-0002/2026") as ids:
        with session_scope() as session:
            CaseRepository(session).set_monitoring_list(ids)

        calls: list[tuple[int, str, int]] = []

        def fake_enqueue(case_id, queue="regular", countdown=0):
            calls.append((case_id, queue, countdown))
            return len(calls)

        monkeypatch.setattr(tasks, "enqueue_case_resync", fake_enqueue)
        monkeypatch.setattr(config, "MONITORING_SPACING_SECONDS", 60)
        monkeypatch.setattr(config, "MONITORING_BATCH_LIMIT", 0)

        report = tasks.sync_monitored_cases()

        assert report == {"selected": 2, "enqueued": 2}
        assert sorted(case_id for case_id, _, _ in calls) == sorted(ids)
        # Разнос обязателен: без него оба дела ушли бы в очередь одновременно и
        # разобрались бы вперегонки, выжигая пул прокси.
        assert sorted(countdown for _, _, countdown in calls) == [0, 60]
        # regular, а не urgent: ночной прогон не вытесняет дела, которые пользователь
        # добавляет прямо сейчас и ждёт на экране.
        assert {queue for _, queue, _ in calls} == {"regular"}


def test_scheduler_ignores_unmonitored_cases(monkeypatch) -> None:
    """Дело без флага в очередь не попадает."""
    with committed_cases("01-0001/2026", "01-0002/2026") as ids:
        monitored, ignored = ids
        with session_scope() as session:
            CaseRepository(session).set_monitoring_list([monitored])

        calls: list[int] = []
        monkeypatch.setattr(
            tasks,
            "enqueue_case_resync",
            lambda case_id, queue="regular", countdown=0: calls.append(case_id) or 1,
        )
        monkeypatch.setattr(config, "MONITORING_BATCH_LIMIT", 0)

        tasks.sync_monitored_cases()

        assert monitored in calls
        assert ignored not in calls


def test_case_with_two_subscribers_is_crawled_once(monkeypatch) -> None:
    """Дело с двумя подписчиками — один обход.

    Подписки живут в клиентском сервисе, и он присылает distinct-список. Здесь это видно
    со стороны core: сколько бы раз id ни пришёл, в очередь дело уходит однажды.
    """
    with committed_cases("01-0001/2026") as (case_id,):
        with session_scope() as session:
            CaseRepository(session).set_monitoring_list([case_id, case_id])

        calls: list[int] = []
        monkeypatch.setattr(
            tasks,
            "enqueue_case_resync",
            lambda cid, queue="regular", countdown=0: calls.append(cid) or 1,
        )
        monkeypatch.setattr(config, "MONITORING_BATCH_LIMIT", 0)

        tasks.sync_monitored_cases()

        assert calls == [case_id]


def test_scheduler_survives_a_failing_case(monkeypatch) -> None:
    """Одно дело не поставилось — остальные всё равно уходят на обход.

    Иначе одно битое дело срывало бы весь ночной прогон, и заметили бы это через сутки.
    Расхождение видно в отчёте: selected больше enqueued.
    """
    with committed_cases("01-0001/2026", "01-0002/2026") as ids:
        with session_scope() as session:
            CaseRepository(session).set_monitoring_list(ids)

        seen: list[int] = []

        def flaky(case_id, queue="regular", countdown=0):
            seen.append(case_id)
            if len(seen) == 1:
                raise RuntimeError("брокер недоступен")
            return 1

        monkeypatch.setattr(tasks, "enqueue_case_resync", flaky)
        monkeypatch.setattr(config, "MONITORING_BATCH_LIMIT", 0)

        report = tasks.sync_monitored_cases()

        assert len(seen) == 2
        assert report == {"selected": 2, "enqueued": 1}


def test_batch_limit_caps_the_run(monkeypatch) -> None:
    """Лимит режет выборку, а не число постановок."""
    with committed_cases("01-0001/2026", "01-0002/2026") as ids:
        with session_scope() as session:
            CaseRepository(session).set_monitoring_list(ids)

        monkeypatch.setattr(
            tasks, "enqueue_case_resync", lambda cid, queue="regular", countdown=0: 1
        )
        monkeypatch.setattr(config, "MONITORING_BATCH_LIMIT", 1)

        assert tasks.sync_monitored_cases() == {"selected": 1, "enqueued": 1}


# ------------------------------------------------------------------ HTTP-ручки
def test_put_monitoring_cases_is_idempotent() -> None:
    """Повторный PUT с тем же телом возвращает added=0, removed=0."""
    with committed_cases("01-0001/2026") as (case_id,):
        with TestClient(app) as client:
            first = client.put("/monitoring/cases", json={"case_ids": [case_id]})
            second = client.put("/monitoring/cases", json={"case_ids": [case_id]})

        assert first.status_code == 200, first.text
        assert first.json()["added"] == 1
        assert second.json() == {
            "monitored": 1,
            "added": 0,
            "removed": 0,
            "unknown_ids": [],
        }


def test_empty_list_needs_force() -> None:
    """Пустой список без force отклоняется, с force — проходит.

    Пустое тело — самая вероятная форма аварии на стороне клиента (упал запрос к своей
    БД, опечатка в фильтре), и от правды её в запросе не отличить. Цена ошибки
    несимметрична: лишний обход стоит одного похода, а снятое зря дело перестаёт
    обновляться МОЛЧА, и никто этого не замечает.
    """
    with committed_cases("01-0001/2026") as (case_id,):
        with TestClient(app) as client:
            client.put("/monitoring/cases", json={"case_ids": [case_id]})

            refused = client.put("/monitoring/cases", json={"case_ids": []})
            forced = client.put(
                "/monitoring/cases", json={"case_ids": []}, params={"force": True}
            )

        assert refused.status_code == 409
        assert forced.status_code == 200
        assert forced.json()["monitored"] == 0


def test_unknown_ids_come_back_in_the_response() -> None:
    """Клиент узнаёт о подписке на несуществующее дело из ответа, а не из логов."""
    with committed_cases("01-0001/2026") as (case_id,):
        with TestClient(app) as client:
            response = client.put(
                "/monitoring/cases", json={"case_ids": [case_id, 10**9]}
            )

        assert response.status_code == 200, response.text
        assert response.json()["unknown_ids"] == [10**9]


def test_summaries_of_several_cases_come_in_one_request() -> None:
    """GET /cases?ids= отдаёт витрины пачкой, отсутствующие id молча пропускает."""
    with committed_cases("01-0001/2026", "01-0002/2026") as ids:
        with TestClient(app) as client:
            response = client.get(
                "/cases", params={"ids": f"{ids[0]},{ids[1]},{10**9}"}
            )
            broken = client.get("/cases", params={"ids": "1,не-число"})
            too_many = client.get(
                "/cases", params={"ids": ",".join(str(n) for n in range(1000))}
            )

        assert response.status_code == 200, response.text
        # Отсутствующего id в ответе нет, и это не 404: список — не карточка, и «одного
        # из тридцати дел уже нет» не причина не показать остальные двадцать девять.
        assert [row["id"] for row in response.json()] == ids
        assert all("court" in row for row in response.json())
        # Мусор в ids — 422, а не короткий список: молчаливый пропуск клиент прочитал бы
        # как «дела удалены».
        assert broken.status_code == 422
        assert too_many.status_code == 422


def test_summary_reports_the_monitoring_flag() -> None:
    """В витрине видно, обходится ли дело: по ней клиент ловит расхождение."""
    with committed_cases("01-0001/2026") as (case_id,):
        with TestClient(app) as client:
            client.put("/monitoring/cases", json={"case_ids": [case_id]})
            row = client.get(f"/cases/{case_id}/summary").json()

        assert row["is_on_monitoring"] is True
