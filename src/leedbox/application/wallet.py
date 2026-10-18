"""Ruble ledger: serialized funds, immutable entries, separate test accounts."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.entitlements import policy, policy_data
from leedbox.application.payments import create_order
from leedbox.application.purchasing import purchase_terms
from leedbox.application.settings import setting
from leedbox.application.versions import fingerprint
from leedbox.domain.payments import InvalidPayment, checkout_url, payment_amount, validate_receipt
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    AccessGrant,
    PaymentOrder,
    User,
    WalletAccount,
    WalletEntry,
)


def balance(db: Session, owner: int, bot: int, test: bool) -> Decimal:
    row = db.get(WalletAccount, (owner, bot, test))
    return row.balance if row else Decimal("0.00")


def account(db: Session, owner: int, bot: int, test: bool) -> WalletAccount:
    user = db.scalar(select(User).where(User.id == owner).with_for_update())
    if user is None or user.expert_started_at is None:
        raise InvalidPayment("Баланс доступен только эксперту")
    db.execute(
        insert(WalletAccount)
        .values(owner_user_id=owner, bot_id=bot, test_mode=test, balance=Decimal("0"))
        .on_conflict_do_nothing()
    )
    row = db.scalar(
        select(WalletAccount)
        .where(
            WalletAccount.owner_user_id == owner,
            WalletAccount.bot_id == bot,
            WalletAccount.test_mode == test,
        )
        .with_for_update()
    )
    assert row is not None
    return row


def credit_topup(db: Session, order: PaymentOrder, refund: bool = False) -> None:
    if order.owner_user_id is None:
        raise InvalidPayment("Покупатель недоступен")
    row = account(db, order.owner_user_id, order.bot_id, order.test_mode)
    key = f"wallet:{'refund' if refund else 'topup'}:{order.id}"
    if db.scalar(
        select(WalletEntry.id).where(
            WalletEntry.bot_id == order.bot_id, WalletEntry.action_key == key
        )
    ):
        return
    credited = db.scalar(
        select(WalletEntry.id).where(
            WalletEntry.bot_id == order.bot_id, WalletEntry.action_key == f"wallet:topup:{order.id}"
        )
    )
    if refund and credited is None:
        return
    amount = -order.amount if refund else order.amount
    # A provider refund after spending becomes a debt; purchases cannot overdraw.
    row.balance += amount
    db.add(
        WalletEntry(
            owner_user_id=order.owner_user_id,
            bot_id=order.bot_id,
            test_mode=order.test_mode,
            kind="refund" if refund else "topup",
            amount=amount,
            balance_after=row.balance,
            action_key=key,
            order_id=order.id,
            snapshot={"currency": "RUB"},
        )
    )
    db.flush()


def topup_offer(db: Session, config: Settings, bot: int, amount: Decimal) -> dict[str, Any]:
    amount = payment_amount(str(amount), "RUB")
    if amount > Decimal("100000"):
        raise InvalidPayment("Пополнение: от 0,01 до 100 000 ₽")
    minimum = Decimal(str(setting(db, "wallet_topup_min_rub")))
    if amount < minimum:
        raise InvalidPayment(f"Минимальное пополнение — {minimum:.2f} ₽")
    if not (
        setting(db, "wallet_visible")
        and setting(db, "wallet_enabled")
        and setting(db, "wallet_topup_enabled")
        and config.robokassa_enabled
    ):
        raise InvalidPayment("Пополнение временно выключено")
    try:
        raw = str(setting(db, "wallet_receipt_template"))
        if not raw or len(raw) > 1000:
            raise ValueError
        data = json.loads(raw)
        if len(data["items"]) != 1:
            raise ValueError
        data["items"][0]["sum"] = float(amount)
        data["items"][0].pop("cost", None)
        receipt = validate_receipt(json.dumps(data, ensure_ascii=False), amount)
    except (ValueError, TypeError, KeyError):
        raise InvalidPayment("Владелец ещё не настроил чек пополнения") from None
    method = str(setting(db, "wallet_sbp_alias"))
    if not method:
        raise InvalidPayment("Владелец ещё не настроил способ СБП Robokassa")
    return {
        **purchase_terms(db, config),
        "amount": str(amount),
        "receipt": receipt,
        "method": method,
        "bot": bot,
        "test": config.robokassa_test,
        "merchant": config.robokassa_merchant,
    }


def create_topup(
    db: Session,
    config: Settings,
    owner: int,
    bot: int,
    offer: dict[str, Any],
    key: str,
    accepted: bool,
) -> PaymentOrder:
    if accepted is not True or offer != topup_offer(db, config, bot, Decimal(offer["amount"])):
        raise InvalidPayment("Условия изменились. Откройте пополнение заново")
    return create_order(
        db,
        owner,
        bot,
        kind="topup",
        provider="robokassa",
        account=config.robokassa_merchant,
        test_mode=config.robokassa_test,
        amount=Decimal(offer["amount"]),
        period_days=0,
        snapshot={
            "terms_version": offer["terms_version"],
            "offer": offer,
            "receipt": offer["receipt"],
            "accepted": True,
        },
        action_key=key,
    )


def topup_checkout(db: Session, config: Settings, owner: int, bot: int, number: int) -> str:
    order = db.get(PaymentOrder, number)
    if order is None or (
        order.owner_user_id,
        order.bot_id,
        order.kind,
        order.status,
        order.account,
        order.test_mode,
    ) != (owner, bot, "topup", "pending", config.robokassa_merchant, config.robokassa_test):
        raise InvalidPayment("Пополнение недоступно")
    if not (
        config.robokassa_enabled
        and setting(db, "wallet_topup_enabled")
        and setting(db, "wallet_enabled")
        and setting(db, "wallet_visible")
    ):
        raise InvalidPayment("Пополнение выключено")
    url = checkout_url(
        order.account,
        order.amount,
        order.id,
        config.robokassa_password1.get_secret_value(),
        config.robokassa_hash,
        order.test_mode,
        order.bot_id,
        str(order.snapshot["receipt"]),
        str(order.snapshot["offer"]["method"]),
    )
    if len(url) > 4096:
        raise InvalidPayment("Чек слишком длинный. Обратитесь к владельцу")
    return url


def access_offer(db: Session, config: Settings | None = None) -> dict[str, Any]:
    current = policy(db)
    if not setting(db, "wallet_visible") or not setting(db, "wallet_enabled"):
        raise InvalidPayment("Оплата с баланса выключена")
    if current.mode != "paid":
        raise InvalidPayment("Сервис сейчас бесплатный — оплачивать доступ не нужно")
    try:
        price = payment_amount(str(current.price), "RUB")
    except InvalidPayment:
        raise InvalidPayment("Владелец ещё не настроил цену доступа в рублях и копейках") from None
    if current.currency != "RUB":
        raise InvalidPayment("Цена доступа должна быть задана в рублях")
    return {
        **purchase_terms(db, config),
        "price": str(price),
        "days": current.period_days,
        "policy": fingerprint(policy_data(current)),
    }


def buy_access(
    db: Session,
    config: Settings,
    owner: int,
    bot: int,
    offer: dict[str, Any],
    key: str,
    accepted: bool,
) -> WalletEntry:
    if not accepted or not key or len(key) > 100:
        raise InvalidPayment("Подтвердите условия покупки")
    test = config.robokassa_test
    if not test and config.environment != "production":
        raise InvalidPayment("Реальные списания разрешены только в production")
    row = account(db, owner, bot, test)
    old = db.scalar(
        select(WalletEntry).where(WalletEntry.bot_id == bot, WalletEntry.action_key == key)
    )
    if old:
        if (old.owner_user_id, old.kind, old.snapshot) != (owner, "purchase", offer):
            raise InvalidPayment("Подтверждение принадлежит другой покупке")
        return old
    if offer != access_offer(db, config):
        raise InvalidPayment("Цена или условия изменились. Откройте покупку заново")
    price = Decimal(offer["price"])
    if row.balance < price:
        raise InvalidPayment("Недостаточно средств. Пополните баланс")
    row.balance -= price
    entry = WalletEntry(
        owner_user_id=owner,
        bot_id=bot,
        test_mode=test,
        kind="purchase",
        amount=-price,
        balance_after=row.balance,
        action_key=key,
        snapshot=offer,
    )
    db.add(entry)
    if not test:
        now = datetime.now(UTC)
        base = now
        for grant in sorted(
            db.scalars(select(AccessGrant).where(AccessGrant.user_id == owner)),
            key=lambda g: g.starts_at,
        ):
            if grant.starts_at <= base < grant.ends_at:
                base = grant.ends_at
        db.add(
            AccessGrant(
                user_id=owner,
                kind="paid",
                starts_at=now,
                ends_at=base + timedelta(days=offer["days"]),
                source="wallet_payment",
                action_key="wallet:" + key,
            )
        )
    db.flush()
    return entry
