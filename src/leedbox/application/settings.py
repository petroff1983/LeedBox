"""Typed operational defaults, all overridable without restarting."""

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.sections import VISIBILITY_DEFAULTS
from leedbox.application.versions import fingerprint, lock_content, record_version
from leedbox.domain import ai_prompts
from leedbox.domain.content_agent import DRAFT, EDIT, PLAN
from leedbox.domain.dzen import ARTICLE, SYSTEM
from leedbox.domain.image_prompts import SYSTEM as IMAGE_SYSTEM
from leedbox.domain.lead_magnet_agent import CRITIC, GENERATE, REVISE
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AuditLog, Setting

DEFAULTS: dict[str, Any] = {
    "legal_gate_enabled": False,
    **VISIBILITY_DEFAULTS,
    "ai_dzen_enabled": False,
    "ai_images_enabled": True,
    "ai_content_enabled": True,
    "ai_lead_magnet_enabled": True,
    "ai_lead_magnet_generate_prompt": GENERATE,
    "ai_lead_magnet_critic_prompt": CRITIC,
    "ai_lead_magnet_revise_prompt": REVISE,
    "ai_content_plan_prompt": PLAN,
    "ai_content_draft_prompt": DRAFT,
    "ai_content_edit_prompt": EDIT,
    "ai_content_search_tokens": 400000,
    "ai_content_apify_budget_usd": "0.04",
    "ai_images_prompt": IMAGE_SYSTEM,
    "ai_dzen_prompt": SYSTEM,
    "ai_dzen_article_template": ARTICLE,
    "ai_token_packages": [],
    "ai_token_price_rub": "0.0003",
    "ai_credits_enabled": False,
    "ai_credit_price_rub": "1.00",
    **{
        "ai_credit_cost_" + key: value
        for key, value in {
            "dzen_search": 3,
            "dzen": 5,
            "writing_profile": 12,
            "image_prompts": 10,
            "lead_magnet": 15,
            "lead_magnet_revision": 15,
            "content_analysis": 30,
            "content_post": 30,
            "youtube_search": 5,
            "content_search": 18,
        }.items()
    },
    "finance_fee_percent": "0",
    "finance_tax_percent": "0",
    "finance_server_monthly_rub": "0",
    "zenstat_package_price_rub": "500.00",
    "zenstat_package_requests": 1000,
    "youtube_quota_limit": 10000,
    "zenstat_low_requests": 100,
    "zenstat_balance_check_hours": 24,
    "ai_token_purchase_min_rub": "200.00",
    "wallet_topup_min_rub": "200.00",
    "ai_free_dzen": 1,
    "ai_free_image_prompts": 1,
    "ai_free_lead_magnet": 1,
    "ai_free_content_search": 1,
    "ai_free_youtube_search": 1,
    "ai_youtube_search_tokens": 1000,
    "ai_provider_low_usd": "2.00",
    "ai_provider_low_cny": "15.00",
    "ai_dzen_search_tokens": 4000,
    "zenstat_request_cost_rub": "0.50",
    "ai_input_usd_per_million": "0.30",
    "ai_output_usd_per_million": "1.20",
    "ai_usd_rub": "85",
    "main_menu_banner": "default",
    "store_products": [],
    "storefront_seed_version": 0,
    "products_enabled": True,
    "consultations_enabled": True,
    "consultation_photo": "default",
    "consultation_contact_url": "",
    "consultation_channel_url": "",
    "consultation_blog_url": "",
    **{"prompt_template_" + tool: ai_prompts.default_template(tool) for tool in ai_prompts.FIELDS},
    "stars_access_price": 0,
    "wallet_visible": False,
    "wallet_enabled": False,
    "wallet_topup_enabled": False,
    "wallet_receipt_template": "",
    "wallet_sbp_alias": "",
    "payment_terms": "",
    "offer_ad_limit": 160,
    "offer_button_limit": 64,
    "active_material_limit": 50,
    "notification_interval_days": 7,
    "rate_requests": 120,
    "rate_window_seconds": 60,
    "subscription_cooldown_seconds": 5,
    "materials_enabled": True,
    "services_enabled": True,
    "recommendations_enabled": False,
    "owner_contact": "",
    "service_contact_url": "",
    "service_about_url": "",
    "owner_details": "",
    "service_consent_text": (
        "Я согласен передать владельцу сервиса выбранные услуги, контакт и комментарий "
        "для обработки этой заявки. Согласие не распространяется на рекламные рассылки."
    ),
    "author_consent_version": "beta-1",
    "author_consent_text": (
        "Я согласен получать от автора уведомления об обновлении этого материала. "
        "Согласие можно отозвать в любой момент. Частота ограничена настройками сервиса."
    ),
}


