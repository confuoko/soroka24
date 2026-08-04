"""Снапшоты HTML страниц суда: складываем в S3 то, что реально распарсили.

Зачем: раньше HTML жил только внутри Celery-таска и выбрасывался, поэтому по факту
нельзя было ни перепроверить diff, ни отладить парсер на настоящей разметке.

Раскладка в бакете:
    <бакет>/html_snapshots/<уид дела>/<уид дела>_<время>.html.gz
    <бакет>/html_snapshots/<уид дела>/failed/<уид дела>_<время>.html.gz
Папка на дело + УИД в имени файла: скачанный поодиночке объект остаётся понятным.
gzip — карточка дела весит ~500 КБ текста и сжимается примерно в 8 раз.

Подпапка failed/ — страницы, на которых парсинг упал (капча, блокировка, изменившаяся
разметка). Они лежат внутри папки дела, чтобы всё по делу было в одном префиксе, но
отдельно от карточек: карточки — данные, отказы — материал для разбора инцидента, и
путать их нельзя. Кто перебирает снапшоты дела, должен отфильтровать их через
is_failure_key() — иначе капча приедет в парсер как карточка.

В БД пишется ключ объекта и имя бакета (см. app/monitoring/parse_history.py).
"""
import gzip
import hashlib
from datetime import datetime

from app.config import HTML_SNAPSHOT_PREFIX, S3_BUCKET
from app.storage.s3 import get_object, put_object

# Формат времени в имени объекта: сортируемый и без двоеточий, чтобы файл можно было
# без переименования сохранить на диск (в Windows двоеточие в имени запрещено).
_TS_FORMAT = "%Y-%m-%dT%H-%M-%SZ"

# Подпапка внутри папки дела для страниц, на которых упали.
FAILURE_SUBDIR = "failed"


def snapshot_sha256(html: str) -> str:
    """Хэш содержимого страницы — по нему видно, изменилась ли разметка с прошлого раза."""
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def snapshot_key(uid: str, fetched_at: datetime, failed: bool = False) -> str:
    """Ключ объекта в бакете для этого дела и момента получения страницы.

    failed=True — страница, на которой парсинг упал: кладём в подпапку failed/.
    """
    folder = f"{uid}/{FAILURE_SUBDIR}" if failed else uid
    return f"{HTML_SNAPSHOT_PREFIX}/{folder}/{uid}_{fetched_at.strftime(_TS_FORMAT)}.html.gz"


def is_failure_key(key: str) -> bool:
    """Это ключ страницы-отказа (лежит в подпапке failed/), а не карточки дела?"""
    return f"/{FAILURE_SUBDIR}/" in key


def save_snapshot(uid: str, html: str, fetched_at: datetime, failed: bool = False) -> dict:
    """Сжать HTML и положить в S3.

    failed=True — страница отказа (см. snapshot_key).

    Возвращает {"html_bucket", "html_key", "html_sha256", "html_size"}: бакет, ключ
    объекта, хэш исходного (несжатого) текста и его размер в байтах.
    """
    raw = html.encode("utf-8")
    key = snapshot_key(uid, fetched_at, failed=failed)

    # mtime=0 — чтобы одинаковый HTML давал побайтово одинаковый архив (иначе gzip
    # подмешивает в заголовок текущее время и объекты нельзя сравнивать напрямую).
    put_object(key, gzip.compress(raw, mtime=0))

    return {
        "html_bucket": S3_BUCKET,
        "html_key": key,
        "html_sha256": hashlib.sha256(raw).hexdigest(),
        "html_size": len(raw),
    }


def read_snapshot(key: str) -> str:
    """Прочитать сохранённый снапшот из S3 и распаковать в текст (для отладки)."""
    return gzip.decompress(get_object(key)).decode("utf-8")
