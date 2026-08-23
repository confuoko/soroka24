"""Чтение конфигурации из переменных окружения.

Простые модульные константы: ни классов, ни pydantic-settings. Прочитать сверху вниз и
увидеть весь набор настроек — важнее, чем валидация типов, которой здесь нечего проверять.

Всё читается ОДИН РАЗ при импорте. Значит, переменную окружения нельзя подменить после
импорта модуля — в тестах это делается через monkeypatch самой константы.

Отличие от старого core: переменных MONITORING_* здесь нет. Пользовательский мониторинг
живёт не в core (см. services/core_v2/ARCHITECTURE.md и ТЗ PRIORITY 2).
"""
import os
from pathlib import Path

# Корень сервиса (папка, в которой лежат app/, data/, alembic/).
# app/config.py -> parents[0] = app, parents[1] = core_v2.
CORE_ROOT = Path(__file__).resolve().parents[1]

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

# Подключение к PostgreSQL. База СВОЯ, отдельная от старого core: alembic_version —
# однострочная таблица, и две независимые истории миграций в одной базе несовместимы.
# Это позволяет держать старый core запущенным рядом и сверять поведение.
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg2://soroka:soroka@localhost:5432/soroka_core_v2"
)

# Доступ к админке SQLAdmin (логин/пароль и секрет для сессии-cookie).
# Дефолты — только для локальной разработки, в проде задать через env!
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "dev-admin-secret-change-me")

# JSON-справочник судов. Без строк Court не работает ничего: суд дела определяется
# по этому справочнику, а не по УИД.
COURTS_JSON_PATH = Path(os.getenv("COURTS_JSON_PATH", CORE_ROOT / "data" / "courts.json"))

# --- S3 (картинки капчи и отладочный архив HTML) ------------------------------

# Локально — MinIO из docker-compose; в проде облачный endpoint.
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
S3_BUCKET = os.getenv("S3_BUCKET", "soroka")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "soroka")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "soroka_secret")
# Регион boto3 требует формально; MinIO его игнорирует.
S3_REGION = os.getenv("S3_REGION", "us-east-1")

# --- Архив HTML страниц суда --------------------------------------------------

# Ключи в бакете: html_snapshots/<уид>/<код суда>-<номер дела>/<уид>_<время>.html.gz
HTML_SNAPSHOT_PREFIX = os.getenv("HTML_SNAPSHOT_PREFIX", "html_snapshots")

# По умолчанию ВЫКЛЮЧЕНО: архив разметки нужен только при разборе поехавшей вёрстки
# портала, а место в бакете занимает всегда.
HTML_SNAPSHOT_ENABLED = os.getenv("HTML_SNAPSHOT_ENABLED", "0") not in (
    "0",
    "false",
    "False",
)

# --- Распознавание капчи (rucaptcha.com) --------------------------------------

# Пусто — капчу разгадывать нечем, клиент честно падает.
RUCAPTCHA_API_KEY = os.getenv("RUCAPTCHA_API_KEY", "")

# Ключи в бакете: captcha/<хост>/<case_id>/<время>.png
CAPTCHA_PREFIX = os.getenv("CAPTCHA_PREFIX", "captcha")

# Портал умеет показать вторую капчу сразу после верно разгаданной первой.
CAPTCHA_ATTEMPTS = int(os.getenv("CAPTCHA_ATTEMPTS", "3"))

# Сколько секунд ждать разгадки от сервиса, прежде чем считать попытку провалившейся.
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "120"))

# Пул языков для распознавания: "rn" — русский и цифры.
CAPTCHA_LANGUAGE_POOL = os.getenv("CAPTCHA_LANGUAGE_POOL", "rn")

# --- Прокси -------------------------------------------------------------------

# Ходить на портал только через прокси из таблицы proxy. Пустой пул = задача падает.
# Выключать имеет смысл только на локальной отладке.
COURT_PROXY_REQUIRED = os.getenv("COURT_PROXY_REQUIRED", "1") not in (
    "0",
    "false",
    "False",
)
