"""Снапшоты HTML страниц суда: складываем в S3 то, что реально распарсили.

Зачем: раньше HTML жил только внутри Celery-таска и выбрасывался, поэтому по факту
нельзя было ни перепроверить diff, ни отладить парсер на настоящей разметке.

Раскладка в бакете:
    <бакет>/html_snapshots/<уид дела>/<уид дела>_<время>.html.gz
Папка на дело + УИД в имени файла: скачанный поодиночке объект остаётся понятным.
gzip — карточка дела весит ~500 КБ текста и сжимается примерно в 8 раз.

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


def snapshot_sha256(html: str) -> str:
    """Хэш содержимого страницы — по нему видно, изменилась ли разметка с прошлого раза."""
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def snapshot_key(uid: str, fetched_at: datetime) -> str:
    """Ключ объекта в бакете для этого дела и момента получения страницы."""
    return f"{HTML_SNAPSHOT_PREFIX}/{uid}/{uid}_{fetched_at.strftime(_TS_FORMAT)}.html.gz"


def save_snapshot(uid: str, html: str, fetched_at: datetime) -> dict:
    """Сжать HTML и положить в S3.

    Возвращает {"html_bucket", "html_key", "html_sha256", "html_size"}: бакет, ключ
    объекта, хэш исходного (несжатого) текста и его размер в байтах.
    """
    raw = html.encode("utf-8")
    key = snapshot_key(uid, fetched_at)

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
