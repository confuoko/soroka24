"""Формы админки должны собираться и рисоваться.

Регресс на живую поломку: страницы /admin/proxy/edit и /admin/proxy/create отдавали
500 с AttributeError: 'BooleanInputWidget' object has no attribute 'validation_attrs'.
Причина — несовместимость библиотек: BooleanInputWidget из sqladmin 0.28 наследует
wtforms.widgets.Input, но не объявляет validation_attrs, а WTForms 3.2 убрал этот
атрибут с базового класса. Вылезло на Proxy.enabled — первом булевом поле в проекте.

Проверяем не «настройка выставлена», а то, что поле реально РИСУЕТСЯ: именно рисование
и падало. Так тест поймает поломку и от следующего обновления библиотек.
"""
import asyncio

import pytest

from app.admin import ProxyAdmin
from app.models import Proxy
from app.database import SessionLocal


def _build_form(view_cls):
    """Собрать форму модели так же, как это делает админка.

    session_maker обычно проставляет Admin.add_view при регистрации вью; здесь админку
    целиком не поднимаем, поэтому задаём его сами.
    """
    view = view_cls()
    view.session_maker = SessionLocal
    view.is_async = False
    return asyncio.run(view.scaffold_form())()


def test_boolean_field_renders() -> None:
    """Галка enabled рисуется — на ней и падала форма прокси."""
    html = str(_build_form(ProxyAdmin).enabled())

    assert 'name="enabled"' in html
    assert 'type="checkbox"' in html


def test_boolean_field_looks_like_a_checkbox() -> None:
    """У галки класс form-check-input, а не form-control.

    Регресс: с form-control чекбокс растягивается в пустой прямоугольник, и поле
    выглядит нередактируемым — именно так эта поломка и проявилась.
    """
    html = str(_build_form(ProxyAdmin).enabled())

    assert "form-check-input" in html
    assert "form-control" not in html


def test_list_has_switch_actions() -> None:
    """Включать и выключать прокси можно прямо из списка, не открывая карточку."""
    actions = {name for name in dir(ProxyAdmin) if name.endswith("_proxies")}

    assert {"enable_proxies", "disable_proxies"} <= actions


def test_whole_proxy_form_renders() -> None:
    """Рисуются все поля формы, а не только булево: 500 отдавала страница целиком."""
    for field in _build_form(ProxyAdmin):
        assert str(field())


@pytest.mark.parametrize("column", ["host", "port", "scheme", "username", "password"])
def test_editable_columns_present(column) -> None:
    """Прокси нельзя завести, если в форме нет его адреса и учётных данных."""
    assert column in _build_form(ProxyAdmin)._fields


def test_password_is_hidden_from_details() -> None:
    """Пароль не показываем в карточке — она открыта всем, у кого есть доступ в админку."""
    assert Proxy.password in ProxyAdmin.column_details_exclude_list
