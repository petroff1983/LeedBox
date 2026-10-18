"""Owner commands and trusted, transactional refund confirmations. No provider calls."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.services import owner_required
from leedbox.application.versions import fingerprint, lock_content
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.payments import InvalidPayment
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    AccessGrant,
    AuditLog,
    PaymentOperation,
    PaymentOrder,
    PaymentReceipt,
    User,
)


def order_version(order: PaymentOrder) -> str:
    return fingerprint([order.id, order.status, str(order.amount), str(order.refunded_at)])


def owner_order(
    db: Session, config: Settings, actor: int, bot_id: int, number: int
) -> PaymentOrder:
    owner_required(config, actor)
    row = db.scalar(
        select(PaymentOrder)
        .where(PaymentOrder.id == number, PaymentOrder.bot_id == bot_id)
        .with_for_update()
    )
    if row is None:
        raise ObjectUnavailable
    return row


def command(
    db: Session,
    config: Settings,
    actor: int,
    bot_id: int,
    number: int,
    action: str,
    expected: str,
    reason: str,
) -> PaymentOrder:
    order = owner_order(db, config, actor, bot_id, number)
    if expected != order_version(order):
        raise InvalidPayment("Заказ изменился. Откройте его заново")
    if not reason.strip() or len(reason) > 300:
        raise InvalidPayment("Укажите причину: от 1 до 300 символов")
    if action == "cancel":
        if order.status != "pending" or db.get(PaymentReceipt, number) is not None:
            raise InvalidPayment("Отменить можно только неоплаченный заказ")
        order.status = "cancelled"
    elif action in {"refund", "reconcile"}:
        if order.kind == "topup" and action == "refund":
            raise InvalidPayment(
                "Возврат предоплаты — через поддержку после проверки остатка и чеков"
            )
        if not config.payment_operations_enabled:
            raise InvalidPayment("Операции провайдеров выключены в конфигурации")
        if action == "refund" and (
            order.status not in {"paid", "review"} or not db.get(PaymentReceipt, number)
        ):
            raise InvalidPayment("Возврат доступен только для подтверждённой оплаты")
        lock_content(db, "payment_operation", f"{number}:{action}")
        old = db.scalar(
            select(PaymentOperation).where(
                PaymentOperation.order_id == number, PaymentOperation.kind == action
            )
        )
        if old:
            if action == "refund" and old.provider_ref and old.status == "failed":
                old.status, old.outcome = "submitted", ""  # Retry status reads only.
            if action == "reconcile" and old.status in {"done", "failed"}:
                old.status, old.outcome = "pending", ""
            return order  # Never blindly resubmit a refund, including uncertain ones.
        db.add(
            PaymentOperation(
                order_id=number, kind=action, actor_telegram_id=actor, reason=reason.strip()
            )
        )
    else:
        raise InvalidPayment("Неизвестное действие")
    db.add(
        AuditLog(
            actor_telegram_id=actor,
            action="payment_" + action + "_requested",
            subject_user_id=order.owner_user_id,
            details={"order_id": number},
        )
    )
    return order


def confirm_refund(
    db: Session,
    *,
    number: int,
    bot_id: int,
    provider: str,
    account: str,
    test_mode: bool,
    amount: Decimal,
    charge_id: str,
) -> PaymentOrder:
    """Trusted adapter only: full refund, original charge, exact provider scope."""
    lock_content(db, "payment_charge", f"{provider}:{account}:{test_mode}:{charge_id}")
    row = db.scalar(select(PaymentOrder).where(PaymentOrder.id == number).with_for_update())
    receipt = db.get(PaymentReceipt, number)
    if (
        row is None
        or receipt is None
        or (row.bot_id, row.provider, row.account, row.test_mode, row.amount, receipt.charge_id)
        != (bot_id, provider, account, test_mode, amount, charge_id)
    ):
        raise InvalidPayment("Возврат не совпадает с исходной оплатой")
    if row.status == "refunded":
        return row
    if row.kind == "topup" and row.owner_user_id is not None:
        from leedbox.application.wallet import credit_topup

        credit_topup(db, row, refund=True)
    row.status, row.refunded_at = "refunded", datetime.now(UTC)
    if row.owner_user_id is not None:
        db.scalar(select(User).where(User.id == row.owner_user_id).with_for_update())
        _rebuild_paid_grants(db, row.owner_user_id)
    operation = db.scalar(
        select(PaymentOperation).where(
            PaymentOperation.order_id == number, PaymentOperation.kind == "refund"
        )
    )
    if operation:
        operation.status, operation.outcome = "done", "full_refund_confirmed"
    db.add(
        AuditLog(
            action="payment_refunded",
            subject_user_id=row.owner_user_id,
            details={"order_id": number, "provider": provider, "test_mode": test_mode},
        )
    )
    db.flush()
    return row


def _rebuild_paid_grants(db: Session, owner_id: int) -> None:
    # A later payment may have inherited a refunded interval. Rebuild the remaining
    # payment intervals, keeping trials/gifts/manual grants and the financial audit.
    db.flush()
    grants = list(
        db.scalars(select(AccessGrant).where(AccessGrant.user_id == owner_id).with_for_update())
    )
    paid = {g.action_key: g for g in grants if g.source == "stars_payment"}
    basis = [g for g in grants if g.source != "stars_payment"]
    orders = db.scalars(
        select(PaymentOrder)
        .where(
            PaymentOrder.owner_user_id == owner_id,
            PaymentOrder.provider == "stars",
            PaymentOrder.test_mode.is_(False),
            PaymentOrder.paid_at.is_not(None),
        )
        .order_by(PaymentOrder.paid_at, PaymentOrder.id)
    )
    for order in orders:
        grant = paid.get(f"payment:{order.id}")
        if not grant:
            continue
        if order.status != "paid":
            grant.ends_at = grant.starts_at
            continue
        assert order.paid_at is not None
        base = order.paid_at
        for old in sorted(basis, key=lambda g: g.starts_at):
            if old.starts_at <= base < old.ends_at:
                base = old.ends_at
        grant.starts_at, grant.ends_at = order.paid_at, base + timedelta(days=order.period_days)
        basis.append(grant)
