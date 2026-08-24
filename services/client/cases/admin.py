"""Админка: посмотреть подписки и ожидания глазами.

Судебных данных здесь нет, поэтому и смотреть в админке нечего, кроме «кто на что
подписан» и «кто чего ждёт». Второе — первое, куда стоит заглянуть, если пользователь
говорит «добавил дело, а его нет»: висящее ожидание с непустым last_error сразу скажет,
почему.
"""
from django.contrib import admin

from cases.models import CaseSubscription, PendingCaseSearch, UserCaseChange


@admin.register(CaseSubscription)
class CaseSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "core_case_id", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("core_case_id", "user__username")


@admin.register(PendingCaseSearch)
class PendingCaseSearchAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "core_task_id", "query", "created_at", "resolved_at", "last_error")
    list_filter = ("resolved_at",)
    search_fields = ("core_task_id", "query", "user__username")


@admin.register(UserCaseChange)
class UserCaseChangeAdmin(admin.ModelAdmin):
    """Изменения по делам, разложенные по пользователям.

    Сюда заглядывают с двумя вопросами. «Почему у человека счётчик непрочитанного?» — видно
    по read_at. И «почему счётчик удвоился?» — если такое случилось, здесь будут две строки
    с одним integration_event_id, а значит сломался UNIQUE, а не доставка.
    """

    list_display = (
        "id", "user", "subscription", "event_type",
        "core_entity_id", "occurred_at", "read_at",
    )
    list_filter = ("event_type", "read_at")
    search_fields = ("integration_event_id", "user__username", "subscription__core_case_id")
    # Строки пишет consumer, read_at ставит страница дела. Править их руками незачем.
    readonly_fields = ("integration_event_id", "occurred_at")
