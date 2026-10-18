import asyncio
import base64
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import test_purchasing
from aiogram.client.telegram import TEST
from aiogram.types import StarTransaction, StarTransactions, TransactionPartnerUser
from aiogram.types import User as TgUser
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_admin import TEST_SECRET, TEST_TOKEN
from test_admin_postgres import RENDER_ASSETS, RENDER_SNAPSHOTS, password_login
from test_payments_postgres import fiscal_receipt, payments_fixture  # noqa: F401
from test_services_postgres import service_fixture, submit  # noqa: F401

from leedbox.application.payment_inbox import capture, recover
from leedbox.application.payment_operations import command, confirm_refund, order_version
from leedbox.application.payments import confirm_payment, create_order
from leedbox.application.purchasing import accept_service_checkout, quote_request
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.payment_handoff import ticket, verify
from leedbox.domain.payments import InvalidPayment
from leedbox.infrastructure import robokassa_operations as robo
from leedbox.infrastructure.models import (
    AccessGrant,
    PaymentInbox,
    PaymentOperation,
    PaymentOrder,
    PaymentReceipt,
)
from leedbox.telegram.payment_operations import run_one
from leedbox.web.app import create_app

purchasing_fixture = test_purchasing.purchasing_fixture


@pytest.mark.integration
def test_refund_before_success_is_atomic_and_late_success_does_not_restore_access(
    purchasing_fixture,
):
    engine, _, actors, owners, bot_id = purchasing_fixture
    with Session(engine) as db, db.begin():
        order = create_order(
            db,
            owners[1],
            bot_id,
            kind="access",
            provider="stars",
            account=str(bot_id),
            test_mode=False,
            amount=Decimal(100),
            period_days=30,
            snapshot={"terms_version": "synthetic"},
            action_key="refund-before-success",
        )
        number = order.id
        trial_end = db.scalar(
            select(AccessGrant).where(AccessGrant.user_id == owners[1], AccessGrant.kind == "trial")
        ).ends_at
    fields = dict(
        currency="XTR",
        total_amount=100,
        invoice_payload=f"access:{number}",
        telegram_payment_charge_id="synthetic-refund-before-success",
    )
    message = test_purchasing.Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=test_purchasing.Chat(id=actors[1], type="private"),
        from_user=TgUser(id=actors[1], is_bot=False, first_name="Fixture"),
        refunded_payment=test_purchasing.RefundedPayment(**fields),
    )
    refund_id = capture(
        engine, bot_id, False, test_purchasing.Update(update_id=9101, message=message)
    )
    assert recover(engine, bot_id, False) == 1
    with Session(engine) as db:
        assert db.get(PaymentInbox, refund_id).status == "processed"
        assert db.get(PaymentOrder, number).status == "refunded"
        assert db.get(PaymentReceipt, number).charge_id == fields["telegram_payment_charge_id"]
        grant = db.scalar(select(AccessGrant).where(AccessGrant.action_key == f"payment:{number}"))
        assert grant.ends_at == grant.starts_at
        assert (
            db.scalar(
                select(AccessGrant).where(
                    AccessGrant.user_id == owners[1], AccessGrant.kind == "trial"
                )
            ).ends_at
            == trial_end
        )
    success = message.model_copy(
        update={
            "refunded_payment": None,
            "successful_payment": test_purchasing.SuccessfulPayment(
                **fields, provider_payment_charge_id=""
            ),
        }
    )
    capture(engine, bot_id, False, test_purchasing.Update(update_id=9102, message=success))
    assert recover(engine, bot_id, False) == 1
    assert recover(engine, bot_id, False) == 0
    with Session(engine) as db:
        assert db.get(PaymentOrder, number).status == "refunded"
        grant = db.scalar(select(AccessGrant).where(AccessGrant.action_key == f"payment:{number}"))
        assert grant.ends_at == grant.starts_at


def paid(db, fixture, key, test=True):
    _, _, _, owners, bot_id = fixture
    row = create_order(
        db,
        owners[1],
        bot_id,
        kind="access",
        provider="stars",
        account=str(bot_id),
        test_mode=test,
        amount=Decimal(100),
        period_days=30,
        snapshot={"terms_version": "synthetic"},
        action_key=key,
    )
    confirm_payment(
        db,
        order_id=row.id,
        bot_id=bot_id,
        provider="stars",
        account=str(bot_id),
        test_mode=test,
        currency="XTR",
        amount=Decimal(100),
        charge_id="synthetic-" + key,
    )
    return row


