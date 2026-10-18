"""Validation for factual service descriptions and request contacts; no payments."""

import re
from dataclasses import asdict, dataclass
from typing import Any

from leedbox.domain.links import text_field
from leedbox.domain.materials import http_url


def service_text(value: str, limit: int, required: bool = True) -> str:
    clean = value.strip()
    if (
        (required and not clean)
        or len(clean) > limit
        or any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in clean)
    ):
        raise ValueError(f"Введите текст длиной до {limit} символов без управляющих знаков")
    return clean


@dataclass(frozen=True)
class ServiceInput:
    title: str
    audience: str
    description: str
    duration: str
    price_label: str
    executor_details: str
    conditions: str
    sort_order: int = 0
    enabled: bool = False

    def validated(self) -> dict[str, Any]:
        fields = asdict(self)
        for key, limit in (
            ("title", 80),
            ("audience", 300),
            ("description", 1000),
            ("duration", 150),
            ("price_label", 150),
            ("executor_details", 500),
            ("conditions", 500),
        ):
            fields[key] = service_text(fields[key], limit)
        if type(self.sort_order) is not int or not -10000 <= self.sort_order <= 10000:
            raise ValueError("Порядок должен быть целым числом от -10000 до 10000")
        if type(self.enabled) is not bool:
            raise ValueError("Некорректное состояние публикации")
        return fields


def request_contact(value: str) -> str:
    clean = text_field(value, limit=200)
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", clean):
        return clean
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", clean):
        return clean
    if re.fullmatch(r"\+?[0-9 ()-]{7,30}", clean) and 7 <= sum(c.isdigit() for c in clean) <= 15:
        return clean
    raise ValueError("Введите @username, email или номер телефона")


def owner_contact_url(value: str) -> str | None:
    clean = value.strip().removeprefix("@")
    if not clean:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", clean):
        raise ValueError("Контакт владельца: Telegram username без ссылки")
    return f"https://t.me/{clean}"


def storefront_url(value: str, contact: bool = False) -> str | None:
    clean = value.strip()
    if not clean:
        return None
    if contact and clean.startswith("@"):
        return owner_contact_url(clean)
    return http_url(clean)


STATUS_LABELS = {"new": "Новая", "working": "В работе", "closed": "Закрыта"}
