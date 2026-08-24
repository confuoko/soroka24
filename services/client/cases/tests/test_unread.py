"""Unread: раскладка изменений по подписчикам и «что нового» на экране.

Сценарии из ТЗ §11: изменение по делу с двумя подписчиками даёт два UserCaseChange,
повторная доставка не создаёт дублей, unread count корректен, открытие карточки выставляет
read_at.

Три теста здесь важнее остальных.

test_duplicate_delivery_creates_no_duplicates — про единственное свойство, ради которого
UNIQUE (user, integration_event_id) вообще существует. Доставка at-least-once: то же
сообщение придёт снова после любого сбоя на пути, и счётчик у пользователя не должен
удвоиться.

test_case_page_shows_what_is_new_before_marking_it_read — про порядок «читаем, потом
помечаем». Обратный порядок не падает и не ломает данные, он просто делает страницу
бесполезной: человек открыл дело и не увидел, что именно изменилось.

test_unread_is_per_user — unread персональный. Прочитанное одним не должно исчезать у
другого, хотя изменение в core было одно.
"""
import datetime as dt
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.urls import reverse

from cases.consumer import CaseChange, Outcome, fan_out, handle
from cases.integrations.core import CoreUnavailable
from cases.models import CaseSubscription, UserCaseChange

pytestmark = pytest.mark.django_db

CASE_ID = 481
OCCURRED = dt.datetime(2026, 8, 23, 13, 20, tzinfo=dt.timezone.utc)


def change(message_id=1502, case_id=CASE_ID, entity_id=712, event_type="event_new"):
    return CaseChange(
        id=message_id,
        type=event_type,
        case_id=case_id,
        entity_id=entity_id,
        occurred_at=OCCURRED,
        version=1,
    )


@pytest.fixture(autouse=True)
def quiet_monitoring():
    """Подписки в тестах заводятся напрямую, но отписка зовёт core. В сеть не пускаем."""
    with patch("cases.monitoring.core.replace_monitored_cases") as mock:
        mock.return_value = {"monitored": 0, "added": 0, "removed": 0, "unknown_ids": []}
        yield mock


@pytest.fixture
def natasha(client):
    user = User.objects.create_user("natasha", password="secret123")
    client.login(username="natasha", password="secret123")
    CaseSubscription.objects.create(user=user, core_case_id=CASE_ID)
    return user


@pytest.fixture
def anna():
    """Второй подписчик того же дела. Без логина: он нужен не всем тестам."""
    user = User.objects.create_user("anna", password="secret123")
    CaseSubscription.objects.create(user=user, core_case_id=CASE_ID)
    return user


# ------------------------------------------------------------- раскладка
def test_change_reaches_every_subscriber(natasha, anna) -> None:
    """Одно изменение в core → строка каждому подписчику.

    Дело при этом обошли ОДИН раз: размножается не работа, а знание о том, кому показано.
    """
    assert fan_out(change()) == 2

    assert UserCaseChange.objects.count() == 2
    assert set(UserCaseChange.objects.values_list("user_id", flat=True)) == {
        natasha.id,
        anna.id,
    }


def test_change_carries_the_pointers_not_the_data(natasha) -> None:
    """В строке указатели, а не судебные данные: подробности берутся у core при показе."""
    fan_out(change())

    row = UserCaseChange.objects.get()
    assert row.integration_event_id == 1502
    assert row.event_type == "event_new"
    assert row.core_entity_id == 712
    assert row.occurred_at == OCCURRED
    assert row.read_at is None


def test_field_change_has_no_entity(natasha) -> None:
    """У изменения скалярного поля дела сущности нет — поменялась сама карточка."""
    fan_out(change(entity_id=None, event_type="case_field_changed"))

    assert UserCaseChange.objects.get().core_entity_id is None


def test_nobody_subscribed_is_not_an_error() -> None:
    """Ноль подписчиков — штатный исход, а не сбой.

    Между обходом дела и доставкой сообщения последний подписчик мог отписаться. Возвращать
    сообщение в очередь незачем: подписчики от этого не появятся.
    """
    assert fan_out(change()) == 0
    assert UserCaseChange.objects.count() == 0


