# Хранилища данных, которые не помещаются в БД (архив HTML страниц и картинки капчи в S3).
from app.storage.captcha_images import captcha_key, save_captcha
from app.storage.html_snapshots import (
    card_folder,
    case_id_from_url,
    save_snapshot,
    snapshot_key,
)
from app.storage.s3 import put_object

__all__ = [
    "save_snapshot",
    "snapshot_key",
    "card_folder",
    "case_id_from_url",
    "save_captcha",
    "captcha_key",
    "put_object",
]
