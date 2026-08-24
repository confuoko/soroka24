"""Страницы: вход, список, добавление, ожидание, карточка, отписка.

core здесь подменён целиком: живого backend'а тестам не нужно, а нужно, чтобы каждая
ветка ответа `POST /search_case` приводила к правильному состоянию. Патчим модуль
`cases.integrations.core` там, где его ИМПОРТИРОВАЛИ (в views и в monitoring), — иначе
подмена не сработает.

Два теста стоят особняком.

test_core_outage_does_not_break_the_list — про решение, а не про удобство: список подписок
наш, и показывать его надо даже когда core молчит. Пустая страница выглядела бы как «дела
пропали».

test_another_users_case_is_not_visible — DetailView построен над подпиской именно поэтому:
проверка прав достаётся бесплатно, и забыть её нельзя.
"""
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from cases.integrations.core import CoreUnavailable
from cases.models import CaseSubscription, PendingCaseSearch

pytestmark = pytest.mark.django_db


@pytest.fixture
def natasha(client):
    user = User.objects.create_user("natasha", password="secret123")
    client.login(username="natasha", password="secret123")
    return user


@pytest.fixture(autouse=True)
def quiet_monitoring():
    """Синхронизацию мониторинга по умолчанию не пускаем в сеть.

    autouse, потому что её зовёт почти каждое действие с подпиской, и незаглушенная она
    в каждом тесте пыталась бы достучаться до core.
    """
    with patch("cases.monitoring.core.replace_monitored_cases") as mock:
        mock.return_value = {
            "monitored": 0, "added": 0, "removed": 0, "unknown_ids": []
        }
        yield mock


SUMMARY = {
    "id": 481,
    "uid": "77MS0466-01-2026-003751-93",
    "code": "05-0444/1/2026",
    "status": "Рассмотрено",
    "last_checked_at": "2026-08-23T03:04:11Z",
    "last_changed_at": "2026-05-21T03:07:52Z",
    "updated_at": "2026-08-23T03:04:11Z",
    "is_on_monitoring": True,
    "court": {"id": 12, "code": "77MS0466", "name": "Судебный участок № 463",
              "level": "mirsud", "region": "Москва", "base_url": "https://x"},
}


# ------------------------------------------------------------------------ вход
def test_pages_require_login(client) -> None:
    """Без входа страницы не отдаются — уводят на логин."""
    for name, kwargs in (
        ("my-cases", {}),
        ("add-case", {}),
        ("case-detail", {"core_case_id": 481}),
    ):
        response = client.get(reverse(name, kwargs=kwargs))
        assert response.status_code == 302
        assert reverse("login") in response["Location"]


def test_login_page_uses_the_standard_view(client) -> None:
    """Логин — готовая LoginView из django.contrib.auth.urls."""
    assert client.get(reverse("login")).status_code == 200


# -------------------------------------------------------------------- мои дела
def test_my_cases_shows_subscriptions_with_summaries(client, natasha) -> None:
    CaseSubscription.objects.create(user=natasha, core_case_id=481)

    with patch("cases.views.core.summaries_by_id", return_value={481: SUMMARY}) as mock:
        response = client.get(reverse("my-cases"))

    assert response.status_code == 200
    assert "05-0444/1/2026" in response.content.decode()
    # Один запрос на всю страницу, а не по одному на дело.
    assert mock.call_count == 1


def test_my_cases_asks_core_once_for_many_cases(client, natasha) -> None:
    """Тридцать подписок — один запрос к core, а не тридцать.

    Ровно ради этого в core добавлена ручка GET /cases?ids=. Регресс здесь не сломал бы
    ничего видимого — страница просто стала бы линейно медленнее с числом подписок.
    """
    for case_id in range(1, 31):
        CaseSubscription.objects.create(user=natasha, core_case_id=case_id)

    with patch("cases.views.core.summaries_by_id", return_value={}) as mock:
        client.get(reverse("my-cases"))

    assert mock.call_count == 1
    requested = sorted(mock.call_args.args[0])
    assert requested == list(range(1, 31))


def test_core_outage_does_not_break_the_list(client, natasha) -> None:
    """core молчит — список подписок всё равно показан, и об этом сказано вслух."""
    CaseSubscription.objects.create(user=natasha, core_case_id=481)

    with patch("cases.views.core.summaries_by_id", side_effect=CoreUnavailable("нет")):
        response = client.get(reverse("my-cases"))

    assert response.status_code == 200
    assert response.context["core_unavailable"] is True
    # Дело в списке есть, хоть и без номера.
    assert len(response.context["rows"]) == 1


