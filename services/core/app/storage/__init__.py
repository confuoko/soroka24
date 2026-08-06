# Хранилища данных, которые не помещаются в БД (снапшоты HTML и картинки капчи в S3).
from app.storage.captcha_images import captcha_key, save_captcha
from app.storage.html_snapshots import (
    case_id_from_url,
    is_failure_key,
    read_snapshot,
    save_snapshot,
    snapshot_key,
    snapshot_sha256,
    url_label,
)
from app.storage.s3 import get_object, list_keys, put_object

__all__ = [
    "save_snapshot",
    "read_snapshot",
    "snapshot_key",
    "snapshot_sha256",
    "is_failure_key",
    "url_label",
    "case_id_from_url",
    "save_captcha",
    "captcha_key",
    "put_object",
    "get_object",
    "list_keys",
]
