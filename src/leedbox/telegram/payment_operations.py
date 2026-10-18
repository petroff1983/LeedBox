"""Provider operations run only by the existing bot worker, after owner confirmation."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aiogram import Bot
from aiogram.client.telegram import TEST
from aiogram.types import SuccessfulPayment, TransactionPartnerUser
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.payment_operations import confirm_refund
from leedbox.application.payments import confirm_payment
from leedbox.application.stars import confirm_stars_payment
from leedbox.domain.payments import InvalidPayment
from leedbox.infrastructure import robokassa_operations as robo
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import PaymentOperation, PaymentOrder, PaymentReceipt, User


def claim(engine: Engine, bot_id: int) -> dict[str, Any] | None:
    with Session(engine) as db, db.begin():
        # A crash during a money-changing request is ambiguous, NOT permission to retry.
        for old in db.scalars(
            select(PaymentOperation)
            .join(PaymentOrder)
            .where(
                PaymentOrder.bot_id == bot_id,
                PaymentOperation.status == "processing",
                PaymentOperation.updated_at < datetime.now(UTC) - timedelta(minutes=5),
            )
            .with_for_update()
        ):
            old.status, old.outcome = "uncertain", "worker_interrupted"
        op = db.scalar(
            select(PaymentOperation)
            .join(PaymentOrder)
            .where(
                PaymentOrder.bot_id == bot_id,
                PaymentOperation.status.in_(["pending", "submitted"]),
                (PaymentOperation.status == "pending")
                | (PaymentOperation.updated_at < datetime.now(UTC) - timedelta(seconds=30)),
            )
            .order_by(PaymentOperation.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if op is None:
            return None
        order = db.get(PaymentOrder, op.order_id)
        assert order is not None
        user = db.get(User, order.owner_user_id) if order.owner_user_id else None
        receipt = db.get(PaymentReceipt, order.id)
        job = {
            "id": op.id,
            "number": order.id,
            "kind": op.kind,
            "provider": order.provider,
            "account": order.account,
            "test": order.test_mode,
            "amount": order.amount,
            "telegram_id": user.telegram_id if user else None,
            "charge": receipt.charge_id if receipt else None,
            "receipt": order.snapshot.get("receipt"),
            "reference": op.provider_ref,
        }
        op.status, op.updated_at = "processing", datetime.now(UTC)
        return job


def outcome(
    engine: Engine, number: int, state: str, code: str, reference: str | None = None
) -> None:
    with Session(engine) as db, db.begin():
        row = db.scalar(
            select(PaymentOperation).where(PaymentOperation.id == number).with_for_update()
        )
        if row and row.status != "done":
            row.status, row.outcome, row.updated_at = state, code, datetime.now(UTC)
            if reference:
                row.provider_ref = reference


def settle(engine: Engine, job: dict[str, Any]) -> None:
    with Session(engine) as db, db.begin():
        confirm_refund(
            db,
            number=job["number"],
            bot_id=int(job["bot_id"]),
            provider=job["provider"],
            account=job["account"],
            test_mode=job["test"],
            amount=job["amount"],
            charge_id=job["charge"],
        )


async def stars_reconcile(bot: Bot, engine: Engine, job: dict[str, Any]) -> bool:
    # Search a bounded provider window. Absence is NOT proof of unpaid status.
    found: SuccessfulPayment | None = None
    refunded: set[str] = set()
    complete = False
    for offset in range(0, 10000, 100):
        page = await bot.get_star_transactions(offset=offset, limit=100, request_timeout=15)
        for tx in page.transactions:
            if tx.nanostar_amount:
                continue
            if (
                isinstance(tx.receiver, TransactionPartnerUser)
                and tx.receiver.user.id == job["telegram_id"]
                and abs(tx.amount) == job["amount"]
            ):
                refunded.add(tx.id)
            source = tx.source
            if (
                not isinstance(source, TransactionPartnerUser)
                or source.transaction_type != "invoice_payment"
            ):
                continue
            if (
                source.user.id != job["telegram_id"]
                or source.invoice_payload != f"access:{job['number']}"
            ):
                continue
            if tx.amount != job["amount"] or source.subscription_period:
                raise InvalidPayment("Неподдерживаемая транзакция")
            if found is not None and found.telegram_payment_charge_id != tx.id:
                raise InvalidPayment("Несколько транзакций: нужна ручная проверка")
            found = SuccessfulPayment(
                currency="XTR",
                total_amount=tx.amount,
                invoice_payload=source.invoice_payload,
                telegram_payment_charge_id=tx.id,
                provider_payment_charge_id="",
            )

        if len(page.transactions) < 100:
            complete = True
            break
    # Do not infer payment state from a truncated provider history.
    if not complete:
        raise InvalidPayment("История превышает окно проверки")

    def confirm() -> None:
        with Session(engine) as db, db.begin():
            if found is not None:
                confirm_stars_payment(db, bot.id, job["telegram_id"], found, job["test"])
                job["charge"] = found.telegram_payment_charge_id
            if job["charge"] in refunded:
                confirm_refund(
                    db,
                    number=job["number"],
                    bot_id=bot.id,
                    provider="stars",
                    account=job["account"],
                    test_mode=job["test"],
                    amount=job["amount"],
                    charge_id=job["charge"],
                )

    if found is None and job["charge"] not in refunded:
        return False
    await asyncio.to_thread(confirm)
    return True


async def run_one(bot: Bot, engine: Engine, config: Settings) -> bool:
    if not config.payment_operations_enabled:
        return False
    job = await asyncio.to_thread(claim, engine, bot.id)
    if job is None:
        return False
    job["bot_id"] = bot.id
    money_attempted = False
    try:
        if not job["test"] and config.environment != "production":
            raise InvalidPayment("Основные платежи доступны только в production-конфигурации")
        if job["provider"] == "stars":
            if job["test"] != (bot.session.api == TEST) or job["account"] != str(bot.id):
                raise InvalidPayment("Другое окружение платежа")
            if job["telegram_id"] is None:
                raise InvalidPayment("Покупатель удалил данные: нужна ручная проверка")
            if job["kind"] == "refund":
                money_attempted = True
                ok = await bot.refund_star_payment(
                    job["telegram_id"], job["charge"], request_timeout=15
                )
                if ok is not True:
                    raise InvalidPayment("Провайдер не подтвердил возврат")
                await asyncio.to_thread(settle, engine, job)
            else:
                found = await stars_reconcile(bot, engine, job)
                if not found:
                    raise InvalidPayment("Не найдено в проверенном окне; нужна ручная проверка")
        else:
            if job["test"] != config.robokassa_test or job["account"] != config.robokassa_merchant:
                raise InvalidPayment("Другое окружение платежа")
            if job["reference"] and job["kind"] == "refund":
                state = await asyncio.to_thread(robo.refund_state, job["reference"], job["amount"])
                if state == "processing":
                    await asyncio.to_thread(
                        outcome, engine, job["id"], "submitted", "provider_processing"
                    )
                    return True
                if state != "finished":
                    raise InvalidPayment("Возврат отменён провайдером")
                await asyncio.to_thread(settle, engine, job)
            else:
                info = await asyncio.to_thread(robo.operation, config, job["number"])
                if (info["bot_id"], Decimal(info["amount"]), info["currency"]) != (
                    str(bot.id),
                    job["amount"],
                    "RUB",
                ):
                    raise InvalidPayment("Операция не совпадает с заказом")
                if info["state"] != "100":
                    raise InvalidPayment("Провайдер пока не подтвердил оплату")
                if job["kind"] == "refund":
                    money_attempted = True
                    ref = await asyncio.to_thread(
                        robo.refund, config, info["op_key"], job["receipt"], job["amount"]
                    )
                    await asyncio.to_thread(
                        outcome, engine, job["id"], "submitted", "provider_processing", ref
                    )
                    return True

                def confirm_robo() -> None:
                    with Session(engine) as db, db.begin():
                        confirm_payment(
                            db,
                            order_id=job["number"],
                            bot_id=bot.id,
                            provider="robokassa",
                            account=job["account"],
                            test_mode=False,
                            currency="RUB",
                            amount=job["amount"],
                            charge_id=str(job["number"]),
                        )

                await asyncio.to_thread(confirm_robo)
        await asyncio.to_thread(outcome, engine, job["id"], "done", "provider_confirmed")
    except Exception:
        await asyncio.to_thread(
            outcome,
            engine,
            job["id"],
            "uncertain" if money_attempted else "failed",
            "check_provider_before_retry" if money_attempted else "provider_check_failed",
        )
    return True