def test_missing_case_is_shown_as_missing(client, natasha) -> None:
    """core на связи, но дела не отдал — значит, его там больше нет."""
    CaseSubscription.objects.create(user=natasha, core_case_id=481)

    with patch("cases.views.core.summaries_by_id", return_value={}):
        response = client.get(reverse("my-cases"))

    assert response.context["rows"][0]["summary"] is None
    assert "дела нет в сервисе" in response.content.decode()


def test_pending_searches_are_visible_in_the_list(client, natasha) -> None:
    """Ищущееся дело видно на главной.

    Иначе между «добавил» и «нашли» дело не существует на экране вовсе, и человек решает,
    что добавление не сработало.
    """
    PendingCaseSearch.objects.create(
        user=natasha, query="https://mos-sud.ru/x", core_task_id=77
    )

    with patch("cases.views.core.summaries_by_id", return_value={}):
        response = client.get(reverse("my-cases"))

    assert len(response.context["pending"]) == 1
    assert "mos-sud.ru" in response.content.decode()


def test_other_users_subscriptions_are_not_listed(client, natasha) -> None:
    anna = User.objects.create_user("anna", password="x")
    CaseSubscription.objects.create(user=anna, core_case_id=999)

    with patch("cases.views.core.summaries_by_id", return_value={}):
        response = client.get(reverse("my-cases"))

    assert list(response.context["subscriptions"]) == []


# --------------------------------------------------------------- добавить дело
def test_existing_case_subscribes_immediately(client, natasha) -> None:
    """status=exists — ждать нечего, подписываем и ведём на карточку."""
    answer = {"status": "exists", "case_id": 481, "case_ids": [481], "task_id": None}

    with patch("cases.views.core.search_case", return_value=answer):
        response = client.post(reverse("add-case"), {"query": "https://x/case"})

    assert response.status_code == 302
    assert response["Location"] == reverse("case-detail", kwargs={"core_case_id": 481})
    assert CaseSubscription.objects.filter(user=natasha, core_case_id=481).exists()


def test_processing_creates_a_pending_search(client, natasha) -> None:
    """status=processing — заводим ожидание и ведём на страницу ожидания.

    Ожидание живёт в БАЗЕ, а не только в URL: закрытую вкладку добирает
    resolve_pending_searches, иначе подписка потерялась бы молча.
    """
    answer = {"status": "processing", "case_id": None, "case_ids": [], "task_id": 77}

    with patch("cases.views.core.search_case", return_value=answer):
        response = client.post(reverse("add-case"), {"query": "https://x/case"})

    pending = PendingCaseSearch.objects.get(user=natasha, core_task_id=77)
    assert pending.query == "https://x/case"
    assert pending.is_pending is True
    assert response["Location"] == reverse("pending-search", kwargs={"pk": pending.pk})


def test_double_submit_does_not_create_two_pendings(client, natasha) -> None:
    """Повторный POST той же формы не заводит второе ожидание."""
    answer = {"status": "processing", "case_ids": [], "task_id": 77}

    with patch("cases.views.core.search_case", return_value=answer):
        client.post(reverse("add-case"), {"query": "https://x/case"})
        client.post(reverse("add-case"), {"query": "https://x/case"})

    assert PendingCaseSearch.objects.filter(core_task_id=77).count() == 1


def test_several_cards_subscribe_to_all_of_them(client, natasha) -> None:
    """Один УИД — несколько карточек: подписываем на все (для MVP).

    УИД сквозной, и по нему бывает несколько производств. Экран выбора отложен, но
    потерять карточки нельзя.
    """
    answer = {"status": "exists", "case_id": 481, "case_ids": [481, 1188], "task_id": None}

    with patch("cases.views.core.search_case", return_value=answer):
        client.post(reverse("add-case"), {"query": "77MS0466-01-2026-003751-93"})

    assert set(
        CaseSubscription.objects.filter(user=natasha).values_list("core_case_id", flat=True)
    ) == {481, 1188}


def test_found_cards_and_a_running_task_do_both(client, natasha) -> None:
    """Часть нашлась, поиск всё равно идёт: подписываем И заводим ожидание.

    Так отвечает ветка по УИД: карточки могли прийти со страниц других инстанций, а
    московской среди них может не быть.
    """
    answer = {"status": "processing", "case_id": 481, "case_ids": [481], "task_id": 77}

    with patch("cases.views.core.search_case", return_value=answer):
        response = client.post(reverse("add-case"), {"query": "77MS0466-01-2026-003751-93"})

    assert CaseSubscription.objects.filter(core_case_id=481).exists()
    assert PendingCaseSearch.objects.filter(core_task_id=77).exists()
    assert "pending" in response["Location"]


