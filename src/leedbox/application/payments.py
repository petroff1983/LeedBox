"""Transactional payment journal. Caller owns commit; acknowledgement follows commit."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.versions import fingerprint, lock_content
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.payments import InvalidPayment, checkout_url, payment_amount
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AccessGrant, AuditLog, PaymentOrder, PaymentReceipt, User


def create_order(
    session: Session,
    owner_id: int,
    bot_id: int,
    *,
    kind: str,
    provider: str,
    account: str,
    test_mode: bool,
    amount: Decimal,
    period_days: int,
    snapshot: dict[str, Any],
    action_key: str,
) -> PaymentOrder:
    currency = "XTR" if provider == "stars" else "RUB"
    amount = payment_amount(str(amount), currency)
    if (
        (kind, provider)
        not in {("access", "stars"), ("service", "robokassa"), ("topup", "robokassa")}
        or not 0 < bot_id <= 2**63 - 1
        or not 0 < len(account) <= 100
        or not 0 < len(action_key) <= 64
        or not isinstance(test_mode, bool)
        or (kind == "access" and not 0 < period_days <= 365000)
        or (kind in {"service", "topup"} and period_days != 0)
        or not isinstance(snapshot.get("terms_version"), str)
        or not snapshot["terms_version"].strip()
    ):
        raise InvalidPayment("Invalid order or unsupported payment route")
    fields = dict(
        owner_user_id=owner_id,
        bot_id=bot_id,
        kind=kind,
        provider=provider,
        account=account,
        test_mode=test_mode,
        currency=currency,
        amount=str(amount),
        period_days=period_days,
        snapshot=snapshot,
    )
    digest = fingerprint(fields)
    lock_content(session, "payment_create", f"{bot_id}:{action_key}")
    user = session.scalar(select(User).where(User.id == owner_id).with_for_update())
    if user is None or user.expert_started_at is None:
        raise ObjectUnavailable
    old = session.scalar(
        select(PaymentOrder).where(
            PaymentOrder.bot_id == bot_id, PaymentOrder.action_key == action_key
        )
    )
    if old:
        if old.request_hash != digest:
            raise InvalidPayment("Order form already used")
        return old
    fields["amount"] = amount
    order = PaymentOrder(**fields, action_key=action_key, request_hash=digest)
    session.add(order)
    session.flush()
    return order


def service_checkout(
    session: Session, config: Settings, owner_id: int, bot_id: int, id: int
) -> str:
    order = session.scalar(
        select(PaymentOrder).where(
            PaymentOrder.id == id,
            PaymentOrder.owner_user_id == owner_id,
            PaymentOrder.bot_id == bot_id,
        )
    )
    if order is None:
        raise ObjectUnavailable
    if (
        not config.robokassa_enabled
        or order.provider != "robokassa"
        or order.kind != "service"
        or order.status != "pending"
        or order.account != config.robokassa_merchant
        or order.test_mode != config.robokassa_test
    ):
        raise InvalidPayment("Checkout unavailable")
    return checkout_url(
        order.account,
        order.amount,
        order.id,
        config.robokassa_password1.get_secret_value(),
        config.robokassa_hash,
        order.test_mode,
        order.bot_id,
        str(order.snapshot["receipt"]),
    )


def confirm_payment(
    session: Session,
    *,
    order_id: int,
    bot_id: int,
    provider: str,
    account: str,
    test_mode: bool,
    currency: str,
    amount: Decimal,
    charge_id: str,
    now: datetime | None = None,
) -> PaymentOrder:
    """Only trusted adapters may call this after verifying provider authenticity."""
    if not 0 < len(charge_id) <= 200:
        raise InvalidPayment("Invalid payment charge")
    now = now or datetime.now(UTC)
    lock_content(session, "payment_charge", f"{provider}:{account}:{test_mode}:{charge_id}")
    order = session.scalar(
        select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update()
    )
    if order is None or (
        order.bot_id,
        order.provider,
        order.account,
        order.test_mode,
        order.currency,
        order.amount,
    ) != (bot_id, provider, account, test_mode, currency, amount):
        raise InvalidPayment("Payment does not match order")
    existing = session.scalar(
        select(PaymentReceipt).where(
            PaymentReceipt.provider == provider,
            PaymentReceipt.account == account,
            PaymentReceipt.test_mode == test_mode,
            PaymentReceipt.charge_id == charge_id,
        )
    )
    if existing:
        if existing.order_id != order.id:
            raise InvalidPayment("Payment charge already used")
        return order
    if session.get(PaymentReceipt, order.id) is not None:
        raise InvalidPayment("Order already paid with a different charge")
    user = session.scalar(select(User).where(User.id == order.owner_user_id).with_for_update())
    review = order.status != "pending" or user is None
    session.add(
        PaymentReceipt(
            order_id=order.id,
            provider=provider,
            account=account,
            test_mode=test_mode,
            charge_id=charge_id,
            confirmed_at=now,
        )
    )
    order.status, order.paid_at = ("review" if review else "paid"), now
    if order.kind == "topup" and not review:
        from leedbox.application.wallet import credit_topup

        credit_topup(session, order)
    # Test confirmations are journaled but NEVER grant real access.
    if order.kind == "access" and not test_mode and not review:
        assert user is not None
        base = now
        grants = session.scalars(select(AccessGrant).where(AccessGrant.user_id == user.id)).all()
        for grant in sorted(grants, key=lambda item: item.starts_at):
            if grant.starts_at <= base < grant.ends_at:
                base = grant.ends_at
        session.add(
            AccessGrant(
                user_id=user.id,
                kind="paid",
                starts_at=now,
                ends_at=base + timedelta(days=order.period_days),
                source="stars_payment",
                action_key=f"payment:{order.id}",
            )
        )
    session.add(
        AuditLog(
            action="payment_confirmed",
            subject_user_id=order.owner_user_id,
            details={
                "order_id": order.id,
                "provider": provider,
                "test_mode": test_mode,
                "status": order.status,
            },
        )
    )
    session.flush()
    return order
