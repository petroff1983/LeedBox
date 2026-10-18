"""Small curated storefront using versioned settings and existing service commands."""

import re
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.partners import PartnerInput
from leedbox.application.services import save_service
from leedbox.application.settings import set_setting, setting
from leedbox.application.ui_texts import TEXTS, read_text, set_text
from leedbox.application.versions import fingerprint
from leedbox.domain.services import ServiceInput
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import Service, User

PRODUCTS = [
    (
        "syntx",
        "🤖 ИИ в Telegram",
        "Тексты и другие ИИ-инструменты в Telegram.",
        "https://t.me/syntxaibot?start=aff_395181636",
    ),
    (
        "socrat",
        "🛍 Подписки на сервисы",
        "Бот для покупки подписок. Условия и цены — в сервисе.",
        "https://t.me/SocratStore_bot?start=395181636",
    ),
    (
        "zarub",
        "💳 Оплата зарубежных сервисов",
        "Пользуюсь для оплаты зарубежных сервисов: без ежемесячной платы за обслуживание карты. "
        "Перед покупкой проверьте актуальные условия и совместимость с нужным сервисом.",
        "https://t.me/zarub_robot?start=ref_yF44lQ",
    ),
    (
        "perplexity",
        "🔎 Perplexity Pro",
        "Подписка для поиска и работы с источниками через ИИ.",
        "https://plati.market/itm/perplexity-ai-pro-podpiska-1-12-mesjacev-bez-vpn/4366461?ai=1416125",
    ),
    (
        "google",
        "✨ Google AI Pro · 6 месяцев",
        "Подписка Google AI Pro. Состав и условия — у продавца.",
        "https://plati.market/itm/24-7-instant-google-ai-pro-6-months-on-your-mail-gemini-3-1-pro-5tb/5816660?ai=1416125",
    ),
    (
        "claude",
        "🧠 Claude Pro / Max / Code",
        "Подписки Anthropic для работы с Claude и кодом.",
        "https://plati.market/itm/4023986?ai=1416125",
    ),
    (
        "protalk",
        "🧩 Ассистенты без кода",
        "Сервис для создания ИИ-ассистентов без программирования.",
        "https://pro-talk.ru/?ref=rec7szVu3cIYMgdHq",
    ),
]


def validate_products(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 50:
        raise ValueError("Каталог: не более 50 товаров")
    clean, codes = [], set()
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("Некорректная карточка товара")
        code = item.get("code", "")
        order = item.get("sort_order", 0)
        if (
            not isinstance(code, str)
            or not re.fullmatch(r"[a-z0-9_-]{1,32}", code)
            or code in codes
            or type(order) is not int
            or not -10000 <= order <= 10000
        ):
            raise ValueError("Проверьте идентификатор и порядок товара")
        fields = set(PartnerInput.__dataclass_fields__)
        if set(item) != fields | {"code", "sort_order"}:
            raise ValueError("Некорректные поля товара")
        data = PartnerInput(**{key: item[key] for key in fields}).validated()
        if len(data["name"]) > 64:
            raise ValueError("Название кнопки товара: до 64 символов")
        clean.append({**data, "code": code, "sort_order": order})
        codes.add(code)
    return clean


def products(session: Session, preview: bool = False) -> list[dict[str, Any]]:
    if not setting(session, "products_enabled") and not preview:
        return []
    result = []
    for item in setting(session, "store_products"):
        try:
            checked = validate_products([item])[0]
        except (ValueError, TypeError):
            continue  # A malformed manual override must never publish unmarked advertising.
        if checked["enabled"] or preview:
            result.append(checked)
    return sorted(result, key=lambda item: (item["sort_order"], item["code"]))


def install(session: Session, config: Settings, actor: int) -> None:
    """Explicit, repeat-safe initial content; never overwrite owner edits."""
    if not config.is_owner(actor):
        raise PermissionError("Owner access required")
    if setting(session, "storefront_seed_version") >= 1:
        return
    for key in ("services_intro", "service_catalog"):
        set_text(session, config, actor, key, TEXTS[key], fingerprint(read_text(session, key)))
    if not setting(session, "store_products"):
        values = [
            {**asdict(PartnerInput(name, description, url)), "code": code, "sort_order": order}
            for order, (code, name, description, url) in enumerate(PRODUCTS)
        ]
        set_setting(session, config, actor, "store_products", values)
    definitions = [
        (
            "🤖 Telegram-бот",
            "От 10 000 ₽",
            "3–5 дней",
            "✅ Готовый бот под вашу задачу\n✅ Сервер + настройка\n✅ 30 дней поддержки",
        ),
        (
            "🤖 ИИ-агент",
            "От 20 000 ₽",
            "7–14 дней",
            "✅ ИИ-агент под вашу задачу\n✅ Сервер + настройка\n✅ 30 дней поддержки",
        ),
        (
            "✍️ Разработка сложных промптов",
            "От 5 000 ₽",
            "3–5 дней",
            "✅ Промпт под вашу задачу и выбранную нейросеть\n"
            "✅ Проверка на примерах и доработка\n✅ Инструкция по использованию",
        ),
        (
            "🤖 Готовый ИИ-сервис",
            "От 10 000 ₽",
            "3–5 дней",
            "✅ Рабочий ИИ-сервис под вашу задачу\n✅ Сервер + настройка\n✅ 30 дней поддержки",
        ),
    ]
    for order, (title, price, duration, description) in enumerate(definitions):
        if session.scalar(select(Service.id).where(Service.title == title)):
            continue
        save_service(
            session,
            config,
            actor,
            ServiceInput(
                title,
                "Для экспертов и бизнеса",
                description,
                duration,
                price,
                "Владелец LeedBox",
                "Объём, точная стоимость и срок согласуются до начала работы.",
                order,
                True,
            ),
        )
    if not setting(session, "owner_contact"):
        username = session.scalar(select(User.username).where(User.telegram_id == actor))
        if username:
            set_setting(session, config, actor, "owner_contact", username)
    set_setting(session, config, actor, "storefront_seed_version", 1)