@pytest.mark.parametrize("status", ["invalid_query", "invalid_uid"])
def test_bad_input_becomes_a_field_error(client, natasha, status) -> None:
    """Ошибка формата — ошибка ПОЛЯ: пользователь ввёл не то."""
    with patch("cases.views.core.search_case", return_value={"status": status}):
        response = client.post(reverse("add-case"), {"query": "мусор"})

    assert response.status_code == 200
    assert response.context["form"].errors.get("query")
    assert not CaseSubscription.objects.exists()


@pytest.mark.parametrize("status", ["link_required", "unsupported_court"])
def test_court_refusal_shows_the_message_from_core(client, natasha, status) -> None:
    """Отказ про суд — ошибка формы, и текст берём с сервера как есть.

    Пользователь ввёл всё правильно, просто портал так не умеет. Текст от core знает про
    конкретный участок; сочинять свой — терять эти подробности.
    """
    message = "Судебный участок № 235: поиск по УИД тут не поддерживается."

    with patch(
        "cases.views.core.search_case",
        return_value={"status": status, "message": message},
    ):
        response = client.post(reverse("add-case"), {"query": "77MS0235-01-2026-1-1"})

    assert message in response.content.decode()
    assert response.context["form"].errors.get("__all__")


def test_core_outage_on_add_is_reported_not_crashed(client, natasha) -> None:
    """core недоступен — говорим об этом в форме, а не отдаём 500."""
    with patch("cases.views.core.search_case", side_effect=CoreUnavailable("нет")):
        response = client.post(reverse("add-case"), {"query": "https://x/case"})

    assert response.status_code == 200
    assert response.context["form"].errors.get("__all__")


def test_adding_a_case_syncs_monitoring(client, natasha, quiet_monitoring) -> None:
    """Подписались — core узнал новый список дел на обходе."""
    answer = {"status": "exists", "case_id": 481, "case_ids": [481]}

    with patch("cases.views.core.search_case", return_value=answer):
        client.post(reverse("add-case"), {"query": "https://x/case"})

    quiet_monitoring.assert_called_once()
    assert quiet_monitoring.call_args.args[0] == [481]


# ----------------------------------------------------------- страница ожидания
def test_pending_page_shows_progress_while_running(client, natasha) -> None:
    pending = PendingCaseSearch.objects.create(
        user=natasha, query="https://x/case", core_task_id=77
    )
    task = {"task_id": 77, "status": "running", "attempts": 2, "last_error": None}

    with patch("cases.monitoring.core.get_search_task", return_value=task), \
         patch("cases.views.core.get_search_task", return_value=task):
        response = client.get(reverse("pending-search", kwargs={"pk": pending.pk}))

    assert response.status_code == 200
    # Обновление метатегом, без JS.
    assert "http-equiv=\"refresh\"" in response.content.decode()


def test_pending_page_creates_the_subscription_on_success(client, natasha) -> None:
    """Задача завершилась — подписка создаётся, ожидание закрывается."""
    pending = PendingCaseSearch.objects.create(
        user=natasha, query="https://x/case", core_task_id=77
    )
    task = {"task_id": 77, "status": "success", "case_id": 481, "attempts": 1}

    with patch("cases.monitoring.core.get_search_task", return_value=task):
        response = client.get(reverse("pending-search", kwargs={"pk": pending.pk}))

    assert response.status_code == 302
    assert CaseSubscription.objects.filter(user=natasha, core_case_id=481).exists()
    pending.refresh_from_db()
    assert pending.is_pending is False


def test_failed_search_explains_itself(client, natasha) -> None:
    pending = PendingCaseSearch.objects.create(
        user=natasha, query="https://x/case", core_task_id=77
    )
    task = {"task_id": 77, "status": "failed", "last_error": "captcha timeout"}

    with patch("cases.monitoring.core.get_search_task", return_value=task), \
         patch("cases.views.core.get_search_task", return_value=task):
        response = client.get(reverse("pending-search", kwargs={"pk": pending.pk}))

    assert response.status_code == 200
    assert "captcha timeout" in response.content.decode()
    assert not CaseSubscription.objects.exists()


def test_core_outage_leaves_the_pending_waiting(client, natasha) -> None:
    """core недоступен — ожидание НЕ провалено.

    Пометить его провалившимся из-за нашего же таймаута значило бы соврать: дело в core,
    возможно, уже нашлось.
    """
    pending = PendingCaseSearch.objects.create(
        user=natasha, query="https://x/case", core_task_id=77
    )

    with patch("cases.monitoring.core.get_search_task", side_effect=CoreUnavailable("нет")), \
         patch("cases.views.core.get_search_task", side_effect=CoreUnavailable("нет")):
        response = client.get(reverse("pending-search", kwargs={"pk": pending.pk}))

    assert response.status_code == 200
    pending.refresh_from_db()
    assert pending.is_pending is True
    assert pending.last_error == ""


