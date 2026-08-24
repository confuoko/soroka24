"""Корневой URLconf клиентского сервиса.

Аутентификация — готовыми Django-вьюхами через django.contrib.auth.urls: логин, выход,
смена пароля. Своего писать нечего (ТЗ §9).
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Даёт login/, logout/, password_change/ и остальное — всё стандартными вьюхами.
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("cases.urls")),
]
