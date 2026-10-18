"""Fixed-action quotes; purchased value and provider tokens are separate units."""

from collections.abc import Callable
from decimal import ROUND_CEILING, Decimal
from functools import wraps
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.settings import setting
from leedbox.infrastructure.models import AIAccount, AIOperation, AuditLog

TARIFFS = {
    "dzen_search": ("Дзен: поиск топ-5", 1),
    "dzen": ("Дзен: пять заголовков", 1),
    "writing_profile": ("Подробный промпт статьи", 1),
    "image_prompts": ("Пять промптов картинок", 1),
    "lead_magnet": ("Пять тем лид-магнитов", 1),
    "lead_magnet_revision": ("Критика и доработка лид-магнитов", 1),
    "content_analysis": ("Анализ паттернов", 1),
    "content_post": ("Один пост по паттернам", 1),
    "youtube_search": ("YouTube: анализ канала", 1),
    "content_search": ("Telegram: сбор 20, выдача до 10", 8),
}


def enabled(db: Session) -> bool:
    return bool(setting(db, "ai_credits_enabled"))


def unit_price(db: Session) -> Decimal:
    return Decimal(str(setting(db, "ai_credit_price_rub")))


def price(db: Session, task: str) -> int:
    return int(setting(db, "ai_credit_cost_" + task))


def quoted_value(db: Session, offer: dict[str, Any]) -> dict[str, Any]:
    if not enabled(db):
        return offer
    task = str(offer.get("purpose", "dzen"))
    count = 1
    if task == "content":
        task = "content_post" if offer.get("content_phase") == "generation" else "content_analysis"
        if task == "content_post":
            from leedbox.domain.content_agent import requested_topics

            count = len(requested_topics(offer.get("requests", [])))
            if not 1 <= count <= 10:
                raise ValueError("Укажите количество постов от 1 до 10")
    return {
        **offer,
        "credit_quote": {
            "unit": "credits",
            "task": task,
            "count": count,
            "amount": price(db, task) * count,
            "unit_price_rub": str(unit_price(db)),
        },
    }


