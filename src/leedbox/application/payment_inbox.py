"""Minimal trusted provider events; no names, contacts, order_info or card details."""

from aiogram.types import SuccessfulPayment, Update
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.payment_operations import confirm_refund
from leedbox.application.stars import confirm_stars_payment
from leedbox.domain.payments import InvalidPayment, invoice_id
from leedbox.infrastructure.models import AuditLog, PaymentInbox


def capture(engine: Engine, bot_id: int, test_mode: bool, update: Update) -> int | None:
    message = update.message
    if message is None or not (message.successful_payment or message.refunded_payment):
        return None
    payment = message.successful_payment or message.refunded_payment
    assert payment is not None
    payload = {
        name: getattr(payment, name)
        for name in ("currency", "total_amount", "invoice_payload", "telegram_payment_charge_id")
    }
    if message.successful_payment:
        payload["is_recurring"] = bool(message.successful_payment.is_recurring)
        payload["is_subscription"] = bool(message.successful_payment.subscription_expiration_date)
    with Session(engine) as db, db.begin():
        db.execute(
            insert(PaymentInbox)
            .values(
                bot_id=bot_id,
                test_mode=test_mode,
                update_id=update.update_id,
                telegram_user_id=message.from_user.id if message.from_user else None,
                kind="successful" if message.successful_payment else "refunded",
                payload=payload,
                status="pending",
            )
            .on_conflict_do_nothing(index_elements=["bot_id", "test_mode", "update_id"])
        )
        number = db.scalar(
            select(PaymentInbox.id).where(
                PaymentInbox.bot_id == bot_id,
                PaymentInbox.test_mode == test_mode,
                PaymentInbox.update_id == update.update_id,
            )
        )
        assert number is not None
        return number


def _process(db: Session, row: PaymentInbox) -> str:
    if row.status != "pending":
        return row.status
    try:
        if (
            row.telegram_user_id is None
            or row.payload.get("is_recurring")
            or row.payload.get("is_subscription")
        ):
            raise InvalidPayment("Provider event requires manual review")
        payment = SuccessfulPayment(
            currency=row.payload["currency"],
            total_amount=row.payload["total_amount"],
            invoice_payload=row.payload["invoice_payload"],
            telegram_payment_charge_id=row.payload["telegram_payment_charge_id"],
            provider_payment_charge_id="",
        )
        # A failed domain operation must not partially modify the journal.
        with db.begin_nested():
            if row.kind == "refunded":
                from leedbox.application.stars import validate_stars_order

                order = validate_stars_order(
                    db,
                    row.bot_id,
                    row.telegram_user_id,
                    payment.invoice_payload,
                    payment.currency,
                    payment.total_amount,
                    row.test_mode,
                )
                from leedbox.infrastructure.models import PaymentReceipt

                if db.get(PaymentReceipt, order.id) is None:
                    # A trusted full refund may arrive before its success update.
                    # Both journal entries settle atomically, with no surviving grant.
                    confirm_stars_payment(
                        db, row.bot_id, row.telegram_user_id, payment, row.test_mode
                    )
                confirm_refund(
                    db,
                    number=invoice_id(payment.invoice_payload.removeprefix("access:")),
                    bot_id=row.bot_id,
                    provider="stars",
                    account=order.account,
                    test_mode=row.test_mode,
                    amount=order.amount,
                    charge_id=payment.telegram_payment_charge_id,
                )
            else:
                confirm_stars_payment(db, row.bot_id, row.telegram_user_id, payment, row.test_mode)
    except InvalidPayment:
        row.status = "review"
        db.add(
            AuditLog(action="payment_inbox_review", details={"inbox_id": row.id, "kind": row.kind})
        )
    else:
        row.status = "processed"
    return row.status


def process_one(engine: Engine, number: int) -> str:
    with Session(engine) as db, db.begin():
        row = db.scalar(select(PaymentInbox).where(PaymentInbox.id == number).with_for_update())
        if row is None:
            raise InvalidPayment("Unknown provider event")
        return _process(db, row)


def recover(engine: Engine, bot_id: int, test_mode: bool, limit: int = 10) -> int:
    processed = 0
    for _ in range(limit):
        with Session(engine) as db, db.begin():
            row = db.scalar(
                select(PaymentInbox)
                .where(
                    PaymentInbox.bot_id == bot_id,
                    PaymentInbox.test_mode == test_mode,
                    PaymentInbox.status == "pending",
                )
                .order_by(PaymentInbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                break
            _process(db, row)
            processed += 1
    return processed