@pytest.mark.integration
def test_full_refund_rebuilds_later_payment_and_preserves_trial(purchasing_fixture):
    engine, config, actors, owners, bot_id = purchasing_fixture
    with Session(engine) as db, db.begin():
        first = paid(db, purchasing_fixture, "first-live", False)
        second = paid(db, purchasing_fixture, "second-live", False)
        trial = db.scalar(
            select(AccessGrant).where(AccessGrant.user_id == owners[1], AccessGrant.kind == "trial")
        )
        trial_end = trial.ends_at
        assert db.scalar(
            select(AccessGrant).where(AccessGrant.action_key == f"payment:{second.id}")
        ).ends_at == trial_end + timedelta(days=60)
        args = dict(
            number=first.id,
            bot_id=bot_id,
            provider="stars",
            account=str(bot_id),
            test_mode=False,
            amount=Decimal(100),
            charge_id="synthetic-first-live",
        )
        with pytest.raises(InvalidPayment):
            confirm_refund(db, **{**args, "amount": Decimal(99)})
        confirm_refund(db, **args)
        confirm_refund(db, **args)
        assert trial.ends_at == trial_end
        assert db.scalar(
            select(AccessGrant).where(AccessGrant.action_key == f"payment:{second.id}")
        ).ends_at == trial_end + timedelta(days=30)
        assert first.status == "refunded" and second.status == "paid"
        with pytest.raises(InvalidPayment):
            command(
                db, config, actors[0], bot_id, second.id, "cancel", order_version(second), "test"
            )
        with pytest.raises(ObjectUnavailable):
            command(
                db,
                config,
                actors[0],
                bot_id + 1,
                second.id,
                "cancel",
                order_version(second),
                "test",
            )


@pytest.mark.integration
@pytest.mark.parametrize("failure", [False, True])
def test_stars_refund_queue_never_retries_ambiguous_money_request(purchasing_fixture, failure):
    engine, config, actors, _, bot_id = purchasing_fixture
    config.payment_operations_enabled = True
    with Session(engine) as db, db.begin():
        row = paid(db, purchasing_fixture, "refund-queue")
        number = row.id
        command(
            db, config, actors[0], bot_id, number, "refund", order_version(row), "Synthetic test"
        )
        command(
            db, config, actors[0], bot_id, number, "refund", order_version(row), "Synthetic test"
        )
        assert db.scalar(select(func.count()).select_from(PaymentOperation)) == 1
    fake = SimpleNamespace(
        id=bot_id,
        session=SimpleNamespace(api=TEST),
        refund_star_payment=AsyncMock(
            side_effect=TimeoutError if failure else None, return_value=True
        ),
    )
    assert asyncio.run(run_one(fake, engine, config))
    assert not asyncio.run(run_one(fake, engine, config))
    assert fake.refund_star_payment.await_count == 1
    with Session(engine) as db:
        op = db.scalar(select(PaymentOperation).where(PaymentOperation.order_id == number))
        assert op.status == ("uncertain" if failure else "done")
        assert db.get(PaymentOrder, number).status == ("paid" if failure else "refunded")


@pytest.mark.integration
def test_stars_reconciliation_links_cross_page_refund(purchasing_fixture):
    engine, config, actors, owners, bot_id = purchasing_fixture
    config.payment_operations_enabled = True
    with Session(engine) as db, db.begin():
        row = create_order(
            db,
            owners[1],
            bot_id,
            kind="access",
            provider="stars",
            account=str(bot_id),
            test_mode=True,
            amount=Decimal(100),
            period_days=30,
            snapshot={"terms_version": "fixture"},
            action_key="missing-update",
        )
        number = row.id
        command(
            db, config, actors[0], bot_id, number, "reconcile", order_version(row), "Lost update"
        )
    user = TgUser(id=actors[1], is_bot=False, first_name="Synthetic buyer")
    partner = TransactionPartnerUser(
        type="user",
        transaction_type="invoice_payment",
        user=user,
        invoice_payload=f"access:{number}",
    )
    incoming = StarTransaction(
        id="reconciled-charge", amount=100, date=datetime.now(UTC), source=partner
    )
    refund = StarTransaction(
        id="reconciled-charge", amount=100, date=datetime.now(UTC), receiver=partner
    )
    fake = SimpleNamespace(
        id=bot_id,
        session=SimpleNamespace(api=TEST),
        get_star_transactions=AsyncMock(
            side_effect=[
                StarTransactions(transactions=[refund] * 100),
                StarTransactions(transactions=[incoming]),
            ]
        ),
    )
    assert asyncio.run(run_one(fake, engine, config))
    with Session(engine) as db:
        assert db.get(PaymentOrder, number).status == "refunded"
        assert (
            db.scalar(select(PaymentOperation).where(PaymentOperation.order_id == number)).status
            == "done"
        )


