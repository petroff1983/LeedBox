"""Owner-uploaded, immutable banner assets; no URL fetching or arbitrary file paths."""

import hashlib
import re
from importlib.resources import files
from pathlib import Path

from leedbox.domain.materials import http_url

MAX_BANNER_BYTES = 5 * 1024 * 1024
UPLOADED = re.compile(r"upload:([a-f0-9]{64}\.(?:png|jpg))")


def validate_banner(value: str) -> str:
    if value in {"default", "off"} or UPLOADED.fullmatch(value):
        return value
    if value.startswith(("http://", "https://")):
        return http_url(value)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,512}", value):
        return value  # Telegram photo file_id, never interpreted as a filesystem path.
    raise ValueError(
        "Выберите файл PNG/JPEG, укажите HTTP/HTTPS URL или Telegram file_id. "
        "default — стандартный баннер; off — без картинки."
    )


def image_extension(data: bytes) -> str:
    if not data or len(data) > MAX_BANNER_BYTES:
        raise ValueError("Изображение должно быть не больше 5 МБ")
    width = height = 0
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33 and data[12:16] == b"IHDR":
        width, height = int.from_bytes(data[16:20]), int.from_bytes(data[20:24])
        extension = "png"
    elif data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        extension = "jpg"
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 255:
                break
            marker = data[offset + 1]
            if marker == 255:
                offset += 1
                continue
            length = int.from_bytes(data[offset + 2 : offset + 4])
            if length < 2 or offset + length + 2 > len(data):
                break
            if marker in {192, 193, 194} and length >= 8:
                height = int.from_bytes(data[offset + 5 : offset + 7])
                width = int.from_bytes(data[offset + 7 : offset + 9])
                break
            offset += length + 2
    else:
        raise ValueError("Загрузите PNG или JPEG, не SVG и не документ")
    if (
        not width
        or not height
        or width + height > 10000
        or max(width, height) / min(width, height) > 20
    ):
        raise ValueError(
            "Размеры фото не подходят: сумма сторон до 10 000 пикселей, соотношение сторон до 20:1"
        )
    return extension


def save_banner(data: bytes, directory: Path) -> str:
    extension = image_extension(data)
    name = hashlib.sha256(data).hexdigest() + "." + extension
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    try:
        with path.open("xb") as target:
            target.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError("Файл баннера повреждён. Загрузите другую картинку") from None
    return "upload:" + name


def banner_source(value: str, directory: Path) -> Path | str | None:
    validate_banner(value)
    if value == "off":
        return None
    if value == "default":
        path = Path(str(files("leedbox").joinpath("assets/menu-banner.png")))
        return path if path.is_file() else None
    uploaded = UPLOADED.fullmatch(value)
    if uploaded:
        path = directory / uploaded[1]
        return path if path.is_file() else None
    return value


def consultation_source(value: str, directory: Path) -> Path | str | None:
    if value == "default":
        path = Path(str(files("leedbox").joinpath("assets/consultation-portrait-v1.png")))
        return path if path.is_file() else None
    return banner_source(value, directory)