def setting(session: Session, key: str) -> Any:
    row = session.get(Setting, key)
    return row.value if row else DEFAULTS[key]


def set_setting(
    session: Session,
    settings: Settings,
    actor: int,
    key: str,
    value: Any,
    expected: str | None = None,
) -> None:
    if not settings.is_owner(actor):
        raise PermissionError("Owner access required")
    if key not in DEFAULTS:
        raise ValueError("Unknown setting")
    default = DEFAULTS[key]
    if type(value) is not type(default):
        raise ValueError("Invalid setting type")
    if (
        isinstance(default, int)
        and not isinstance(default, bool)
        and not (
            (0 if key == "stars_access_price" or key.startswith("ai_free_") else 1)
            <= value
            <= (
                1000000
                if key
                in {"ai_content_search_tokens", "ai_dzen_search_tokens", "ai_youtube_search_tokens"}
                else 100000
            )
        )
    ):
        if key == "stars_access_price":
            raise ValueError("Цена доступа: от 0 до 100000 Stars; 0 отключает покупку")
        raise ValueError("Invalid limit")
    if key == "legal_gate_enabled" and value:
        from leedbox.application.legal import require_ready

        require_ready(session, settings)
    if key == "notification_interval_days" and value < 7:
        raise ValueError("Уведомления автора: интервал не менее семи дней")
    if key == "author_consent_text" and (not value.strip() or len(value) > 1500):
        raise ValueError("Текст согласия: от 1 до 1500 символов")
    if key == "ai_token_packages":
        from leedbox.application.agents import validate_packages

        value = validate_packages(value)
    elif key == "ai_dzen_article_template":
        from leedbox.domain.dzen import validate_article_template

        validate_article_template(value)
    elif key in {"finance_fee_percent", "finance_tax_percent", "finance_server_monthly_rub"}:
        from decimal import Decimal, InvalidOperation

        try:
            number = Decimal(value)
            ceiling = 49 if key.endswith("percent") else 10000000
            if not number.is_finite() or not 0 <= number <= ceiling:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("Некорректная ставка или сумма расходов") from None
    elif key in {
        "ai_dzen_prompt",
        "ai_images_prompt",
        "ai_content_plan_prompt",
        "ai_content_draft_prompt",
        "ai_content_edit_prompt",
        "ai_lead_magnet_generate_prompt",
        "ai_lead_magnet_critic_prompt",
        "ai_lead_magnet_revise_prompt",
    }:
        if not 100 <= len(value) <= 10000 or any(marker in value for marker in ("{{", "{%")):
            raise ValueError("Инструкция ИИ: 100–10000 символов, без переменных")
    elif key in {
        "ai_credit_price_rub",
        "zenstat_package_price_rub",
        "ai_token_price_rub",
        "ai_token_purchase_min_rub",
        "wallet_topup_min_rub",
        "ai_input_usd_per_million",
        "ai_output_usd_per_million",
        "ai_usd_rub",
        "ai_provider_low_usd",
        "ai_provider_low_cny",
        "zenstat_request_cost_rub",
        "ai_content_apify_budget_usd",
    }:
        from decimal import Decimal, InvalidOperation

        try:
            number = Decimal(value)
            if not number.is_finite() or not 0 < number <= 10000:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError("Введите положительную десятичную стоимость или курс") from None
        if key == "ai_credit_price_rub" and number != number.quantize(Decimal("0.01")):
            raise ValueError("Цена кредита должна быть кратна одной копейке")
    elif key.startswith("prompt_template_"):
        ai_prompts.validate_template(key.removeprefix("prompt_template_"), value)
    elif key == "store_products":
        from leedbox.application.storefront import validate_products

        value = validate_products(value)
    elif isinstance(value, str) and len(value) > 3000:
        if key == "payment_terms":
            raise ValueError("Условия покупки и возврата: до 3000 символов")
        raise ValueError("Setting too long")
    if key == "owner_details" and len(value) > 500:
        raise ValueError("Сведения владельца: до 500 символов")
    if key == "service_consent_text" and (not value.strip() or len(value) > 1500):
        raise ValueError("Текст согласия: от 1 до 1500 символов")
    if key == "author_consent_version" and (not value.strip() or len(value) > 40):
        raise ValueError("Версия согласия: от 1 до 40 символов")
    if key == "owner_contact":
        from leedbox.domain.services import owner_contact_url

        owner_contact_url(value)
    if key in {"main_menu_banner", "consultation_photo"}:
        from leedbox.infrastructure.banner_assets import validate_banner

        value = validate_banner(value)
    if key in {
        "service_contact_url",
        "service_about_url",
        "consultation_contact_url",
        "consultation_channel_url",
        "consultation_blog_url",
    }:
        from leedbox.domain.services import storefront_url

        storefront_url(
            value,
            contact=key
            in {"service_contact_url", "consultation_contact_url", "consultation_channel_url"},
        )
    lock_content(session, "setting", key)
    before = setting(session, key)
    if expected is not None and fingerprint(before) != expected:
        raise ValueError("Настройка изменилась. Откройте её заново")
    if before == value:
        return
    if key == "ai_credits_enabled":
        from leedbox.application import credits

        if not value and before:
            raise ValueError("Обратное переключение единиц баланса запрещено")
        if value:
            credits.activate(session, actor)
    session.execute(
        insert(Setting)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": value})
    )
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="setting_changed",
            details={
                "key": key,
                "before": before,
                "after": value,
            },
        )
    )
    record_version(session, "setting", key, actor, {"value": value}, {"value": before})
    if key in {"zenstat_package_price_rub", "zenstat_package_requests"}:
        from decimal import Decimal

        package_price = (
            value
            if key == "zenstat_package_price_rub"
            else setting(session, "zenstat_package_price_rub")
        )
        requests = (
            value
            if key == "zenstat_package_requests"
            else setting(session, "zenstat_package_requests")
        )
        cost = Decimal(str(package_price)) / int(requests)
        set_setting(
            session,
            settings,
            actor,
            "zenstat_request_cost_rub",
            str(cost.quantize(Decimal("0.000001"))),
        )
    if key == "author_consent_text":
        set_setting(
            session, settings, actor, "author_consent_version", "text-" + fingerprint(value)[:35]
        )


def product_event(
    session: Session,
    bot_id: int,
    user_id: int | None,
    name: str,
    material_id: int | None = None,
    **details: Any,
) -> None:
    from leedbox.infrastructure.models import ProductEvent

    if set(details) - {
        "reason",
        "state",
        "previous",
        "tool",
        "step",
        "version",
        "mode",
        "kind",
        "partner_id",
        "article_id",
    }:
        raise ValueError("Product events must not contain user input")
    for key in ("partner_id", "article_id"):
        if key in details and (type(details[key]) is not int or not 0 < details[key] <= 2**63 - 1):
            raise ValueError("Analytics catalog IDs must be numeric")
    session.add(
        ProductEvent(
            bot_id=bot_id,
            telegram_user_id=user_id,
            name=name,
            material_id=material_id,
            details=details,
        )
    )