def test_another_users_pending_is_not_visible(client, natasha) -> None:
    anna = User.objects.create_user("anna", password="x")
    pending = PendingCaseSearch.objects.create(
        user=anna, query="https://x", core_task_id=99
    )

    response = client.get(reverse("pending-search", kwargs={"pk": pending.pk}))

    assert response.status_code == 404


# ------------------------------------------------------------- страница дела
def test_case_page_shows_data_from_core(client, natasha) -> None:
    CaseSubscription.objects.create(user=natasha, core_case_id=481)
    card = {
        "id": 481, "code": "05-0444/1/2026", "uid": "77MS0466-01-2026-003751-93",
        "status": "Рассмотрено", "court": {"name": "Судебный участок № 463", "region": "Москва"},
        "judges": [{"id": 5, "full_name": "Иванова И. И."}],
        "sides": [], "events": [], "court_sessions": [], "documents": [],
        "place_history": [], "urls": [],
    }

    with patch("cases.views.core.get_case", return_value=card):
        response = client.get(reverse("case-detail", kwargs={"core_case_id": 481}))

    body = response.content.decode()
    assert response.status_code == 200
    assert "05-0444/1/2026" in body
    assert "Иванова И. И." in body


def test_another_users_case_is_not_visible(client, natasha) -> None:
    """Чужое дело — 404, и проверять права отдельно не приходится.

    DetailView построен над ПОДПИСКОЙ, поэтому queryset уже ограничен своими. Забыть эту
    проверку нельзя — её просто нет как отдельного кода.
    """
    anna = User.objects.create_user("anna", password="x")
    CaseSubscription.objects.create(user=anna, core_case_id=999)

    with patch("cases.views.core.get_case", return_value={}) as mock:
        response = client.get(reverse("case-detail", kwargs={"core_case_id": 999}))

    assert response.status_code == 404
    # До core дело даже не дошло: незачем спрашивать про то, что показывать не будем.
    mock.assert_not_called()


def test_case_page_survives_core_outage(client, natasha) -> None:
    CaseSubscription.objects.create(user=natasha, core_case_id=481)

    with patch("cases.views.core.get_case", side_effect=CoreUnavailable("нет")):
        response = client.get(reverse("case-detail", kwargs={"core_case_id": 481}))

    assert response.status_code == 200
    assert response.context["core_unavailable"] is True


# -------------------------------------------------------------------- отписка
def test_unsubscribe_deactivates_and_syncs(client, natasha, quiet_monitoring) -> None:
    """Отписка гасит флаг и сообщает core новый список."""
    CaseSubscription.objects.create(user=natasha, core_case_id=481)

    response = client.post(reverse("unsubscribe", kwargs={"core_case_id": 481}))

    assert response.status_code == 302
    subscription = CaseSubscription.objects.get(user=natasha, core_case_id=481)
    assert subscription.is_active is False
    # Список пуст, и это правда — значит force, иначе core отклонил бы его с 409 и дело
    # осталось бы на обходе навсегда.
    assert quiet_monitoring.call_args.args[0] == []
    assert quiet_monitoring.call_args.kwargs["force"] is True


def test_unsubscribe_needs_post(client, natasha) -> None:
    """GET не отписывает: по ссылке мог бы пройти префетчер браузера."""
    CaseSubscription.objects.create(user=natasha, core_case_id=481)

    response = client.get(reverse("unsubscribe", kwargs={"core_case_id": 481}))

    assert response.status_code == 405
    assert CaseSubscription.objects.get(core_case_id=481).is_active is True


def test_cannot_unsubscribe_from_another_users_case(client, natasha) -> None:
    anna = User.objects.create_user("anna", password="x")
    CaseSubscription.objects.create(user=anna, core_case_id=999)

    response = client.post(reverse("unsubscribe", kwargs={"core_case_id": 999}))

    assert response.status_code == 404
    assert CaseSubscription.objects.get(core_case_id=999).is_active is True


def test_resubscribing_reactivates_instead_of_duplicating(client, natasha) -> None:
    """Подписался снова после отписки — та же строка оживает, дубля нет."""
    CaseSubscription.objects.create(user=natasha, core_case_id=481, is_active=False)
    answer = {"status": "exists", "case_id": 481, "case_ids": [481]}

    with patch("cases.views.core.search_case", return_value=answer):
        client.post(reverse("add-case"), {"query": "https://x/case"})

    assert CaseSubscription.objects.filter(core_case_id=481).count() == 1
    assert CaseSubscription.objects.get(core_case_id=481).is_active is True
