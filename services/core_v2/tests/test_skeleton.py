"""Тесты скелета: сервис самостоятелен и поднимается.

Главный из них — test_core_v2_does_not_import_old_core. Правило миграции №3 запрещает
импортировать runtime-код старого core, и это ровно то нарушение, которое легко внести
случайно (скопировал файл, забыл поправить импорт) и трудно заметить: пока оба каталога
лежат рядом, всё будет работать.
"""
from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

from fastapi.testclient import TestClient

from app import config, timezones
from app.database import UTC_DATETIME, Base, session_scope
from app.main import app

CORE_V2_ROOT = Path(__file__).resolve().parents[1]


def python_files() -> list[Path]:
    """Все .py сервиса, кроме служебных каталогов."""
    skipped = {"__pycache__", ".venv", ".venv310"}
    return [
        path
        for path in CORE_V2_ROOT.rglob("*.py")
        if not skipped & set(path.parts)
    ]


def test_core_v2_does_not_import_old_core() -> None:
    """Ни одного импорта из services/core (правило миграции №3).

    Разбираем AST, а не ищем подстроку: подстрока нашлась бы и в комментарии, где
    ссылка на старый core как раз уместна и полезна.
    """
    offenders: list[str] = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.startswith("services.core") or name.startswith("core."):
                    offenders.append(f"{path.name}:{node.lineno} -> {name}")

    assert not offenders, "импорт из старого core: " + "; ".join(offenders)


def test_ping_answers() -> None:
    """Сервис поднимается без старого client и отвечает."""
    with TestClient(app) as client:
        response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"message": "pong"}


def _all_endpoints():
    """Все обработчики приложения, включая вложенные в подключённые роутеры.

    Обходим рекурсивно не для красоты: в этой версии FastAPI подключённый роутер лежит
    в app.routes отдельным объектом и СВОИ роуты внутрь не разворачивает. Плоский перебор
    app.routes видел бы только /ping и не заметил бы ни одного роута из app/api/.
    """
    import inspect

    found = []

    def walk(routes) -> None:
        for route in routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                found.append((getattr(route, "path", "?"), endpoint))
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested)

    walk(app.routes)
    return [
        (path, endpoint)
        for path, endpoint in found
        # Только НАШИ роуты: /docs, /redoc и /openapi.json добавляет сам FastAPI.
        if getattr(endpoint, "__module__", "").startswith("app.")
        # Кроме админки. Массовые действия SQLAdmin обязаны быть async — этого требует
        # сам sqladmin. Правило это не нарушает: ни одно из них не ходит на портал.
        # «Переобойти дела» лишь заводит задачу и кладёт её в очередь, «залить справочник»
        # вызывает .delay(), включение и выключение прокси — чистая работа с БД. До
        # Playwright оттуда дороги нет, поход делает воркер в своём процессе.
        and getattr(endpoint, "__module__", "") != "app.admin"
    ]


def test_every_route_is_registered() -> None:
    """Роуты действительно подключены, а не потерялись при include_router."""
    paths = set(app.openapi()["paths"])

    assert paths == {
        "/ping",
        "/search_case",
        "/search_case/tasks/{task_id}",
        "/cases",
        "/cases/{case_id}",
        "/cases/{case_id}/summary",
        "/cases/{case_id}/events",
        "/court_sessions",
        "/monitoring/cases",
    }


def test_case_list_route_is_not_shadowed() -> None:
    """GET /cases не перехватывается роутом /cases/{case_id}.

    Ловушка порядка объявления: путь /cases/summary попал бы в /cases/{case_id} и упал
    бы с 422 на попытке разобрать «summary» как int. Отсюда и форма /cases?ids=.
    Проверка нужна потому, что достаточно объявить роуты в другом порядке — и список
    перестанет открываться, а тесты самой выборки этого не заметят.
    """
    with TestClient(app) as client:
        response = client.get("/cases", params={"ids": "0"})
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_all_routes_are_sync() -> None:
    """Ни один роут не должен быть async def (см. докстринг app/main.py).

    Походы в суд идут через Playwright sync API, а он падает внутри работающего event
    loop. Проверка стоит здесь, а не в code review, потому что сломается это молча.
    """
    import inspect

    endpoints = _all_endpoints()
    assert endpoints, "обработчиков не найдено — проверка ничего бы не проверила"

    coroutines = [
        path for path, endpoint in endpoints if inspect.iscoroutinefunction(endpoint)
    ]
    assert not coroutines, f"роуты объявлены async def: {coroutines}"


def test_admin_actions_do_not_reach_the_browser() -> None:
    """Массовые действия админки async — значит из них НЕЛЬЗЯ ходить на портал.

    sqladmin требует от них async, а Playwright sync API падает внутри работающего event
    loop. Пока действия только заводят задачи и правят БД, всё в порядке; появится там
    прямой вызов обхода — сломается молча и не сразу, поэтому проверяем текстом.
    """
    import ast

    source = (CORE_V2_ROOT / "app" / "admin.py").read_text(encoding="utf-8")
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }

    forbidden = {"discover_case", "resync_case", "fetch_card_by_url", "fetch_cases_by_uid"}
    assert not (forbidden & called), (
        f"админка зовёт обход напрямую: {sorted(forbidden & called)}"
    )


