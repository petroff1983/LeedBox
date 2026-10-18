"""One real Telegram tester, isolated test funds, ordinary bot unchanged for everyone else."""

import argparse
import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from leedbox.application.wallet import access_offer
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.logging import configure_logging
from leedbox.infrastructure.models import AccessPolicy, PaymentOrder, Setting, User, WalletEntry
from leedbox.telegram.payment_sandbox import TEST_SUPPORT_USERNAME, build_payment_sandbox_router
from leedbox.telegram.runtime import run


def load_sandbox():
    spec = importlib.util.spec_from_file_location(
        "local_robokassa_sandbox", Path(__file__).with_name("robokassa-sandbox.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.config_and_state(False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "run", "status"])
    parser.add_argument("--actor", type=int, required=True)
    args = parser.parse_args()
    original = Settings()
    if original.environment != "development" or original.is_owner(args.actor):
        raise ValueError("A local non-owner tester is required")
    main_engine = create_database_engine(original)
    try:
        with Session(main_engine) as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            user = db.scalar(select(User).where(User.telegram_id == args.actor))
            if not user or user.username != "backenddreamer" or not user.expert_started_at:
                raise ValueError("The explicitly selected expert must already be registered")
    finally:
        main_engine.dispose()
    config, sandbox_state = load_sandbox()
    engine = create_database_engine(config)
    try:
        if args.mode == "prepare":
            with Session(engine) as db, db.begin():
                user = db.scalar(select(User).where(User.telegram_id == args.actor))
                if user is None:
                    db.add(User(telegram_id=args.actor, expert_started_at=datetime.now(UTC)))
                receipt = json.dumps(
                    {
                        "items": [
                            {
                                "name": "TEST ONLY - fictitious topup, no sale",
                                "quantity": 1,
                                "sum": 200,
                                "payment_object": "service",
                                "payment_method": "full_prepayment",
                                "tax": "none",
                            }
                        ]
                    }
                )
                for key, value in {
                    "wallet_visible": True,
                    "wallet_enabled": True,
                    "wallet_topup_enabled": True,
                    # This flag is intentionally enabled only in the isolated
                    # acceptance DB so the ordinary agent billing path charges
                    # the test credit balance instead of legacy token units.
                    "ai_credits_enabled": True,
                    "wallet_receipt_template": receipt,
                    "payment_terms": (
                        "Только тест: условные рубли, не продажа и не платный доступ."
                    ),
                    "owner_contact": TEST_SUPPORT_USERNAME,
                }.items():
                    db.merge(Setting(key=key, value=value))
                db.merge(
                    AccessPolicy(
                        id=1,
                        mode="paid",
                        price=Decimal("100"),
                        currency="RUB",
                        period_days=30,
                        trial_days=0,
                        grace_days=0,
                        activation_grace_days=0,
                        expired_delivery="bypass",
                        hide_recommendations_for_paid=False,
                    )
                )
                # Earlier merchant acceptance used a deliberately high invoice ID.
                maximum = db.scalar(select(func.max(PaymentOrder.id))) or 1
                db.execute(
                    text("SELECT setval(pg_get_serial_sequence('payment_orders','id'), :n)"),
                    {"n": maximum},
                )
                db.flush()
                # Validate the exact seeded configuration, not only synthetic test fixtures.
                access_offer(db)
            print(json.dumps({"prepared": True, "test_only": True, "non_owner": True}))
        elif args.mode == "status":
            from leedbox.application.wallet import balance

            with Session(engine) as db:
                db.execute(text("SET TRANSACTION READ ONLY"))
                uid = db.scalar(select(User.id).where(User.telegram_id == args.actor))
                orders = db.scalars(
                    select(PaymentOrder)
                    .where(PaymentOrder.owner_user_id == uid)
                    .order_by(PaymentOrder.id.desc())
                    .limit(5)
                ).all()
                entries = db.scalars(
                    select(WalletEntry)
                    .where(WalletEntry.owner_user_id == uid)
                    .order_by(WalletEntry.id.desc())
                    .limit(5)
                ).all()
                print(
                    json.dumps(
                        {
                            "test_only": True,
                            "test_balance": str(balance(db, uid, sandbox_state["bot_id"], True)),
                            "live_balance": str(balance(db, uid, sandbox_state["bot_id"], False)),
                            "orders": [{"id": x.id, "status": x.status} for x in orders],
                            "entries": [{"kind": x.kind, "amount": str(x.amount)} for x in entries],
                        }
                    )
                )
        else:
            with Session(engine) as db:
                access_offer(db)
            router = build_payment_sandbox_router(engine, config, args.actor)
            # Only this explicit launcher adds the test routes. Main settings stay disabled/free.
            # Pass the sandbox Settings to the whole runtime: normal agent
            # routers, FSM storage, and workers must use the same isolated DB
            # as the test wallet. The runtime filters all updates to this actor.
            asyncio.run(run(config, local_test_router=router, local_test_actor=args.actor))
    finally:
        engine.dispose()


if __name__ == "__main__":
    configure_logging()
    try:
        main()
    except Exception:
        print(json.dumps({"event": "telegram_payment_sandbox_failed", "secrets_omitted": True}))
        raise SystemExit(1) from None
