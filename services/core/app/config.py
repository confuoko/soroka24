"""Чтение конфигурации из переменных окружения.

Пока — простые модульные константы (без классов и без бизнес-логики).
Позже здесь можно перейти на pydantic-settings, когда появятся настройки БД и т.п.
"""
import os
from pathlib import Path

# Корень сервиса core (папка, в которой лежат app/, data/, scripts/).
# app/config.py -> parents[0] = app, parents[1] = core.
CORE_ROOT = Path(__file__).resolve().parents[1]

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")

# Подключение к PostgreSQL (SQLAlchemy). В docker хост — postgres, локально — localhost.
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg2://soroka:soroka@localhost:5432/soroka"
)

# Доступ к админке SQLAdmin (логин/пароль и секрет для сессии-cookie).
# Дефолты — только для локальной разработки, в проде задать через env!
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "dev-admin-secret-change-me")

# JSON-справочник судов: его заливает в БД команда админки и scripts/sync_courts.py.
COURTS_JSON_PATH = Path(os.getenv("COURTS_JSON_PATH", CORE_ROOT / "data" / "courts.json"))

# --- S3 (снапшоты HTML страниц суда) -----------------------------------------

# Адрес S3-совместимого хранилища. Локально — MinIO из docker-compose; в проде
# сюда подставляется облачный endpoint (например, https://storage.yandexcloud.net).
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
S3_BUCKET = os.getenv("S3_BUCKET", "soroka")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "soroka")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "soroka_secret")
# Регион нужен boto3 формально; MinIO его игнорирует.
S3_REGION = os.getenv("S3_REGION", "us-east-1")

# --- Снапшоты HTML страниц суда ----------------------------------------------

# Префикс ключей в бакете: html_snapshots/<уид>/<уид>_<время>.html.gz
HTML_SNAPSHOT_PREFIX = os.getenv("HTML_SNAPSHOT_PREFIX", "html_snapshots")

# Выключатель на случай, если снапшоты не нужны (тесты, отладка).
HTML_SNAPSHOT_ENABLED = os.getenv("HTML_SNAPSHOT_ENABLED", "1") not in ("0", "false", "False")

# --- Прокси для походов браузера на портал суда -------------------------------

# Сам пул живёт в таблице proxy (правится через админку) — здесь только жёсткий
# запрет ходить мимо него: с пустым пулом задача падает и браузер не запускается.
# Это защита от похода на портал не с того IP. В российском облаке можно 0.
COURT_PROXY_REQUIRED = os.getenv("COURT_PROXY_REQUIRED", "1") not in ("0", "false", "False")

# Сколько последних записей хранить в Case.diff_history: поле JSONB перезаписывается
# целиком при каждом апдейте, поэтому расти без предела ему нельзя.
DIFF_HISTORY_LIMIT = int(os.getenv("DIFF_HISTORY_LIMIT", "200"))
