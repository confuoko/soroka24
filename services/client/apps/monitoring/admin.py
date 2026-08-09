from django.contrib import admin

from apps.monitoring.models import MonitoredCase


@admin.register(MonitoredCase)
class MonitoredCaseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "state",
        "core_case_id",
        "core_status",
        "core_changed_at",
        "core_last_checked_at",
    )
    list_filter = ("state",)
    search_fields = ("user__username", "source_url", "core_case_id")
    readonly_fields = ("created_at", "updated_at")
