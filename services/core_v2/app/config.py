"""Чтение конфигурации из переменных окружения.

Простые модульные константы: ни классов, ни pydantic-settings. Прочитать сверху вниз и
увидеть весь набор настроек — важнее, чем валидация типов, которой здесь нечего проверять.

Всё читается ОДИН РАЗ при импорте. Значит, переменную окружения нельзя подменить после
импорта модуля — в тестах это делается через monkeypatch самой константы.
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

# --- Публикация изменений наружу (integration events) -------------------------

# Брокер, в который уезжают сообщения об изменениях. По умолчанию тот же RabbitMQ, что и
# у Celery, но переменная СВОЯ, и это не дублирование ради дублирования: очередь
# case_changes к Celery отношения не имеет (там обычный JSON, а не celery-протокол), и
# однажды её может понадобиться увести на отдельный брокер, не трогая очереди задач.
INTEGRATION_BROKER_URL = os.getenv("INTEGRATION_BROKER_URL", CELERY_BROKER_URL)

# Exchange и очередь для сообщений об изменениях.
#
# Отдельный exchange, а не публикация в очередь напрямую: завтра к тому же потоку
# захочется привязать вторую очередь (Telegram-бот, аналитика), и с exchange это одна
# строка на стороне подписчика, а не переделка publisher'а.
#
# Имена читаются из env, потому что вторая сторона (клиентский сервис) — отдельный
# репозиторий деплоя, и держать их синхронными проще через общий .env, чем через
# одинаковые константы в двух кодовых базах.
CASE_CHANGES_EXCHANGE = os.getenv("CASE_CHANGES_EXCHANGE", "soroka.case_changes")
CASE_CHANGES_QUEUE = os.getenv("CASE_CHANGES_QUEUE", "case_changes")

# Как часто publisher заглядывает в таблицу, когда там пусто, секунды.
#
# Опрос, а не LISTEN/NOTIFY: секунда задержки на пути «портал → пользователь», где сам
# обход занимает 25-35 секунд, не значит ничего, а опрос по частичному индексу пустой
# таблицы стоит примерно ничего. Когда окажется, что значит, — здесь и поменяем.
PUBLISHER_POLL_SECONDS = float(os.getenv("PUBLISHER_POLL_SECONDS", "1.0"))

# Сколько сообщений забирать за раз. Полная порция означает «в таблице есть ещё» —
# publisher тогда идёт за следующей сразу, не выжидая PUBLISHER_POLL_SECONDS.
PUBLISHER_BATCH_SIZE = int(os.getenv("PUBLISHER_BATCH_SIZE", "100"))

# --- Регулярный обход дел на мониторинге --------------------------------------

# Час ночного прогона (UTC — у Celery enable_utc=True). Три часа выбраны не случайно:
# порталы судов в это время свободны, а капча на них разгадывается заметно легче.
MONITORING_HOUR = int(os.getenv("MONITORING_HOUR", "3"))

# Пауза между постановками дел в очередь, секунды.
#
# Обязательна, и не ради вежливости к порталу. Один поход — это 25-35 секунд работы
# браузера, аренда прокси из ограниченного пула и ОПЛАЧЕННАЯ капча. Без разноса тысяча
# дел уходит в очередь одновременно, воркеры разбирают её вперегонки, прокси кончаются,
# и деньги за капчу тратятся на попытки, которые всё равно упадут по таймауту.
MONITORING_SPACING_SECONDS = int(os.getenv("MONITORING_SPACING_SECONDS", "60"))

# Сколько дел брать за один прогон. 0 — все.
#
# Лимит осмыслен вместе с сортировкой в list_monitored_ids: она ставит вперёд самые давно
# не проверявшиеся, поэтому урезанная выборка не оставляет хвост списка без обхода
# навсегда — обойдённое уезжает в конец очереди само.
MONITORING_BATCH_LIMIT = int(os.getenv("MONITORING_BATCH_LIMIT", "0"))

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