def test_robokassa_adapter_uses_signed_refund_receipt_and_validates_status():
    from leedbox.infrastructure.config import Settings

    config = Settings(
        _env_file=None,
        bot_token=TEST_TOKEN,
        robokassa_enabled=True,
        robokassa_test=False,
        environment="production",
        robokassa_merchant="fixture",
        robokassa_password1="one",
        robokassa_password2="two",
        robokassa_password3="three",
    )
    ref = "68cd7fa6-1338-4745-ba5c-28d16cbcdb3d"
    with patch.object(
        robo, "transport", return_value=json.dumps({"success": True, "requestId": ref}).encode()
    ) as request:
        assert robo.refund(config, "fixture-op", fiscal_receipt(), Decimal(100)) == ref
        payload = request.call_args.args[1].decode().split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        assert decoded["OpKey"] == "fixture-op"
        assert decoded["InvoiceItems"][0]["Cost"] == 100
        assert decoded["InvoiceItems"][0]["Tax"] == "none"
        assert "RefundSum" not in decoded  # Full refund only.
    with (
        patch.object(
            robo,
            "transport",
            return_value=json.dumps({"requestId": ref, "amount": 99, "label": "finished"}).encode(),
        ),
        pytest.raises(InvalidPayment),
    ):
        robo.refund_state(ref, Decimal(100))
    xml = (
        b"<OperationStateResponse><Result><Code>0</Code></Result><State><Code>100</Code></State>"
        b"<Info><OutSum>100.00</OutSum><OutCurrLabel>RUB</OutCurrLabel>"
        b"<OpKey>fixture-op</OpKey></Info><UserFields><Field>"
        b"<Name>Shp_bot</Name><Value>123456</Value></Field><Field><Name>Shp_mode</Name>"
        b"<Value>live</Value></Field></UserFields></OperationStateResponse>"
    )
    with patch.object(robo, "transport", return_value=xml):
        assert robo.operation(config, 1)["amount"] == "100.00"
    with (
        patch.object(robo, "transport", return_value=b"<!DOCTYPE malicious>"),
        pytest.raises(InvalidPayment),
    ):
        robo.operation(config, 1)


def test_checkout_ticket_tampering_expiry_and_secret():
    token = ticket(1, "synthetic-secret")
    assert verify(token, "synthetic-secret") == 1
    for value, secret in [
        (token, "other"),
        (token + "0", "synthetic-secret"),
        (ticket(1, "synthetic-secret", int(time.time()) - 1), "synthetic-secret"),
    ]:
        with pytest.raises(InvalidPayment):
            verify(value, secret)


@pytest.mark.integration
def test_robokassa_refund_is_submitted_until_provider_finishes(purchasing_fixture, request):
    engine, config, actors, _, bot_id = purchasing_fixture
    config.payment_operations_enabled = True
    fixture = request.getfixturevalue("service_fixture")
    config.environment = "production"  # Synthetic scope only; provider calls mocked.
    with Session(engine) as db, db.begin():
        row = submit(db, fixture, bot_id=bot_id)
        order = quote_request(
            db,
            config,
            actors[0],
            bot_id,
            row.id,
            Decimal(100),
            fiscal_receipt(),
            "physical_service",
            "robo-refund",
        )
        order.test_mode = False  # Synthetic live-scope adapter, never a provider call.
        number = order.id
        confirm_payment(
            db,
            order_id=number,
            bot_id=bot_id,
            provider="robokassa",
            account="fixture",
            test_mode=False,
            currency="RUB",
            amount=Decimal(100),
            charge_id=str(number),
        )
        config.robokassa_test = False
        command(
            db,
            config,
            actors[0],
            bot_id,
            number,
            "refund",
            order_version(order),
            "Synthetic full refund",
        )
    fake = SimpleNamespace(id=bot_id)
    ref = "68cd7fa6-1338-4745-ba5c-28d16cbcdb3d"
    with (
        patch.object(
            robo,
            "operation",
            return_value={
                "bot_id": str(bot_id),
                "amount": "100.00",
                "currency": "RUB",
                "op_key": "fixture",
                "state": "100",
            },
        ),
        patch.object(robo, "refund", return_value=ref) as create,
    ):
        assert asyncio.run(run_one(fake, engine, config))
        with Session(engine) as db, db.begin():
            op = db.scalar(select(PaymentOperation).where(PaymentOperation.order_id == number))
            assert op.status == "submitted" and db.get(PaymentOrder, number).status == "paid"
            op.updated_at = datetime.now(UTC) - timedelta(seconds=31)
        with patch.object(robo, "refund_state", return_value="finished"):
            assert asyncio.run(run_one(fake, engine, config))
        assert create.call_count == 1
    with Session(engine) as db:
        assert db.get(PaymentOrder, number).status == "refunded"


