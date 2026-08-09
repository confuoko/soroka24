"""Маршруты client."""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from apps.accounts.views import signup
from pages.views import coming_soon, home

urlpatterns = [
    path("", home, name="home"),
    path("coming-soon/", coming_soon, name="coming_soon"),
    # Вход и выход — штатные view Django, своего кода не пишем.
    #
    # Имена web_login/web_logout, а НЕ login/logout: djoser регистрирует свои роуты
    # (/api/v1/auth/token/login|logout) ровно с этими именами, и при совпадении
    # reverse() отдаёт последний зарегистрированный — то есть эндпоинт API. Из-за
    # этого выход уводил на JSON вместо страницы входа.
    path("accounts/login/", auth_views.LoginView.as_view(), name="web_login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="web_logout"),
    path("accounts/signup/", signup, name="signup"),
    path("cases/", include("apps.monitoring.urls")),
    path("api/v1/", include("apps.api.urls")),
    path("admin/", admin.site.urls),
]
