"""Админка на SQLAdmin: веб-интерфейс для просмотра и правки записей моделей.

Монтируется на /admin. Вход по логину/паролю из config (env). Для каждой модели —
свой ModelView со списком колонок; add/edit/delete работают из коробки.
"""
from datetime import date, datetime

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import Date, DateTime
from sqlalchemy import inspect as sa_inspect
from starlette.requests import Request

from app.config import ADMIN_PASSWORD, ADMIN_SECRET_KEY, ADMIN_USERNAME
from app.models.database import (
    Case,
    CaseLink,
    Court,
    CourtSession,
    Document,
    Event,
    Instance,
    Judge,
    PlaceHistory,
    SearchTask,
    Side,
    engine,
)


def _format_no_ms(value):
    """Показать дату/время без миллисекунд (None -> пусто)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return value


def _make_dt_formatter(key: str):
    """Собрать форматтер одной колонки. SQLAdmin зовёт его как formatter(obj, prop, request)."""
    def formatter(obj, *args):
        return _format_no_ms(getattr(obj, key))
    return formatter


def _no_ms_formatters(model) -> dict:
    """Собрать форматтеры для всех полей-дат/времени модели (без миллисекунд)."""
    formatters = {}
    # Проходим по колонкам таблицы и берём только Date/DateTime.
    for column in sa_inspect(model).columns:
        if isinstance(column.type, (Date, DateTime)):
            attr = getattr(model, column.key)  # напр. Case.created_at
            formatters[attr] = _make_dt_formatter(column.key)
    return formatters


class AdminAuth(AuthenticationBackend):
    """Простая авторизация в админку по логину/паролю из env."""

    async def login(self, request: Request) -> bool:
        # Проверяем введённые логин/пароль; при успехе кладём метку в сессию.
        form = await request.form()
        if form.get("username") == ADMIN_USERNAME and form.get("password") == ADMIN_PASSWORD:
            request.session.update({"token": "ok"})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        # Выход — очищаем сессию.
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        # Пускаем в админку, только если в сессии есть метка входа.
        return request.session.get("token") == "ok"


class CaseAdmin(ModelView, model=Case):
    """Дела."""

    name = "Дело"
    name_plural = "Дела"
    column_list = [Case.id, Case.uid, Case.application_number, Case.status, Case.created_at]


class CourtAdmin(ModelView, model=Court):
    """Суды (справочник)."""

    name = "Суд"
    name_plural = "Суды"
    column_list = [Court.id, Court.name, Court.zone, Court.parser_type]


class JudgeAdmin(ModelView, model=Judge):
    """Судьи (справочник)."""

    name = "Судья"
    name_plural = "Судьи"
    column_list = [Judge.id, Judge.full_name]


class SideAdmin(ModelView, model=Side):
    """Стороны (справочник)."""

    name = "Сторона"
    name_plural = "Стороны"
    column_list = [Side.id, Side.full_name, Side.type]


class EventAdmin(ModelView, model=Event):
    """События по делу."""

    name = "Событие"
    name_plural = "События"
    column_list = [Event.id, Event.case_id, Event.event_date, Event.state_description]


class PlaceHistoryAdmin(ModelView, model=PlaceHistory):
    """История местонахождения."""

    name = "Местонахождение"
    name_plural = "История местонахождения"
    column_list = [PlaceHistory.id, PlaceHistory.case_id, PlaceHistory.place_date, PlaceHistory.place_description]


class InstanceAdmin(ModelView, model=Instance):
    """Инстанции."""

    name = "Инстанция"
    name_plural = "Инстанции"
    column_list = [Instance.id, Instance.case_id, Instance.instance_number]


class DocumentAdmin(ModelView, model=Document):
    """Документы по делу."""

    name = "Документ"
    name_plural = "Документы"
    column_list = [Document.id, Document.case_id, Document.document_date, Document.document_type]


class CourtSessionAdmin(ModelView, model=CourtSession):
    """Судебные заседания."""

    name = "Заседание"
    name_plural = "Заседания"
    column_list = [CourtSession.id, CourtSession.case_id, CourtSession.session_date, CourtSession.stage]


class CaseLinkAdmin(ModelView, model=CaseLink):
    """Группы связанных дел."""

    name = "Группа связей"
    name_plural = "Группы связей"
    column_list = [CaseLink.id, CaseLink.created_at]


class SearchTaskAdmin(ModelView, model=SearchTask):
    """Задачи поиска/синхронизации дел."""

    name = "Задача поиска"
    name_plural = "Задачи поиска"
    column_list = [SearchTask.id, SearchTask.uid, SearchTask.status, SearchTask.attempts, SearchTask.case_id]


def setup_admin(app) -> Admin:
    """Создать админку, повесить её на app (/admin) и зарегистрировать все модели."""
    # Авторизация по логину/паролю; secret_key нужен для cookie-сессии.
    authentication_backend = AdminAuth(secret_key=ADMIN_SECRET_KEY)
    admin = Admin(app, engine, authentication_backend=authentication_backend)

    # Регистрируем вьюхи всех моделей.
    for view in (
        CaseAdmin,
        CourtAdmin,
        JudgeAdmin,
        SideAdmin,
        EventAdmin,
        PlaceHistoryAdmin,
        InstanceAdmin,
        DocumentAdmin,
        CourtSessionAdmin,
        CaseLinkAdmin,
        SearchTaskAdmin,
    ):
        # Все поля-даты/время показываем без миллисекунд (в списке и в карточке).
        view.column_formatters = _no_ms_formatters(view.model)
        view.column_formatters_detail = _no_ms_formatters(view.model)
        admin.add_view(view)

    return admin