def test_inactive_subscription_gets_nothing(natasha) -> None:
    """Отписавшийся не получает новостей по делу."""
    CaseSubscription.objects.update(is_active=False)

    assert fan_out(change()) == 0
    assert UserCaseChange.objects.count() == 0


def test_other_cases_are_untouched(natasha) -> None:
    """Изменение по чужому делу не создаёт ничего."""
    assert fan_out(change(case_id=999)) == 0
    assert UserCaseChange.objects.count() == 0


# ------------------------------------------------------- идемпотентность
def test_duplicate_delivery_creates_no_duplicates(natasha, anna) -> None:
    """Повторная доставка того же сообщения не удваивает счётчик.

    Доставка at-least-once: сообщение придёт снова после любого сбоя на пути, и это штатно.
    """
    for _ in range(6):
        assert fan_out(change()) == 2

    assert UserCaseChange.objects.count() == 2


def test_duplicate_does_not_resurrect_read_state(natasha) -> None:
    """Повторная доставка не сбрасывает уже проставленный read_at.

    ignore_conflicts пропускает конфликтующую строку, а не обновляет её. Иначе прочитанное
    вновь становилось бы непрочитанным при каждой перепосылке.
    """
    fan_out(change())
    UserCaseChange.objects.update(read_at=OCCURRED)

    fan_out(change())

    assert UserCaseChange.objects.filter(read_at__isnull=True).count() == 0


def test_unique_constraint_exists(natasha) -> None:
    """Идемпотентность держится на базе, а не на аккуратности кода."""
    fan_out(change())
    row = UserCaseChange.objects.get()

    with pytest.raises(IntegrityError), transaction.atomic():
        UserCaseChange.objects.create(
            user=row.user,
            subscription=row.subscription,
            integration_event_id=row.integration_event_id,
            event_type=row.event_type,
            occurred_at=row.occurred_at,
        )


def test_two_users_can_hold_the_same_event_id(natasha, anna) -> None:
    """UNIQUE — по ПАРЕ. Одно сообщение обязано лечь строкой каждому подписчику."""
    fan_out(change())

    assert UserCaseChange.objects.filter(integration_event_id=1502).count() == 2


# ------------------------------------------------------------- handle()
def test_handle_processes_and_reports(natasha) -> None:
    """Разобранное сообщение раскладывается и подтверждается очереди."""
    body = (
        b'{"id": 1502, "type": "event_new", "version": 1, "case_id": 481, '
        b'"entity_id": 712, "occurred_at": "2026-08-23T13:20:00+00:00"}'
    )

    assert handle(body) == Outcome.PROCESSED
    assert UserCaseChange.objects.count() == 1


# ------------------------------------------------------------ unread в UI
def test_unread_count_on_my_cases(client, natasha) -> None:
    """«Мои дела» показывают, сколько нового по каждому делу."""
    fan_out(change(message_id=1))
    fan_out(change(message_id=2))
    fan_out(change(message_id=3))

    with patch("cases.views.core.summaries_by_id", return_value={}):
        response = client.get(reverse("my-cases"))

    assert response.context["rows"][0]["subscription"].unread == 3


def test_read_changes_do_not_count(client, natasha) -> None:
    fan_out(change(message_id=1))
    fan_out(change(message_id=2))
    UserCaseChange.objects.filter(integration_event_id=1).update(read_at=OCCURRED)

    with patch("cases.views.core.summaries_by_id", return_value={}):
        response = client.get(reverse("my-cases"))

    assert response.context["rows"][0]["subscription"].unread == 1


def test_unread_is_per_user(client, natasha, anna) -> None:
    """Наташа прочитала — у Анны новость на месте."""
    fan_out(change())
    UserCaseChange.objects.filter(user=natasha).update(read_at=OCCURRED)

    with patch("cases.views.core.summaries_by_id", return_value={}):
        response = client.get(reverse("my-cases"))
    assert response.context["rows"][0]["subscription"].unread == 0

    assert UserCaseChange.objects.filter(user=anna, read_at__isnull=True).count() == 1


def test_cases_with_news_come_first(client, natasha) -> None:
    """Дела с новостями наверх: это первое, зачем человек сюда пришёл."""
    quiet = CaseSubscription.objects.create(user=natasha, core_case_id=900)
    fan_out(change())  # новость по CASE_ID, подписка на него старше

    with patch("cases.views.core.summaries_by_id", return_value={}):
        response = client.get(reverse("my-cases"))

    rows = response.context["rows"]
    assert rows[0]["subscription"].core_case_id == CASE_ID
    assert rows[1]["subscription"].core_case_id == quiet.core_case_id


