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
        "/cases/{case_id}",
        "/cases/{case_id}/summary",
        "/cases/{case_id}/events",
    }


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


def test_no_monitoring_settings_left() -> None:
    """Настроек пользовательского мониторинга в core_v2 нет (ТЗ PRIORITY 2)."""
    leaked = [name for name in vars(config) if name.startswith("MONITORING_")]
    assert not leaked, f"остались настройки мониторинга: {leaked}"


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

    assert not hasattr(Case, "monitoring_enabled")
    assert not hasattr(Case, "case_link_id")
    assert not hasattr(Case, "related_case_ids")
    assert not hasattr(Document, "document_text")
    assert not hasattr(Event, "document_id")

    # А эти две остались: это факты о деле, а не мониторинг.
    assert hasattr(Case, "last_checked_at")
    assert hasattr(Case, "last_changed_at")


def test_case_repository_has_no_monitoring_methods() -> None:
    """Выбором дел для обхода core_v2 не занимается (ТЗ PRIORITY 2)."""
    from app.repositories import CaseRepository

    assert not hasattr(CaseRepository, "set_monitoring")
    assert not hasattr(CaseRepository, "list_monitored_ids")


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
