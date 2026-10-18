"""Pure URL builders. No requests are made to user-provided destinations."""

import csv
import io
import re
from collections.abc import Iterable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from leedbox.domain.materials import http_url

UTM_KEYS = ("source", "medium", "campaign", "content", "term")
PRESETS = {
    "yandex": {
        "source": "yandex",
        "medium": "cpc",
        "campaign": "{campaign_id}",
        "content": "{ad_id}",
        "term": "{keyword}",
    },
    "vk": {"source": "vk", "medium": "paid_social", "campaign": "campaign_name"},
    "site": {"source": "source", "medium": "medium", "campaign": "campaign_name"},
}


class ReplacementRequired(ValueError):
    pass


def website_url(value: str) -> str:
    """Accept a pasted domain without weakening validation of explicit URL schemes."""
    value = value.strip()
    if "://" not in value:
        authority = re.split(r"[/?#]", value, maxsplit=1)[0]
        if ":" in authority and not re.fullmatch(r"[^:]+:\d+", authority):
            raise ValueError("Укажите адрес сайта, например example.ru или https://example.ru")
        if "." not in authority.split(":")[0] and authority.split(":")[0] != "localhost":
            raise ValueError("Укажите домен сайта, например example.ru")
        value = "https://" + value
    return http_url(value)


def text_field(value: str, *, required: bool = True, limit: int = 200) -> str:
    value = value.strip()
    if (required and not value) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"Метка: от {'1' if required else '0'} до {limit} символов без переносов")
    return value


def label(value: str, *, required: bool = True, limit: int = 200) -> str:
    return re.sub(r"\s+", "_", text_field(value, required=required, limit=limit))


def existing_utm(url: str) -> bool:
    return any(key.lower().startswith("utm_") for key, _ in parse_qsl(urlsplit(url).query))


def utm_url(url: str, tags: Mapping[str, str | None], *, replace: bool = False) -> str:
    url = http_url(url)
    if len(url) > 4096:
        raise ValueError("Исходная ссылка: не более 4096 символов")
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=200)
    if existing_utm(url) and not replace:
        raise ReplacementRequired("В ссылке уже есть UTM. Подтвердите замену")
    query = [(key, value) for key, value in query if not key.lower().startswith("utm_")]
    for key in UTM_KEYS:
        value = label(tags.get(key) or "", required=key in UTM_KEYS[:3])
        if value:
            query.append((f"utm_{key}", value))
    try:
        host = (parts.hostname or "").encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("Некорректное имя сайта") from None
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host += f":{parts.port}"
    # Preserve documented ad macro braces; all other values are UTF-8 percent encoded.
    result = urlunsplit(
        (
            parts.scheme,
            host,
            quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
            urlencode(query, safe="{}"),
            quote(parts.fragment, safe="/%?:@!$&'()*+,;=-._~"),
        )
    )
    if len(result.encode()) > 4096:
        raise ValueError("Готовая ссылка превышает 4096 байт. Сократите адрес или метки")
    return result


def csv_cell(value: str) -> str:
    # Leading whitespace/control characters must not hide spreadsheet formulas.
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def csv_export(rows: Iterable[Iterable[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    return buffer.getvalue().encode("utf-8-sig")


def deep_link(username: str, payload: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        raise ValueError("Некорректное имя бота")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload):
        raise ValueError("Параметр start: до 64 символов A–Z, a–z, 0–9, _ и -")
    return f"https://t.me/{username}?start={payload}"