def quoted[F: Callable[..., dict[str, Any]]](fn: F) -> F:
    @wraps(fn)
    def wrapped(db: Session, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return quoted_value(db, fn(db, *args, **kwargs))

    return cast(F, wrapped)


def amount(offer: dict[str, Any]) -> int:
    return (
        int(offer["credit_quote"]["amount"]) if "credit_quote" in offer else int(offer["maximum"])
    )


def is_credit(offer: dict[str, Any] | None) -> bool:
    return bool((offer or {}).get("credit_quote", {}).get("unit") == "credits")


def available(funds: AIAccount, credit: bool) -> int:
    return (
        funds.credit_balance - funds.credit_reserved if credit else funds.balance - funds.reserved
    )


def charge(funds: AIAccount, row: AIOperation, legacy_amount: int) -> None:
    if row.billing_mode != "balance":
        return
    if not is_credit(row.snapshot):
        funds.balance -= legacy_amount
        return
    value = amount(row.snapshot)
    # Weighted carrying value: changing the selling price never reprices old credits.
    revenue = (
        funds.credit_value_rub
        if value == funds.credit_balance
        else (funds.credit_value_rub * value / funds.credit_balance).quantize(Decimal("0.000001"))
    )
    funds.credit_balance -= value
    funds.credit_value_rub -= revenue
    row.charged_credits, row.revenue_rub = value, revenue


def api_ceiling(db: Session, offer: dict[str, Any]) -> Decimal:
    """Conservative full-operation bound; byte-based input bound also includes repairs."""
    task = offer.get("purpose", "dzen")
    if task == "dzen_search":
        return Decimal(str(offer.get("provider_cost_rub", setting(db, "zenstat_request_cost_rub"))))
    if task == "youtube_search":
        return Decimal(0)
    if task == "content_search":
        return min(
            Decimal("0.04"),
            Decimal(str(offer.get("apify_budget", setting(db, "ai_content_apify_budget_usd")))),
        ) * Decimal(str(offer.get("rates", {}).get("usd_rub", setting(db, "ai_usd_rub"))))
    rates = offer.get("rates", {})
    incoming = int(offer["maximum"])
    if offer.get("limits"):
        limits = offer["limits"]
        stages = offer.get("stages", list(range(len(limits))))
        outgoing = sum(limits[i] for i in stages)
        if offer.get("max_repairs"):
            outgoing += max(limits)
    else:
        outgoing = int(offer.get("output_limit", 2200))
    return (
        (
            Decimal(incoming)
            * Decimal(str(rates.get("input", setting(db, "ai_input_usd_per_million"))))
            + Decimal(outgoing)
            * Decimal(str(rates.get("output", setting(db, "ai_output_usd_per_million"))))
        )
        / 1000000
        * Decimal(str(rates.get("usd_rub", setting(db, "ai_usd_rub"))))
    )


REFERENCE_USAGE = {
    # Representative completed-operation usage with a 20% safety allowance.
    # This is a pricing check, not a customer token reservation; actual API cost
    # is recorded and shown in owner finance reports.
    "dzen": (807, 534),
    "writing_profile": (3000, 2000),
    "image_prompts": (900, 500),
    "lead_magnet": (901, 740),
    "lead_magnet_revision": (3208, 1098),
    "content_analysis": (3453, 384),
    "content_post": (3584, 2061),
}


def ensure_margin(db: Session, offer: dict[str, Any]) -> None:
    """Require 100% markup over representative cost, not worst-case reserve."""
    if not is_credit(offer):
        return
    task = str(offer.get("credit_quote", {}).get("task", ""))
    revenue = amount(offer) * Decimal(offer["credit_quote"]["unit_price_rub"])
    fraction = (
        Decimal(str(setting(db, "finance_fee_percent")))
        + Decimal(str(setting(db, "finance_tax_percent")))
    ) / 100
    net_revenue = revenue * (Decimal("1") - fraction)
    if task in REFERENCE_USAGE:
        incoming, outgoing = REFERENCE_USAGE[task]
        rates = offer.get("rates", {})
        expected_cost = (
            (
                Decimal(incoming)
                * Decimal(str(rates.get("input", setting(db, "ai_input_usd_per_million"))))
                + Decimal(outgoing)
                * Decimal(str(rates.get("output", setting(db, "ai_output_usd_per_million"))))
            )
            / 1000000
            * Decimal(str(rates.get("usd_rub", setting(db, "ai_usd_rub"))))
        )
        expected_cost *= Decimal("1.20")
        if task == "content_post":
            expected_cost *= Decimal(str(offer.get("credit_quote", {}).get("count", 1)))
    else:
        expected_cost = api_ceiling(db, offer)
    required_revenue = expected_cost * Decimal("2")
    if net_revenue < required_revenue:
        raise ValueError(
            "Цена действия ниже 2× расчётной себестоимости API с учетом комиссии и налога. "
            "Владелец должен проверить тариф."
        )


def activate(db: Session, actor: int) -> None:
    """One-way monetary conversion, atomic with the settings change. No active work."""
    if enabled(db):
        return
    # Table locks also exclude inserts/reservations from other application processes.
    from sqlalchemy import text

    db.execute(text("SELECT pg_advisory_xact_lock(731910000000000020)"))
    db.execute(text("LOCK TABLE users, ai_accounts, ai_operations IN SHARE ROW EXCLUSIVE MODE"))
    if db.scalar(
        select(AIOperation.id)
        .where(
            AIOperation.owner_user_id.is_not(None),
            AIOperation.status.in_(["reserved", "sent", "uncertain"]),
        )
        .limit(1)
    ):
        raise ValueError("Сначала завершите или урегулируйте все операции ИИ, включая тестовые")
    old_price = Decimal(str(setting(db, "ai_token_price_rub")))
    new_price = unit_price(db)
    for funds in db.scalars(select(AIAccount).with_for_update()):
        if funds.reserved or funds.credit_reserved:
            raise ValueError("Есть активный резерв; переход отменён")
        if not funds.balance:
            continue
        old = funds.balance
        value = old * old_price
        converted = int((value / new_price).to_integral_value(rounding=ROUND_CEILING))
        funds.credit_balance += converted
        # Migration is compensation for the legacy balance, not new cash/revenue.
        funds.balance = 0
        db.add(
            AIOperation(
                owner_user_id=funds.owner_user_id,
                bot_id=funds.bot_id,
                test_mode=funds.test_mode,
                action_key=f"credit-migrate:{funds.owner_user_id}:{funds.bot_id}:{int(funds.test_mode)}",
                kind="grant",
                status="completed",
                model="",
                billing_mode="balance",
                snapshot={
                    "unit": "credits",
                    "tokens": converted,
                    "migration": True,
                    "old_tokens": old,
                    "old_price_rub": str(old_price),
                    "unit_price_rub": str(new_price),
                    "legacy_value_rub": str(value),
                },
                balance_after=funds.credit_balance - funds.credit_reserved,
            )
        )
    db.add(
        AuditLog(
            actor_telegram_id=actor,
            action="credits_activated",
            details={
                "legacy_unit_price_rub": str(old_price),
                "credit_unit_price_rub": str(new_price),
            },
        )
    )