@pytest.mark.integration
def test_owner_payment_quote_csrf_stale_and_render(purchasing_fixture, request):
    engine, config, actors, _, bot_id = purchasing_fixture
    fixture = request.getfixturevalue("service_fixture")
    config = config.model_copy(
        update={
            "admin_enabled": True,
            "web_secret_key": SecretStr(TEST_SECRET),
            "bot_token": SecretStr(TEST_TOKEN),
        }
    )
    bot_id = int(TEST_TOKEN.split(":")[0])
    with Session(engine) as db, db.begin():
        row = submit(db, fixture, bot_id=bot_id)
        request_id = row.id
        revision = row.revision
    client = create_app(config, engine).test_client()
    password_login(client, engine, config, actors[0])
    with client.session_transaction(path="/owner/") as browser:
        csrf = browser["csrf"]
    path = f"/owner/payments/quote?request_id={request_id}"
    data = {
        "csrf_token": csrf,
        "confirmed": "1",
        "amount": "100",
        "receipt": fiscal_receipt(),
        "physical": "1",
        "revision": str(revision),
        "action_key": "owner-test-quote",
    }
    assert client.post(path, data={**data, "csrf_token": "bad"}).status_code == 403
    assert "Заявка изменилась" in client.post(path, data={**data, "revision": "0"}).text
    response = client.post(path, data=data)
    assert response.status_code == 302
    assert client.post(path, data=data).location == response.location
    for name, url in [
        ("payment-quote", path),
        ("payment-detail", response.location),
        ("payment-list", "/owner/payments/"),
    ]:
        page = client.get(url)
        assert page.status_code == 200
        RENDER_SNAPSHOTS[name] = page.get_data()
    import re

    for body in RENDER_SNAPSHOTS.values():
        for static in re.findall(rb'(?:src|href)="(/(?:owner/)?static/[^"?]+)', body):
            asset = client.get(static.decode())
            RENDER_ASSETS[static.decode()] = (asset.get_data(), asset.content_type)
    client.post("/owner/logout", data={"csrf_token": csrf})


@pytest.mark.integration
def test_long_receipt_handoff_requires_acceptance_and_active_request(purchasing_fixture, request):
    engine, config, actors, _, bot_id = purchasing_fixture
    config.public_base_url = "https://synthetic.invalid"
    fixture = request.getfixturevalue("service_fixture")
    receipt = json.dumps(
        {
            "items": [
                {
                    "name": "Синтетическая услуга " + "я" * 100,
                    "quantity": 1,
                    "sum": 10,
                    "payment_object": "service",
                    "payment_method": "full_payment",
                    "tax": "none",
                }
                for _ in range(10)
            ]
        },
        ensure_ascii=False,
    )
    with Session(engine) as db, db.begin():
        row = submit(db, fixture, bot_id=bot_id)
        request_id = row.id
        order = quote_request(
            db,
            config,
            actors[0],
            bot_id,
            row.id,
            Decimal(100),
            receipt,
            "physical_service",
            "long-receipt",
        )
        number = order.id
        version = order.snapshot["terms_version"]
    client = create_app(config, engine).test_client()
    path = "/payments/robokassa/checkout/" + ticket(number, config.bot_token.get_secret_value())
    assert client.get(path).status_code == 404
    with Session(engine) as db, db.begin():
        url = accept_service_checkout(db, config, actors[1], bot_id, number, version, True)
    assert url.startswith("https://synthetic.invalid/payments/robokassa/checkout/")
    assert len(url) < 256
    response = client.get(path)
    assert response.status_code == 200
    assert 'method="post"' in response.text and "LeedBox не запрашивает" in response.text
    assert "form-action https://auth.robokassa.ru" in response.headers["Content-Security-Policy"]
    RENDER_SNAPSHOTS["payment-handoff"] = response.get_data()
    with Session(engine) as db, db.begin():
        from leedbox.infrastructure.models import ServiceRequest

        db.get(ServiceRequest, request_id).consent_revoked_at = datetime.now(UTC)
    assert client.get(path).status_code == 404
