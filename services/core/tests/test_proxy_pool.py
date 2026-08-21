"""Пул прокси: ротация по LRU и запрет ходить на портал напрямую.

Тесты идут на настоящем Postgres (фикстура session из conftest.py) — запрос аренды
использует FOR UPDATE ... SKIP LOCKED, которого на SQLite нет.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.browser import proxy as proxy_module
from app.browser.proxy import ProxySettings, ProxyUnavailable, lease_proxy, parse_proxy_url
from app.models.database import Proxy
from app.repositories import ProxyRepository


@pytest.fixture(autouse=True)
def empty_pool(session):
    """Убрать из выдачи боевые прокси: тест должен видеть только то, что завёл сам.

    Удаление живёт внутри тестовой транзакции и откатывается вместе с ней, так что
    настоящий пул в БД не страдает.
    """
    session.execute(delete(Proxy))
    session.flush()


def _make(
    session, host: str, *, enabled: bool = True, last_used_at=None, portals=None
) -> Proxy:
    """Завести прокси в пуле. Порт выводим из хоста, чтобы не задавать его в каждом тесте."""
    proxy = Proxy(
        scheme="http",
        host=host,
        port=8000,
        username="user",
        password="secret",
        enabled=enabled,
        last_used_at=last_used_at,
        portals=portals if portals is not None else [],
    )
    session.add(proxy)
    session.flush()
    return proxy


# ------------------------------------------------------------------ ротация (LRU)
def test_lease_prefers_never_used(session) -> None:
    """Прокси, которым ещё не ходили (last_used_at IS NULL), выдаётся первым."""
    _make(session, "10.0.0.1", last_used_at=datetime(2026, 1, 1))
    fresh = _make(session, "10.0.0.2", last_used_at=None)

    assert ProxyRepository(session).lease() is fresh


def test_lease_prefers_oldest_used(session) -> None:
    """Из использованных выдаётся тот, которым ходили давнее всех."""
    now = datetime.now(timezone.utc)
    older = _make(session, "10.0.0.1", last_used_at=now - timedelta(hours=2))
    _make(session, "10.0.0.2", last_used_at=now - timedelta(minutes=5))

    assert ProxyRepository(session).lease() is older


def test_lease_rotates(session) -> None:
    """Аренда отмечает прокси использованным, поэтому следующий вызов даёт другой.

    Это и есть ротация: без отметки времени пул выдавал бы один и тот же адрес.
    """
    repo = ProxyRepository(session)
    first = _make(session, "10.0.0.1")
    second = _make(session, "10.0.0.2")

    leased = [repo.lease(), repo.lease()]

    assert {p.id for p in leased} == {first.id, second.id}
    assert all(p.last_used_at is not None for p in leased)


def test_lease_skips_disabled(session) -> None:
    """Выключенный прокси в выдачу не попадает — это ручной выключатель из админки."""
    _make(session, "10.0.0.1", enabled=False)

    assert ProxyRepository(session).lease() is None


def test_lease_returns_none_on_empty_pool(session) -> None:
    """Пустой пул — это None, а не ошибка: решение принимает вызывающий код."""
    assert ProxyRepository(session).lease() is None


# ------------------------------------------------- выбор прокси под портал (portals)
# Годность у адресов разная: провайдер режет CONNECT выборочно, и прокси, берущий
# mos-sud, до msudrf может не дойти вовсе. Раньше это лечили закреплением одного адреса
# за движком msudrf прямо в коде клиента, теперь выбирает пул.
OLD = datetime(2026, 1, 1)
NEW = datetime(2026, 8, 1)


def test_lease_skips_a_proxy_that_does_not_reach_the_portal(session) -> None:
    """Проверенный и НЕ подходящий адрес не выдаётся, даже если он давно не использован."""
    _make(session, "10.0.0.1", portals=["mos-sud"], last_used_at=OLD)
    good = _make(session, "10.0.0.2", portals=["msudrf"], last_used_at=NEW)

    assert ProxyRepository(session).lease(portal="msudrf") is good


def test_lease_prefers_a_checked_proxy_over_an_unchecked_one(session) -> None:
    """Непроверенный (portals пуст) идёт последним, даже будучи самым давним по LRU.

    Пустой список значит «годность не измеряли»: попробовать такой можно, но только
    когда заведомо годного нет.
    """
    _make(session, "10.0.0.1", portals=[], last_used_at=OLD)
    good = _make(session, "10.0.0.2", portals=["msudrf"], last_used_at=NEW)

    assert ProxyRepository(session).lease(portal="msudrf") is good


def test_unchecked_proxy_is_used_when_nothing_else_fits(session) -> None:
    """Заведомо годного нет — берём непроверенный, а не отказываемся идти вовсе.

    Иначе адрес, заведённый без --sites, не использовался бы никогда и молча пропадал
    из пула.
    """
    _make(session, "10.0.0.1", portals=["mos-sud"], last_used_at=OLD)
    unchecked = _make(session, "10.0.0.2", portals=[], last_used_at=NEW)

    assert ProxyRepository(session).lease(portal="msudrf") is unchecked


def test_lease_returns_none_when_no_proxy_reaches_the_portal(session) -> None:
    """Все адреса проверены и ни один не годится — выдавать нечего."""
    _make(session, "10.0.0.1", portals=["mos-sud"])
    _make(session, "10.0.0.2", portals=["spb"])

    assert ProxyRepository(session).lease(portal="msudrf") is None


def test_lease_without_portal_ignores_the_field(session) -> None:
    """Портал не определён (суд не поддержан) — фильтра нет, работает обычный LRU."""
    oldest = _make(session, "10.0.0.1", portals=["spb"], last_used_at=OLD)
    _make(session, "10.0.0.2", portals=["msudrf"], last_used_at=NEW)

    assert ProxyRepository(session).lease() is oldest


# --------------------------------------------------- переключение пачкой из админки
def test_set_enabled_switches_selected(session) -> None:
    """Кнопки «Включить»/«Выключить» в списке админки меняют только отмеченные строки."""
    repo = ProxyRepository(session)
    first = _make(session, "10.0.0.1", enabled=False)
    second = _make(session, "10.0.0.2", enabled=False)
    untouched = _make(session, "10.0.0.3", enabled=False)

    changed = repo.set_enabled([first.id, second.id], True)
    session.expire_all()

    assert changed == 2
    assert session.get(Proxy, first.id).enabled is True
    assert session.get(Proxy, second.id).enabled is True
    assert session.get(Proxy, untouched.id).enabled is False


def test_set_enabled_can_switch_off(session) -> None:
    """Выключенный прокси выпадает из ротации — это и есть ручной выключатель."""
    repo = ProxyRepository(session)
    proxy = _make(session, "10.0.0.1", enabled=True)

    repo.set_enabled([proxy.id], False)
    session.expire_all()

    assert repo.lease() is None


def test_set_enabled_ignores_empty_selection(session) -> None:
    """Нажали кнопку, ничего не отметив — просто ничего не происходит, без ошибки."""
    assert ProxyRepository(session).set_enabled([], True) == 0


# ------------------------------------------- запрет ходить на портал мимо прокси
def test_lease_proxy_raises_when_required(monkeypatch) -> None:
    """Пул пуст при COURT_PROXY_REQUIRED=1 → ProxyUnavailable, браузер не запускается.

    Защита от похода на портал не с того IP: лучше провалить задачу, чем засветить
    адрес воркера.
    """
    monkeypatch.setattr(proxy_module, "COURT_PROXY_REQUIRED", True)
    monkeypatch.setattr(ProxyRepository, "lease", lambda self, portal=None: None)

    with pytest.raises(ProxyUnavailable):
        lease_proxy()


def test_lease_proxy_allows_direct_when_not_required(monkeypatch) -> None:
    """При COURT_PROXY_REQUIRED=0 пустой пул означает «идём напрямую»."""
    monkeypatch.setattr(proxy_module, "COURT_PROXY_REQUIRED", False)
    monkeypatch.setattr(ProxyRepository, "lease", lambda self, portal=None: None)

    assert lease_proxy() is None


# ---------------------------------------------------------------- ProxySettings
def test_str_hides_password() -> None:
    """В логи уходит str(proxy) — пароля в нём быть не должно."""
    assert "secret" not in str(ProxySettings("http", "10.0.0.1", 7584, "user", "secret"))


def test_parse_proxy_url() -> None:
    """Строку от провайдера разбираем в ProxySettings как есть."""
    assert parse_proxy_url("http://user:secret@10.0.0.1:7584") == ProxySettings(
        scheme="http", host="10.0.0.1", port=7584, username="user", password="secret",
    )


def test_parse_proxy_url_requires_port() -> None:
    """Без порта строка бессмысленна — падаем сразу, а не в браузере."""
    with pytest.raises(ValueError):
        parse_proxy_url("http://10.0.0.1")