# ------------------------------------------------------- карточка и read_at
CARD = {
    "id": CASE_ID,
    "code": "05-0444/1/2026",
    "uid": "77MS0466-01-2026-003751-93",
    "court": {"name": "Судебный участок № 466", "region": "Москва"},
    "judges": [],
    "sides": [],
    "events": [
        {
            "id": 712,
            "event_date": "2026-08-23T13:20:00Z",
            "state_description": "Назначено судебное заседание",
            "document_str": "",
        },
        {
            "id": 700,
            "event_date": "2026-07-01T10:00:00Z",
            "state_description": "Дело зарегистрировано",
            "document_str": "",
        },
    ],
    "court_sessions": [],
    "documents": [],
    "place_history": [],
    "urls": [],
}


def open_card(client):
    with patch("cases.views.core.get_case", return_value=CARD):
        return client.get(reverse("case-detail", args=[CASE_ID]))


def test_opening_the_case_marks_everything_read(client, natasha) -> None:
    """Для MVP допустимо: открытие страницы помечает прочитанным всё по делу (ТЗ §8)."""
    fan_out(change(message_id=1))
    fan_out(change(message_id=2))

    open_card(client)

    assert UserCaseChange.objects.filter(read_at__isnull=True).count() == 0


def test_case_page_shows_what_is_new_before_marking_it_read(client, natasha) -> None:
    """Сначала читаем, потом помечаем: иначе подсвечивать было бы уже нечего.

    Тот же запрос обязан и показать новое, и погасить его. Обратный порядок не падает — он
    делает страницу бесполезной.
    """
    fan_out(change())

    response = open_card(client)

    assert len(response.context["new_changes"]) == 1
    assert response.context["new_entity_ids"] == {712}
    # И подсветка дошла до разметки — ровно на той строке, которая новая.
    assert b"is-new" in response.content
    # А уже прочитанное после этого запроса непрочитанным не осталось.
    assert UserCaseChange.objects.filter(read_at__isnull=True).count() == 0


def test_second_visit_shows_nothing_new(client, natasha) -> None:
    fan_out(change())
    open_card(client)

    response = open_card(client)

    assert response.context["new_changes"] == []
    assert response.context["new_entity_ids"] == set()


def test_field_changes_are_counted_separately(client, natasha) -> None:
    """У изменения реквизитов сущности нет — подсветить нечего, сказать есть что."""
    fan_out(change(entity_id=None, event_type="case_field_changed"))

    response = open_card(client)

    assert response.context["new_field_changes"] == 1
    assert response.context["new_entity_ids"] == set()


def test_opening_one_case_does_not_read_another(client, natasha) -> None:
    """Прочитанным становится только то дело, которое открыли."""
    CaseSubscription.objects.create(user=natasha, core_case_id=900)
    fan_out(change(message_id=1))
    fan_out(change(message_id=2, case_id=900))

    open_card(client)

    unread = UserCaseChange.objects.filter(read_at__isnull=True)
    assert unread.count() == 1
    assert unread.get().subscription.core_case_id == 900


def test_core_outage_still_marks_read(client, natasha) -> None:
    """Карточку показать нечем, но список изменений наш — и он показан и погашен.

    Иначе недоступность соседнего сервиса оставляла бы вечный счётчик: страницу открыть
    можно, а сбросить его нельзя.
    """
    fan_out(change())

    with patch("cases.views.core.get_case", side_effect=CoreUnavailable("нет связи")):
        response = client.get(reverse("case-detail", args=[CASE_ID]))

    assert response.context["core_unavailable"] is True
    assert len(response.context["new_changes"]) == 1
    assert UserCaseChange.objects.filter(read_at__isnull=True).count() == 0


def test_changes_die_with_the_subscription(natasha) -> None:
    """Удалили подписку — изменения уходят с ней: показывать их больше негде."""
    fan_out(change())

    CaseSubscription.objects.get(user=natasha, core_case_id=CASE_ID).delete()

    assert UserCaseChange.objects.count() == 0
