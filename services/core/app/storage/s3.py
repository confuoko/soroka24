"""Клиент S3-совместимого хранилища (локально — MinIO, в проде — облачный S3).

Вся работа с boto3 собрана здесь, чтобы остальной код знал только «положи объект по
ключу» / «прочитай объект по ключу» и не зависел от конкретного SDK.
"""
from functools import lru_cache

import boto3
from botocore.client import Config

from app.config import (
    S3_ACCESS_KEY,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_REGION,
    S3_SECRET_KEY,
)


@lru_cache(maxsize=1)
def get_client():
    """Клиент S3 (один на процесс: boto3-клиент потокобезопасен и переиспользуется).

    signature_version=s3v4 и path-style адресация — то, что нужно MinIO; облачный S3
    их тоже принимает, поэтому одна настройка работает в обоих случаях.
    """
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def put_object(key: str, body: bytes, content_type: str = "application/gzip") -> None:
    """Положить объект в бакет по ключу (перезаписывает существующий)."""
    get_client().put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType=content_type)
