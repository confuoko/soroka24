"""Маршруты API v1.

Регистрацию и выдачу токенов целиком отдаём djoser — своего кода аутентификации
не пишем:
    POST /api/v1/auth/users/        {username, password}  — регистрация
    POST /api/v1/auth/token/login/  {username, password}  — получить токен
    POST /api/v1/auth/token/logout/                        — отозвать токен
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.api.views import MonitoredCaseViewSet

router = DefaultRouter()
router.register("cases", MonitoredCaseViewSet, basename="case")

urlpatterns = [
    path("", include(router.urls)),
    path("auth/", include("djoser.urls")),
    path("auth/", include("djoser.urls.authtoken")),
]
