from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from apps.accounts.models import Subscription, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "telegram_id", "is_staff", "date_joined")
    # Штатные fieldsets ничего не знают про telegram_id — дописываем своей секцией.
    fieldsets = UserAdmin.fieldsets + (("Телеграм", {"fields": ("telegram_id",)}),)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "started_at", "is_active", "months_elapsed")
    list_filter = ("is_active",)
    search_fields = ("user__username",)

    @admin.display(description="месяцев прошло")
    def months_elapsed(self, obj: Subscription) -> int:
        return obj.months_elapsed
