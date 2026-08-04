# Хранилища данных, которые не помещаются в БД (снапшоты HTML в S3).
from app.storage.html_snapshots import (
    is_failure_key,
    read_snapshot,
    save_snapshot,
    snapshot_key,
    snapshot_sha256,
)
from app.storage.s3 import get_object, list_keys, put_object

__all__ = [
    "save_snapshot",
    "read_snapshot",
    "snapshot_key",
    "snapshot_sha256",
    "is_failure_key",
    "put_object",
    "get_object",
    "list_keys",
]
