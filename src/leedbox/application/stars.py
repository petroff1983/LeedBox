"""Trusted Telegram adapter primitives; invoice issuance is a separate rollout step."""

from decimal import Decimal

from aiogram.types import LabeledPrice, SuccessfulPayment
from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.payments import confirm_payment
from leedbox.domain.payments import InvalidPayment, invoice_id
from leedbox.infrastructure.models import PaymentOrder, User


def invoice_fields(order: PaymentOrder) -> dict[str, object]:
    if (
        order.kind != "access"
        or order.provider != "stars"
        or order.currency != "XTR"
        or order.status != "pending"
        or order.amount != order.amount.to_integral_value()
    ):
        raise InvalidPayment("Stars invoice unavailable")
    return {
        "title": "Доступ к инструментам LeedBox",
        "description": f"Разовый платёж за {order.period_days} дней доступа",
        "payload": f"access:{order.id}",
        "currency": "XTR",
        "provider_token": "",
        "prices": [LabeledPrice(label="Доступ", amount=int(order.amount))],
        "start_parameter": f"access_{order.id}",
    }


def validate_stars_order(
    session: Session,
    bot_id: int,
    telegram_id: int,
    payload: str,
    currency: str,
    total_amount: int,
    test_mode: bool,
) -> PaymentOrder:
    if not payload.startswith("access:"):
        raise InvalidPayment("Invalid invoice payload")
    number = invoice_id(payload.removeprefix("access:"))
    order = session.scalar(
        select(PaymentOrder)
        .join(User, User.id == PaymentOrder.owner_user_id)
        .where(
            PaymentOrder.id == number,
            PaymentOrder.bot_id == bot_id,
            User.telegram_id == telegram_id,
        )
    )
    if order is None or (
        order.kind,
        order.provider,
        order.account,
        order.currency,
        order.amount,
        order.test_mode,
    ) != ("access", "stars", str(bot_id), currency, Decimal(total_amount), test_mode):
        raise InvalidPayment("Stars payment does not match order")
    return order


def confirm_stars_payment(
    session: Session, bot_id: int, telegram_id: int, payment: SuccessfulPayment, test_mode: bool
) -> PaymentOrder:
    order = validate_stars_order(
        session,
        bot_id,
        telegram_id,
        payment.invoice_payload,
        payment.currency,
        payment.total_amount,
        test_mode,
    )
    return confirm_payment(
        session,
        order_id=order.id,
        bot_id=bot_id,
        provider="stars",
        account=str(bot_id),
        test_mode=test_mode,
        currency="XTR",
        amount=Decimal(payment.total_amount),
        charge_id=payment.telegram_payment_charge_id,
    )
