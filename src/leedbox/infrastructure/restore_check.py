"""Isolated local Docker restore drill. Never targets development or production databases."""

import re
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from secrets import randbelow
from uuid import uuid4

from aiogram.types import Chat, Message, SuccessfulPayment, Update
from aiogram.types import User as TgUser
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application.entitlements import start_expert
from leedbox.application.payment_inbox import capture, recover
from leedbox.application.payments import create_order
from leedbox.application.profile import delete_my_data
from leedbox.application.projects import create_project, register_user
from leedbox.application.stars import confirm_stars_payment
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import (
    PaymentInbox,
    PaymentOperation,
    PaymentOrder,
    PaymentReceipt,
    Project,
)


def main() -> None:
    config = Settings()
    url = make_url(config.database_url.get_secret_value())
    if (
        config.environment != "test"
        or url.database != "leedbox_test"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.port != 5434
        or url.username != "leedbox"
    ):
        raise SystemExit("Restore drill accepts only the local compose test database on port 5434")
    suffix = uuid4().hex[:12]
    target = "leedbox_restore_test_" + suffix
    if not re.fullmatch(r"leedbox_restore_test_[a-f0-9]{12}", target):
        raise SystemExit("Invalid restore target")
    dump = "/tmp/leedbox-restore-" + suffix + ".dump"

    def docker(*args: str) -> None:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres-test", *args],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            # Never relay raw driver/subprocess output: configuration may contain secrets.
            raise RuntimeError("Restore drill command failed: " + args[0])

    engine = create_database_engine(config)
    telegram_id = 900000000000 + randbelow(1000000000)
    created = False
    order_id = inbox_id = None
    try:
        with Session(engine) as db, db.begin():
            user = register_user(db, telegram_id)
            start_expert(db, user)
            project = create_project(db, user.id, "Synthetic restore probe", "UTC")
            project_id = project.id
            version = db.scalar(text("SELECT version_num FROM alembic_version"))
            order = create_order(
                db,
                user.id,
                999901,
                kind="access",
                provider="stars",
                account="999901",
                test_mode=True,
                amount=Decimal(1),
                period_days=30,
                snapshot={"terms_version": "synthetic-restore"},
                action_key="restore-" + suffix,
            )
            order_id = order.id
            payment = SuccessfulPayment(
                currency="XTR",
                total_amount=1,
                invoice_payload=f"access:{order.id}",
                telegram_payment_charge_id="restore-" + suffix,
                provider_payment_charge_id="",
            )
            confirm_stars_payment(db, 999901, telegram_id, payment, True)
            db.add(
                PaymentOperation(
                    order_id=order_id,
                    kind="reconcile",
                    status="done",
                    reason="Synthetic restore probe",
                    outcome="test",
                )
            )
        inbox_id = capture(
            engine,
            999901,
            True,
            Update(
                update_id=randbelow(2**31),
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=telegram_id, type="private"),
                    from_user=TgUser(id=telegram_id, is_bot=False, first_name="Synthetic"),
                    successful_payment=payment,
                ),
            ),
        )
        docker("pg_dump", "-U", "leedbox", "-d", "leedbox_test", "-Fc", "-f", dump)
        docker("createdb", "-U", "leedbox", target)
        created = True
        docker("pg_restore", "-U", "leedbox", "-d", target, "--no-owner", "--exit-on-error", dump)
        restored = create_engine(url.set(database=target), pool_pre_ping=True)
        try:
            with Session(restored) as db:
                row = db.scalar(select(Project).where(Project.id == project_id))
                if row is None or row.name != "Synthetic restore probe":
                    raise RuntimeError("Restore data mismatch")
                if db.scalar(text("SELECT version_num FROM alembic_version")) != version:
                    raise RuntimeError("Restore schema mismatch")
                restored_order = db.get(PaymentOrder, order_id)
                restored_inbox = db.get(PaymentInbox, inbox_id)
                if (
                    restored_order is None
                    or restored_order.status != "paid"
                    or db.get(PaymentReceipt, order_id) is None
                    or restored_inbox is None
                    or restored_inbox.status != "pending"
                ):
                    raise RuntimeError("Restore payment mismatch")
                if (
                    db.scalar(select(PaymentOperation).where(PaymentOperation.order_id == order_id))
                    is None
                ):
                    raise RuntimeError("Restore operations mismatch")
            recover(restored, 999901, True)
            with Session(restored) as db:
                restored_inbox = db.get(PaymentInbox, inbox_id)
                if (
                    restored_inbox is None
                    or restored_inbox.status != "processed"
                    or db.scalar(
                        select(func.count())
                        .select_from(PaymentReceipt)
                        .where(PaymentReceipt.order_id == order_id)
                    )
                    != 1
                ):
                    raise RuntimeError("Restore payment replay mismatch")
            print(
                "Restore verified: isolated test database; Alembic revision "
                + str(version)
                + "; project/order/receipt/inbox/operation recovered; "
                + "replay idempotent; no API calls."
            )
        finally:
            restored.dispose()
    finally:
        with Session(engine) as db, db.begin():
            if inbox_id is not None:
                db.execute(delete(PaymentInbox).where(PaymentInbox.id == inbox_id))
            if order_id is not None:
                db.execute(delete(PaymentOrder).where(PaymentOrder.id == order_id))
            delete_my_data(db, telegram_id)
        engine.dispose()
        if created:
            # Exact, freshly-created validated target, in the test container only.
            docker("dropdb", "-U", "leedbox", target)
        docker("rm", "--", dump)


if __name__ == "__main__":
    main()
