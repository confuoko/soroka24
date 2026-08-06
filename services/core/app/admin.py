"""Админка на SQLAdmin: веб-интерфейс для просмотра и правки записей моделей.

Монтируется на /admin. Вход по логину/паролю из config (env). Для каждой модели —
свой ModelView со списком колонок; add/edit/delete работают из коробки.
"""
from datetime import date, datetime

from sqladmin import Admin, ModelView, action
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import Date, DateTime
from sqlalchemy import inspect as sa_inspect
from starlette.requests import Request
from starlette.responses import RedirectResponse
from wtforms import BooleanField

from app.config import ADMIN_PASSWORD, ADMIN_SECRET_KEY, ADMIN_USERNAME
from app.courts.tasks import sync_courts_from_json
from app.models.database import (
    Case,
    CaseLink,
    Court,
    CourtSession,
    Document,
    CaseUrl,
    Event,
    Instance,
    Judge,
    PlaceHistory,
    Proxy,
    SearchTask,
    Side,
    engine,
    session_scope,
)
from app.monitoring.tasks import enqueue_case_resync
from app.repositories import ProxyRepository


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
    # Суд в списке: карточка — это пара «УИД + суд», по одному УИД строк может быть несколько.
    column_list = [Case.id, Case.uid, Case.court, Case.application_number, Case.status, Case.created_at]
    # Историю парсингов (diff_history) SQLAdmin покажет в карточке дела сам —
    # column_details_list по умолчанию включает все колонки модели.

    @action(
        name="resync_cases",
        label="Спарсить заново",
        confirmation_message="Поставить выбранные дела в очередь на повторный парсинг?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def resync_cases(self, request: Request) -> RedirectResponse:
        """Поставить каждое выбранное дело на повторный парсинг.

        Нужна, чтобы наполнять историю diff'ов: обычный POST /search_case для уже
        известного дела возвращает его id и парсинг не запускает.
        """
        pks = [pk for pk in request.query_params.get("pks", "").split(",") if pk]

        # enqueue_case_resync сам создаёт SearchTask по УИД дела и ставит задачу
        # в очередь regular; несуществующие id он молча пропускает (вернёт None).
        for pk in pks:
            enqueue_case_resync(int(pk))

        return RedirectResponse(request.url_for("admin:list", identity=self.identity), status_code=302)


class CaseUrlAdmin(ModelView, model=CaseUrl):
    """Адреса карточек дел.

    Полезно, когда портал переехал: старую ссылку видно, новую можно добавить руками,
    не дожидаясь, пока её кто-нибудь пришлёт.
    """

    name = "Ссылка на дело"
    name_plural = "Ссылки на дела"
    column_list = [CaseUrl.id, CaseUrl.case_id, CaseUrl.url, CaseUrl.last_success_at]
    column_searchable_list = [CaseUrl.url]


class CourtAdmin(ModelView, model=Court):
    """Суды (справочник)."""

    name = "Суд"
    name_plural = "Суды"
    column_list = [Court.id, Court.code, Court.name, Court.level, Court.region]
    column_searchable_list = [Court.code, Court.name, Court.region]

    @action(
        name="sync_courts_json",
        label="Залить суды из courts.json",
        confirmation_message=(
            "Залить/обновить справочник судов из data/courts.json (~7700 записей)? "
            "Существующие суды будут обновлены по коду, отсутствующие в файле не удаляются."
        ),
        add_in_detail=False,
        add_in_list=True,
    )
    async def sync_courts_json(self, request: Request) -> RedirectResponse:
        """Поставить в очередь заливку справочника судов из JSON.

        Команда глобальная — выбранные галочками строки не важны. Через Celery, а не
        напрямую: 7700 записей в одном запросе заблокировали бы event loop uvicorn.
        """
        sync_courts_from_json.apply_async(queue="regular")
        return RedirectResponse(request.url_for("admin:list", identity=self.identity), status_code=302)


class JudgeAdmin(ModelView, model=Judge):
    """Судьи (справочник)."""

    name = "Судья"
    name_plural = "Судьи"
    column_list = [Judge.id, Judge.full_name]


class SideAdmin(ModelView, model=Side):
    """Стороны (справочник)."""

    name = "Сторона"
    name_plural = "Стороны"
    column_list = [Side.id, Side.full_name, Side.role, Side.type]


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
    # Результат показываем в списке: у будущего заседания он пуст и заполняется позже —
    # по нему сразу видно, прошло заседание или ещё нет.
    column_list = [
        CourtSession.id,
        CourtSession.case_id,
        CourtSession.session_date,
        CourtSession.stage,
        CourtSession.result,
    ]


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


class ProxyAdmin(ModelView, model=Proxy):
    """Пул прокси, через которые браузер ходит на портал суда.

    Это штатное место, чтобы завести новый прокси или выключить протухший: снятая
    галка enabled сразу убирает прокси из ротации, передеплой не нужен.
    """

    name = "Прокси"
    name_plural = "Прокси"
    column_list = [
        Proxy.id,
        Proxy.scheme,
        Proxy.host,
        Proxy.port,
        Proxy.enabled,
        Proxy.last_used_at,
        Proxy.comment,
    ]
    column_searchable_list = [Proxy.host, Proxy.comment]
    # Пароль не показываем ни в списке, ни в карточке — а в форме он есть,
    # иначе прокси с авторизацией не завести.
    column_details_exclude_list = [Proxy.password]

    # Обходим несовместимость библиотек: BooleanInputWidget из sqladmin 0.28 наследует
    # wtforms.widgets.Input, но не объявляет validation_attrs, а в WTForms 3.2 этот
    # атрибут убрали с базового класса — и форма падает с AttributeError на любом
    # булевом поле. Подставляем штатный BooleanField: у его CheckboxInput нужный
    # атрибут есть. Выглядит как обычная галка вместо стилизованного переключателя.
    # Появится ещё одна булева колонка в другой модели — ей понадобится то же самое.
    form_overrides = {"enabled": BooleanField}

    # Свой конвертер булевых полей sqladmin ставит галке класс form-check-input, но
    # form_overrides его обходит, и поле попадает в общую ветку с form-control —
    # с ним чекбокс растягивается в пустой прямоугольник вместо галки. Возвращаем
    # нужный класс руками.
    form_widget_args = {"enabled": {"class": "form-check-input"}}

    @action(
        name="enable_proxies",
        label="Включить",
        add_in_detail=True,
        add_in_list=True,
    )
    async def enable_proxies(self, request: Request) -> RedirectResponse:
        """Вернуть выбранные прокси в ротацию прямо из списка."""
        return self._switch(request, enabled=True)

    @action(
        name="disable_proxies",
        label="Выключить",
        add_in_detail=True,
        add_in_list=True,
    )
    async def disable_proxies(self, request: Request) -> RedirectResponse:
        """Убрать выбранные прокси из ротации прямо из списка."""
        return self._switch(request, enabled=False)

    def _switch(self, request: Request, enabled: bool) -> RedirectResponse:
        """Переключить галку у отмеченных строк и вернуться к списку.

        Кнопки в списке — основной способ управлять пулом: какой прокси доходит до
        какого портала, выясняется опытным путём, и переключать их приходится часто.
        """
        pks = [int(pk) for pk in request.query_params.get("pks", "").split(",") if pk]
        with session_scope() as session:
            ProxyRepository(session).set_enabled(pks, enabled)

        return RedirectResponse(
            request.url_for("admin:list", identity=self.identity), status_code=302
        )


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
        CaseUrlAdmin,
        CaseLinkAdmin,
        SearchTaskAdmin,
        ProxyAdmin,
    ):
        # Все поля-даты/время показываем без миллисекунд (в списке и в карточке).
        view.column_formatters = _no_ms_formatters(view.model)
        view.column_formatters_detail = _no_ms_formatters(view.model)
        admin.add_view(view)

    return admin
