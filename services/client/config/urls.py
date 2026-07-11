"""Маршруты client. Пока только страница-заглушка на корне."""
from django.urls import path

from pages.views import coming_soon

urlpatterns = [
    path("", coming_soon, name="coming_soon"),
]
