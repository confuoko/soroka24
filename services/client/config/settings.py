"""Настройки клиентского сервиса Soroka24.

Обычный Django: ORM, формы, шаблоны, аутентификация, стандартные CBV. Ни DRF, ни SPA —
см. ТЗ §13. Всё, что связано с судебными данными, живёт в core_v2 и берётся у него по
HTTP.

Читаем окружение теми же модульными os.getenv, что и core_v2: увидеть весь набор
настроек сверху вниз важнее валидации типов, которой здесь нечего проверять.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Основное -----------------------------------------------------------------

# Дефолт только для локальной разработки. В проде задать через env: на этом ключе
# держатся подписи сессий и токены сброса пароля.
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret-change-me")

DEBUG = os.getenv("DJANGO_DEBUG", "1") not in ("0", "false", "False")

# Через запятую. В DEBUG пусто означает «любой хост» — Django сам разрешает localhost.
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,client-web").split(",")
    if host.strip()
]

# --- Приложения ---------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "cases",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

# --- База данных --------------------------------------------------------------


def _database_from_url(url: str) -> dict:
    """Разобрать postgres://user:pass@host:port/db в настройки Django.

    Своими десятью строками, а не пакетом dj-database-url: одна функция, которую видно
    целиком, понятнее зависимости, которую надо идти читать. Форма URL здесь ровно одна —
    та, что лежит в .env.
    """
    parts = urlsplit(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parts.path.lstrip("/"),
        "USER": parts.username or "",
        "PASSWORD": parts.password or "",
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
    }


# База СВОЯ, отдельная от soroka_core_v2, и это не вкусовщина: две ORM на одних таблицах
# ломаются на каждой миграции. Судебные данные здесь не хранятся вовсе — ни Case, ни
# CaseEvent, ни CourtSession (ТЗ §2).
DATABASES = {
    "default": _database_from_url(
        os.getenv(
            "CLIENT_DATABASE_URL",
            "postgres://soroka:soroka@localhost:5432/soroka_client",
        )
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Локализация --------------------------------------------------------------

LANGUAGE_CODE = "ru-ru"
# Пояс отображения. Данные из core приходят в UTC со смещением, шаблоны переводят их сюда.
TIME_ZONE = os.getenv("TZ", "Europe/Moscow")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Аутентификация -----------------------------------------------------------

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "my-cases"
LOGOUT_REDIRECT_URL = "login"

# --- Интеграция с core_v2 -----------------------------------------------------

# Адрес HTTP API core. Единственный способ добраться до судебных данных: общей БД у
# сервисов нет и не будет.
CORE_API_URL = os.getenv("CORE_API_URL", "http://localhost:8000")

# Таймауты запросов к core, секунды: (на соединение, на чтение).
#
# Читающий таймаут короткий сознательно. Все ручки, которые мы зовём, отвечают из БД —
# в портал суда core на них не ходит (это делают его воркеры). Значит, долгий ответ
# означает не «ещё немного», а «что-то не так», и держать перед пользователем висящую
# страницу полминуты незачем.
CORE_API_TIMEOUT = (
    float(os.getenv("CORE_API_CONNECT_TIMEOUT", "3")),
    float(os.getenv("CORE_API_READ_TIMEOUT", "10")),
)

# --- Подписка на изменения (RabbitMQ) -----------------------------------------

# Брокер, из которого приходят факты изменений. Тот же RabbitMQ, что у Celery в core, но
# очередь другая: в case_changes лежит обычный JSON, а не celery-протокол.
INTEGRATION_BROKER_URL = os.getenv(
    "INTEGRATION_BROKER_URL", "amqp://soroka:soroka@localhost:5672//"
)

# Имена ОБЯЗАНЫ совпадать с теми, что использует publisher core_v2. Расхождение не даёт
# ошибки: publisher уложит сообщения в свой exchange, мы будем слушать свою пустую очередь,
# и обе стороны будут считать, что работают. Поэтому имена живут в общем .env, а не
# константами в двух кодовых базах.
CASE_CHANGES_EXCHANGE = os.getenv("CASE_CHANGES_EXCHANGE", "soroka.case_changes")
CASE_CHANGES_QUEUE = os.getenv("CASE_CHANGES_QUEUE", "case_changes")

# Сколько неподтверждённых сообщений брокер отдаёт разом.
#
# Не «побольше для скорости»: каждое неподтверждённое сообщение — это работа, которую при
# падении процесса брокер отдаст заново. Двадцать штук проглотить и переделать не жалко,
# двадцать тысяч — уже долго.
CONSUMER_PREFETCH = int(os.getenv("CONSUMER_PREFETCH", "20"))

# Версия контракта integration event, которую мы умеем разбирать. Незнакомую отвергаем
# явно, а не читаем поля наугад.
INTEGRATION_EVENT_VERSION = 1

# --- Почта (Phase 7) ----------------------------------------------------------

# Пока в консоль: цепочку уведомлений делаем после работающего unread (ТЗ §8).
EMAIL_BACKEND = os.getenv(
    "DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)

# --- Логи ---------------------------------------------------------------------

# Минимальная настройка: логи собирает docker, но без неё не было бы видно ни обращений к
# core, ни отказов синхронизации мониторинга — а это первое, что смотрят, когда «дела не
# обновляются».
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # Шум про каждый SQL-запрос ни к чему, а вот предупреждения нужны.
        "django.db.backends": {"level": "WARNING"},
    },
}
