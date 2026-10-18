import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from aiogram.types import SuccessfulPayment
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401
from werkzeug.datastructures import MultiDict

from leedbox.application.payments import (
    confirm_payment,
    create_order,
    service_checkout,
)
from leedbox.application.stars import confirm_stars_payment, invoice_fields
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.payments import InvalidPayment, signature
from leedbox.infrastructure.models import AccessGrant, PaymentOrder, PaymentReceipt
from leedbox.web.app import create_app


@pytest.fixture
def payments_fixture(request):
    engine, config, actors, owners, *_ = request.getfixturevalue("service_fixture")
    config = config.model_copy(
        update={
            "robokassa_enabled": True,
            "robokassa_test": True,
            "robokassa_merchant": "fixture",
        }
    )
    from pydantic import SecretStr

    config.robokassa_password1 = SecretStr("fixture-pass1")
    config.robokassa_password2 = SecretStr("fixture-pass2")
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    try:
        yield engine, config, actors, owners, bot_id
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(PaymentOrder).where(PaymentOrder.owner_user_id.in_(owners)))


def fiscal_receipt():
    return json.dumps(
        {
            "items": [
                {
                    "name": "Fixture service",
                    "quantity": 1,
                    "sum": 100,
                    "payment_object": "service",
                    "payment_method": "full_payment",
                    "tax": "none",
                }
            ]
        }
    )


@pytest.mark.integration
def test_robokassa_result_scope_duplicates_and_browser_return(payments_fixture):
    engine, config, actors, owners, bot_id = payments_fixture
    with Session(engine) as db, db.begin():
        order = create_order(
            db,
            owners[1],
            bot_id,
            kind="service",
            provider="robokassa",
            account=config.robokassa_merchant,
            test_mode=config.robokassa_test,
            amount=Decimal("100"),
            period_days=0,
            snapshot={"terms_version": "fixture-terms-v1", "receipt": fiscal_receipt()},
            action_key="service-order-1",
        )
        number = order.id
        assert (
            create_order(
                db,
                owners[1],
                bot_id,
                kind="service",
                provider="robokassa",
                account=config.robokassa_merchant,
                test_mode=config.robokassa_test,
                amount=Decimal("100"),
                period_days=0,
                snapshot={"terms_version": "fixture-terms-v1", "receipt": fiscal_receipt()},
                action_key="service-order-1",
            ).id
            == number
        )
        with pytest.raises(ObjectUnavailable):
            service_checkout(db, config, owners[2], bot_id, number)
        assert "IsTest=1" in service_checkout(db, config, owners[1], bot_id, number)
    client = create_app(config, engine).test_client()
    assert client.get("/payments/robokassa/success?InvId=" + str(number)).status_code == 200
    custom = {"Shp_bot": str(bot_id), "Shp_mode": "test"}
    form = {"OutSum": "100.000000", "InvId": str(number), **custom}
    form["SignatureValue"] = signature(
        [form["OutSum"], form["InvId"]], "fixture-pass2", custom, "md5"
    )
    path = "/payments/robokassa/result"
    assert client.post(path, data={**form, "OutSum": "101"}).status_code == 400
    signed_wrong_sum = {**form, "OutSum": "101.000000"}
    signed_wrong_sum["SignatureValue"] = signature(
        [signed_wrong_sum["OutSum"], form["InvId"]], "fixture-pass2", custom, "md5"
    )
    assert client.post(path, data=signed_wrong_sum).status_code == 400
    assert client.post(path, data={**form, "Shp_mode": "live"}).status_code == 400
    assert (
        client.post(path, data=MultiDict([*form.items(), ("InvId", str(number))])).status_code
        == 400
    )
    assert client.get(path).status_code == 405

    def interrupted(*args, **kwargs):
        confirm_payment(*args, **kwargs)
        raise OperationalError("fixture SQL", {}, Exception("fixture-secret"))

    with patch("leedbox.web.payments.confirm_payment", side_effect=interrupted):
        response = client.post(path, data=form)
        assert response.status_code == 503 and "fixture-secret" not in response.text
    with Session(engine) as db:
        assert db.get(PaymentOrder, number).status == "pending"
        assert db.get(PaymentReceipt, number) is None
    for _ in range(2):
        response = client.post(path, data=form)
        assert response.status_code == 200 and response.text == f"OK{number}"
    with Session(engine) as db:
        assert db.get(PaymentOrder, number).status == "paid"
        assert (
            db.scalar(
                select(func.count())
                .select_from(PaymentReceipt)
                .where(PaymentReceipt.order_id == number)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AccessGrant)
                .where(AccessGrant.user_id == owners[1], AccessGrant.kind == "paid")
            )
            == 0
        )
    disabled = create_app(
        config.model_copy(update={"robokassa_enabled": False}), engine
    ).test_client()
    assert disabled.post(path, data=form).status_code == 404


