"""Админка: посмотреть подписки и ожидания глазами.

Судебных данных здесь нет, поэтому и смотреть в админке нечего, кроме «кто на что
подписан» и «кто чего ждёт». Второе — первое, куда стоит заглянуть, если пользователь
говорит «добавил дело, а его нет»: висящее ожидание с непустым last_error сразу скажет,
почему.
"""
from django.contrib import admin

from cases.models import CaseSubscription, PendingCaseSearch


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

