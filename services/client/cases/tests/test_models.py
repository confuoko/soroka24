"""Модели: ограничения и граница ответственности.

Сценарии из ТЗ §11, Django-часть: `(user, core_case_id)` unique, список для мониторинга
строится с distinct, судебные сущности не дублируются в базе Django.

Последний тест — test_no_court_tables_in_the_schema — самый важный здесь и самый
неочевидный: он проверяет не поведение, а решение. Копия судебной модели в двух сервисах
означала бы, что правка парсера требует миграции здесь, а расхождение копий никто не
заметит.
"""
import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction

from cases.models import CaseSubscription, PendingCaseSearch
from cases.monitoring import monitored_case_ids

pytestmark = pytest.mark.django_db


@pytest.fixture
def natasha():
    return User.objects.create_user("natasha", password="x")


@pytest.fixture
def anna():
    return User.objects.create_user("anna", password="x")


# ------------------------------------------------------------------- подписки
def test_same_user_cannot_subscribe_twice(natasha) -> None:
    """Дважды подписаться на одно дело нельзя."""
    CaseSubscription.objects.create(user=natasha, core_case_id=100)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CaseSubscription.objects.create(user=natasha, core_case_id=100)


def test_different_users_subscribe_to_the_same_case(natasha, anna) -> None:
    """Разные пользователи на одно дело — норма, и это ключевой случай.

    Именно из него следует, что флаг обхода живёт на карточке в core, а не на подписке:
    дело с двумя подписчиками обходится ОДИН раз.
    """
    CaseSubscription.objects.create(user=natasha, core_case_id=100)
    CaseSubscription.objects.create(user=anna, core_case_id=100)

    assert CaseSubscription.objects.filter(core_case_id=100).count() == 2


# --------------------------------------------------- список для мониторинга
def test_monitoring_list_is_distinct(natasha, anna) -> None:
    """Дело с двумя подписчиками попадает в список ОДИН раз.

    Из ТЗ §3: Наташа и Аня следят за делом 100, Пётр — за 200. В core должны уехать два
    id, а не три. Поход на портал стоит прокси и оплаченной капчи, и он не должен
    множиться на число заинтересованных.
    """
    peter = User.objects.create_user("peter", password="x")
    CaseSubscription.objects.create(user=natasha, core_case_id=100)
    CaseSubscription.objects.create(user=anna, core_case_id=100)
    CaseSubscription.objects.create(user=peter, core_case_id=200)

    assert monitored_case_ids() == [100, 200]


def test_inactive_subscriptions_are_not_monitored(natasha) -> None:
    """Отписался — дело уходит из списка, даже если строка осталась."""
    CaseSubscription.objects.create(user=natasha, core_case_id=100)
    CaseSubscription.objects.create(user=natasha, core_case_id=200, is_active=False)

    assert monitored_case_ids() == [100]


def test_case_stays_monitored_while_anyone_follows_it(natasha, anna) -> None:
    """Один отписался, второй остался — дело с обхода не снимается.

    Ошибка здесь стоила бы дорого и была бы тихой: дело перестало бы обновляться для
    второго пользователя, и он узнал бы об этом, только не дождавшись уведомления.
    """
    CaseSubscription.objects.create(user=natasha, core_case_id=100)
    anna_subscription = CaseSubscription.objects.create(user=anna, core_case_id=100)

    anna_subscription.is_active = False
    anna_subscription.save()

    assert monitored_case_ids() == [100]


def test_empty_list_when_nobody_follows_anything() -> None:
    """Пустой список — законное состояние, а не ошибка."""
    assert monitored_case_ids() == []


# --------------------------------------------------------- ожидания поиска
def test_pending_search_is_unique_per_task(natasha) -> None:
    """Одна задача core — одно ожидание. Страховка от двойного submit формы."""
    PendingCaseSearch.objects.create(user=natasha, query="http://x", core_task_id=7)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PendingCaseSearch.objects.create(
                user=natasha, query="http://x", core_task_id=7
            )


def test_pending_flag_follows_resolved_at(natasha) -> None:
    from django.utils import timezone

    pending = PendingCaseSearch.objects.create(
        user=natasha, query="http://x", core_task_id=7
    )
    assert pending.is_pending is True

    pending.resolved_at = timezone.now()
    assert pending.is_pending is False


# ------------------------------------------------------- граница ответственности
def test_no_court_tables_in_the_schema() -> None:
    """В базе Django НЕТ судебных сущностей (ТЗ §2, §11).

    Проверяем живую схему, а не список моделей: модель можно завести и в обход
    models.py — например, сторонним приложением, — а таблица всё равно появится.

    Появись здесь `case` или `court_session`, и мы получили бы вторую копию судебной
    модели: правка парсера в core требовала бы миграции тут, а разошедшиеся копии никто
    бы не заметил.
    """
    tables = set(connection.introspection.table_names())
    forbidden = {
        "case",
        "cases_case",
        "case_event",
        "cases_caseevent",
        "court",
        "cases_court",
        "court_session",
        "cases_courtsession",
        "document",
        "cases_document",
        "judge",
        "cases_judge",
        "side",
        "cases_side",
        "outbox_event",
    }

    assert not (forbidden & tables), f"судебные таблицы в базе клиента: {sorted(forbidden & tables)}"


def test_only_our_own_tables_are_ours() -> None:
    """Приложение cases заводит ровно две таблицы: подписки и ожидания.

    Третья означала бы, что в клиент прокралась сущность, которой по ТЗ §2 здесь пока не
    место — тариф, папка, интервал мониторинга. UserCaseChange добавится в Phase 6, и
    этот тест придётся обновить осознанно.
    """
    ours = {
        name
        for name in connection.introspection.table_names()
        if name.startswith("cases_")
    }

    assert ours == {"cases_casesubscription", "cases_pendingcasesearch"}
