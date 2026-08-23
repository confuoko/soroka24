"""Ключи картинок капчи в S3.

Раскладка идёт по ссылке, а не по УИД: капча показывается ДО карточки дела, то есть
в этот момент УИД ещё неизвестен — за ним мы, собственно, и шли.
"""
from datetime import datetime

from app.storage import captcha_key
from app.storage.captcha_images import save_captcha

TAKEN_AT = datetime(2026, 8, 6, 14, 21, 5)
CASE_URL = (
    "https://95.mo.msudrf.ru/modules.php?name=sud_delo&op=cs"
    "&case_id=429386415&delo_id=1540005"
)


def test_key_is_built_from_host_and_case_id() -> None:
    """Папка — хост участка и case_id: по ключу сразу видно, чьё это дело."""
    assert captcha_key(CASE_URL, TAKEN_AT) == (
        "captcha/95.mo.msudrf.ru/429386415/2026-08-06T14-21-05Z.png"
    )


def test_key_has_no_colons() -> None:
    """Время без двоеточий: иначе объект нельзя сохранить на диск под Windows."""
    assert ":" not in captcha_key(CASE_URL, TAKEN_AT).rsplit("/", 1)[-1]


def test_url_without_case_id_falls_back_to_hash() -> None:
    """Ссылка непривычного вида не должна ронять сохранение картинки."""
    key = captcha_key("https://95.mo.msudrf.ru/modules.php?name=sud_delo", TAKEN_AT)

    assert key.startswith("captcha/95.mo.msudrf.ru/url-")
    assert key.endswith(".png")


def test_same_url_gives_same_folder() -> None:
    """Две капчи одного дела ложатся в одну папку и различаются только временем."""
    first = captcha_key(CASE_URL, datetime(2026, 8, 6, 14, 0, 0))
    second = captcha_key(CASE_URL, datetime(2026, 8, 6, 14, 5, 0))

    assert first.rsplit("/", 1)[0] == second.rsplit("/", 1)[0]
    assert first != second


def test_storage_failure_is_swallowed(monkeypatch) -> None:
    """S3 недоступен → возвращаем None и идём разгадывать дальше.

    Не смогли отложить картинку — не повод отказываться от прохождения проверки,
    ради которого весь поход и затевался.
    """
    from app.storage import captcha_images

    def _boom(*args, **kwargs):
        raise RuntimeError("minio лёг")

    monkeypatch.setattr(captcha_images, "put_object", _boom)

    assert save_captcha(CASE_URL, b"png", TAKEN_AT) is None


def test_successful_save_reports_key_and_size(monkeypatch) -> None:
    """При удачной записи возвращаем ключ и размер — их видно в логах и отчёте скрипта."""
    from app.storage import captcha_images

    recorded = {}

    def _put(key, body, content_type=None):
        recorded.update(key=key, body=body, content_type=content_type)

    monkeypatch.setattr(captcha_images, "put_object", _put)

    result = save_captcha(CASE_URL, b"png-bytes", TAKEN_AT)

    assert result["captcha_key"] == captcha_key(CASE_URL, TAKEN_AT)
    assert result["captcha_size"] == len(b"png-bytes")
    # content_type важен: иначе картинка в консоли MinIO не откроется, а скачается.
    assert recorded["content_type"] == "image/png"
