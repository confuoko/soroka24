"""Настройки Django для client.

Клиентский сервис отвечает за пользователей, подписки и список дел на мониторинге.
Сами дела он НЕ парсит и в БД core не лезет: за делами ходит по HTTP в core-api
(см. apps/core_client/client.py). Поэтому у него своя база — soroka_client, и
миграции Django не пересекаются с alembic-миграциями core.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # REST API: authtoken нужен djoser'у для выдачи токенов (по ним придёт бот).
    "rest_framework",
    "rest_framework.authtoken",
    "djoser",
    # Свои приложения.
    "apps.accounts",
    "apps.monitoring",
    "pages",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- База данных --------------------------------------------------------------
# Отдельная база в том же инстансе PostgreSQL, что и у core. Параметры задаём по
# частям, а не одним DATABASE_URL: у core он в формате SQLAlchemy
# (postgresql+psycopg2://...), который Django не понимает.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("CLIENT_DB_NAME", "soroka_client"),
        "USER": os.getenv("CLIENT_DB_USER", "soroka"),
        "PASSWORD": os.getenv("CLIENT_DB_PASSWORD", "soroka"),
        "HOST": os.getenv("CLIENT_DB_HOST", "localhost"),
        "PORT": os.getenv("CLIENT_DB_PORT", "5432"),
    }
}

# Своя модель пользователя заводится СРАЗУ: после первой миграции поменять
# AUTH_USER_MODEL уже практически невозможно. Поле telegram_id нужно боту.
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Имена web_login/web_logout — не login/logout: последние занимает djoser своими
# API-эндпоинтами (см. комментарий в config/urls.py).
LOGIN_URL = "web_login"
LOGIN_REDIRECT_URL = "case_list"
LOGOUT_REDIRECT_URL = "web_login"

# --- DRF ----------------------------------------------------------------------
# Две схемы аутентификации: токен — для бота и внешних клиентов, сессия — чтобы
# тот же API открывался в браузере залогиненным пользователем.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

DJOSER = {
    "USER_ID_FIELD": "id",
    "LOGIN_FIELD": "username",
    # Регистрация через API должна заводить подписку так же, как веб-форма, —
    # поэтому свой сериализатор, который зовёт accounts.services.register_user.
    "SERIALIZERS": {
        "user_create": "apps.api.serializers.UserCreateSerializer",
    },
}

# --- core-api -----------------------------------------------------------------
# Адрес сервиса парсинга. Внутри docker-сети — имя контейнера.
CORE_API_URL = os.getenv("CORE_API_URL", "http://localhost:8000").rstrip("/")
# Таймаут запроса к core. Сам поход в суд асинхронный (core сразу отдаёт task_id),
# поэтому долго ждать здесь нечего.
CORE_API_TIMEOUT = int(os.getenv("CORE_API_TIMEOUT", "15"))

# --- Celery -------------------------------------------------------------------
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
# Стили лежат в общей папке сервиса, а не внутри приложения: они относятся ко всем
# страницам сразу. При DEBUG=1 их отдаёт сам runserver.
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