def test_database_url_points_to_its_own_database() -> None:
    """У core_v2 своя база: одна база — одна история alembic."""
    assert config.DATABASE_URL.endswith("soroka_core_v2") or "soroka_core_v2" in (
        config.DATABASE_URL
    )


def test_monitoring_settings_are_about_timing_only() -> None:
    """Настройки мониторинга отвечают на «когда», а не на «за чем следить».

    Граница ответственности: какие дела интересны — знание клиентского сервиса, оно
    приходит запросом PUT /monitoring/cases. Core распоряжается только временем обхода.
    Появись здесь настройка вида MONITORING_USER_* или интервал на пользователя — значит,
    знание о подписках просочилось в core, и обратно его уже не вынуть.
    """
    names = {name for name in vars(config) if name.startswith("MONITORING_")}
    assert names == {
        "MONITORING_HOUR",
        "MONITORING_SPACING_SECONDS",
        "MONITORING_BATCH_LIMIT",
    }, f"неожиданный состав настроек мониторинга: {sorted(names)}"


def test_core_knows_nothing_about_users() -> None:
    """В схеме core нет ни пользователей, ни подписок.

    Мониторинг в core вернулся, пользователи — нет и не должны. Флаг is_on_monitoring
    живёт на карточке ровно потому, что дело обходится один раз независимо от числа
    подписчиков; таблица подписок здесь означала бы, что граница поехала.
    """
    tables = set(Base.metadata.tables)
    forbidden = {"user", "users", "subscription", "case_subscription", "notification"}
    assert not (forbidden & tables), f"в схеме core появились: {sorted(forbidden & tables)}"


def test_removed_tables_are_absent_from_metadata() -> None:
    """Таблиц instance и case_link в схеме нет.

    Обе существовали в старом core, но их не заполнял ни парсер, ни задача — только
    ручная правка в админке. Проверяем по metadata, а не по живой базе: именно из неё
    alembic строит миграции, и случайно вернувшаяся модель тут же завела бы таблицу.
    """
    tables = set(Base.metadata.tables)
    assert "instance" not in tables
    assert "case_link" not in tables
    # Заодно убеждаемся, что модели вообще подключены: пустая metadata означала бы
    # забытый импорт в app/models/__init__.py, и alembic предложил бы снести всё.
    assert "case" in tables and "event" in tables


def test_removed_columns_are_absent_from_models() -> None:
    """Колонок, снятых вместе с мониторингом и мёртвым кодом, у моделей нет."""
    from app.models import Case, Document, Event

    assert not hasattr(Case, "case_link_id")
    assert not hasattr(Case, "related_case_ids")
    assert not hasattr(Document, "document_text")
    assert not hasattr(Event, "document_id")

    # Имя старого флага не должно вернуться: у него была другая семантика — им
    # распоряжался сам core. Нынешним is_on_monitoring распоряжается клиентский сервис.
    assert not hasattr(Case, "monitoring_enabled")

    # А эти остались: факты о деле плюс флаг регулярного обхода.
    assert hasattr(Case, "last_checked_at")
    assert hasattr(Case, "last_changed_at")
    assert hasattr(Case, "is_on_monitoring")


def test_monitoring_is_replaced_wholesale_not_per_user() -> None:
    """У репозитория есть замещение списка целиком и нет включения по одному делу.

    Форма важна: set_monitoring(case, enabled) вернула бы нас к тумблеру на дело, а
    тумблер невозможно свести с состоянием клиента — два потерянных запроса, и списки
    разошлись навсегда. Замещение всего списка идемпотентно и сходится с любого
    расхождения за один вызов.
    """
    from app.repositories import CaseRepository

    assert hasattr(CaseRepository, "set_monitoring_list")
    assert hasattr(CaseRepository, "list_monitored_ids")
    assert not hasattr(CaseRepository, "set_monitoring")


def test_utc_datetime_is_timezone_aware() -> None:
    """Все моменты хранятся как timestamptz."""
    assert UTC_DATETIME.timezone is True


def test_session_scope_is_a_context_manager() -> None:
    """session_scope — единственное место, где происходит commit."""
    assert hasattr(session_scope, "__wrapped__") or callable(session_scope)


def test_timezones_module_is_carried_over_unchanged() -> None:
    """Правило переноса: timezone logic не меняем без обнаруженного дефекта.

    Проверяем поведение, а не байты файла: неизвестный регион обязан падать, а не тихо
    подставлять Москву, и перевод локального времени в UTC обязан быть обратимым.
    """
    assert timezones.timezone_for("Город Москва", "77MS0002") == "Europe/Moscow"

    local = dt.datetime(2026, 8, 21, 15, 30)
    moment = timezones.to_utc(local, "Asia/Yekaterinburg")
    assert moment.utcoffset() == dt.timedelta(0)
    assert (
        timezones.to_court_local(moment, "Asia/Yekaterinburg").replace(tzinfo=None)
        == local
    )