@pytest.mark.integration
def test_stars_confirm_owner_snapshot_and_atomic_grant(payments_fixture):
    engine, _, actors, owners, bot_id = payments_fixture
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        db.add(
            AccessGrant(
                user_id=owners[1],
                kind="gift",
                starts_at=now,
                ends_at=now + timedelta(days=120),
                source="fixture",
                action_key="fixture-payment-gift",
            )
        )
        order = create_order(
            db,
            owners[1],
            bot_id,
            kind="access",
            provider="stars",
            account=str(bot_id),
            test_mode=False,
            amount=Decimal("100"),
            period_days=30,
            snapshot={"terms_version": "fixture-v1", "policy_version": "fixture-snapshot"},
            action_key="access-order-1",
        )
        number = order.id
        fields = invoice_fields(order)
        assert fields["currency"] == "XTR" and fields["provider_token"] == ""
    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=100,
        invoice_payload=f"access:{number}",
        telegram_payment_charge_id="fixture-charge-1",
        provider_payment_charge_id="",
    )
    with Session(engine) as db, db.begin(), pytest.raises(InvalidPayment):
        confirm_stars_payment(db, bot_id, actors[2], payment, False)
    with pytest.raises(RuntimeError), Session(engine) as db, db.begin():
        confirm_stars_payment(db, bot_id, actors[1], payment, False)
        raise RuntimeError("Synthetic failure before commit")
    with Session(engine) as db:
        assert db.get(PaymentReceipt, number) is None
        assert db.get(PaymentOrder, number).status == "pending"
    for _ in range(2):
        with Session(engine) as db, db.begin():
            assert confirm_stars_payment(db, bot_id, actors[1], payment, False).status == "paid"
    with Session(engine) as db:
        grants = db.scalars(
            select(AccessGrant).where(AccessGrant.user_id == owners[1], AccessGrant.kind == "paid")
        ).all()
        assert len(grants) == 1 and grants[0].ends_at == now + timedelta(days=150)


@pytest.mark.integration
def test_payment_routes_idempotency_and_cancelled_late_confirmation(payments_fixture):
    engine, _, _, owners, bot_id = payments_fixture
    fields = dict(
        kind="access",
        provider="stars",
        account=str(bot_id),
        test_mode=True,
        amount=Decimal("123"),
        period_days=30,
        snapshot={"terms_version": "fixture-v1"},
        action_key="access-order-2",
    )
    with Session(engine) as db, db.begin():
        with pytest.raises(InvalidPayment):
            create_order(db, owners[1], bot_id, **{**fields, "provider": "robokassa"})
        order = create_order(db, owners[1], bot_id, **fields)
        number = order.id
        with pytest.raises(InvalidPayment):
            create_order(db, owners[1], bot_id, **{**fields, "amount": Decimal("124")})
        order.status = "cancelled"
        confirm_payment(
            db,
            order_id=number,
            bot_id=bot_id,
            provider="stars",
            account=str(bot_id),
            test_mode=True,
            currency="XTR",
            amount=Decimal("123"),
            charge_id="fixture-charge-2",
        )
        assert order.status == "review"
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AccessGrant)
                .where(AccessGrant.action_key == f"payment:{number}")
            )
            == 0
        )


@pytest.mark.integration
def test_parallel_payment_confirmation_issues_one_grant(payments_fixture):
    engine, _, _, owners, bot_id = payments_fixture
    with Session(engine) as db, db.begin():
        order = create_order(
            db,
            owners[1],
            bot_id,
            kind="access",
            provider="stars",
            account=str(bot_id),
            test_mode=False,
            amount=Decimal("100"),
            period_days=30,
            snapshot={"terms_version": "fixture-v1"},
            action_key="parallel-payment",
        )
        number = order.id

    def confirm(_):
        with Session(engine) as db, db.begin():
            return confirm_payment(
                db,
                order_id=number,
                bot_id=bot_id,
                provider="stars",
                account=str(bot_id),
                test_mode=False,
                currency="XTR",
                amount=Decimal("100"),
                charge_id="fixture-parallel-charge",
            ).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(confirm, [1, 2])) == ["paid", "paid"]
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AccessGrant)
                .where(AccessGrant.action_key == f"payment:{number}")
            )
            == 1
        )


@pytest.mark.integration
def test_test_payment_never_grants_access_or_pays_another_order(payments_fixture):
    engine, _, _, owners, bot_id = payments_fixture
    with Session(engine) as db, db.begin():
        orders = [
            create_order(
                db,
                owners[1],
                bot_id,
                kind="access",
                provider="stars",
                account=str(bot_id),
                test_mode=True,
                amount=Decimal("100"),
                period_days=30,
                snapshot={"terms_version": "fixture-v1"},
                action_key=f"test-charge-{index}",
            )
            for index in range(2)
        ]
        numbers = [order.id for order in orders]
    fields = dict(
        bot_id=bot_id,
        provider="stars",
        account=str(bot_id),
        test_mode=True,
        currency="XTR",
        amount=Decimal("100"),
        charge_id="fixture-test-unique-charge",
    )
    with Session(engine) as db, db.begin():
        assert confirm_payment(db, order_id=numbers[0], **fields).status == "paid"
    with pytest.raises(InvalidPayment), Session(engine) as db, db.begin():
        confirm_payment(db, order_id=numbers[1], **fields)
    with Session(engine) as db:
        assert db.get(PaymentOrder, numbers[1]).status == "pending"
        assert (
            db.scalar(
                select(func.count())
                .select_from(AccessGrant)
                .where(AccessGrant.action_key == f"payment:{numbers[0]}")
            )
            == 0
        )
