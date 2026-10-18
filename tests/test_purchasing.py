import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TEST
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerPreCheckoutQuery, GetMe, SendInvoice
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    RefundedPayment,
    SuccessfulPayment,
    Update,
    User,
)
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_payments_postgres import fiscal_receipt, payments_fixture  # noqa: F401
from test_services_postgres import service_fixture, submit  # noqa: F401

from leedbox.application.entitlements import configure_policy
from leedbox.application.payment_inbox import capture, recover
from leedbox.application.purchasing import (
    accept_service_checkout,
    access_offer,
    buy_access,
    quote_request,
)
from leedbox.application.settings import set_setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.payments import InvalidPayment
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    AccessGrant,
    AccessPolicy,
    PaymentInbox,
    PaymentOrder,
    Setting,
)
from leedbox.telegram.handlers import build_router
from leedbox.telegram.payment_polling import create_payment_dispatcher
from leedbox.telegram.runtime import create_bot
from leedbox.telegram.storage import PostgresStorage


@pytest.fixture
def purchasing_fixture(request):
    engine, config, actors, owners, bot_id = request.getfixturevalue("payments_fixture")
    config = config.model_copy(update={"stars_enabled": True, "telegram_test_api": True})
    keys = ["stars_access_price", "payment_terms"]
    with Session(engine) as db, db.begin():
        old_policy = db.get(AccessPolicy, 1)
        saved_policy = (
            {c.name: getattr(old_policy, c.name) for c in AccessPolicy.__table__.columns}
            if old_policy
            else None
        )
        previous = {key: row.value for key in keys if (row := db.get(Setting, key)) is not None}
        configure_policy(
            db,
            config,
            actors[0],
            mode="paid",
            price=Decimal("1000"),
            currency="RUB",
            period_days=30,
            trial_days=90,
            grace_days=0,
            activation_grace_days=0,
            expired_delivery="bypass",
        )
        set_setting(db, config, actors[0], "stars_access_price", 100)
        set_setting(
            db, config, actors[0], "payment_terms", "СИНТЕТИЧЕСКИЕ условия покупки и возврата"
        )
    try:
        yield engine, config, actors, owners, bot_id
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(PaymentInbox).where(PaymentInbox.telegram_user_id.in_(actors)))
            db.execute(delete(AccessPolicy).where(AccessPolicy.id == 1))
            if saved_policy:
                db.add(AccessPolicy(**saved_policy))
            for key in keys:
                db.execute(delete(Setting).where(Setting.key == key))
                if key in previous:
                    db.add(Setting(key=key, value=previous[key]))


@pytest.mark.integration
def test_access_purchase_requires_fresh_price_terms_and_explicit_acceptance(purchasing_fixture):
    engine, config, actors, owners, bot_id = purchasing_fixture
    with Session(engine) as db, db.begin():
        with pytest.raises(ValueError, match="от 0 до 100000 Stars"):
            set_setting(db, config, actors[0], "stars_access_price", -1)
        with pytest.raises(ValueError, match="до 3000 символов"):
            set_setting(db, config, actors[0], "payment_terms", "x" * 3001)
        offer = access_offer(db, config, bot_id)
        with pytest.raises(InvalidPayment):
            buy_access(db, config, owners[1], bot_id, offer, "test-no-accept", False)
        set_setting(db, config, actors[0], "stars_access_price", 200)
        with pytest.raises(InvalidPayment, match="изменились"):
            buy_access(db, config, owners[1], bot_id, offer, "test-stale", True)
        fresh = access_offer(db, config, bot_id)
        order = buy_access(db, config, owners[1], bot_id, fresh, "test-purchase", True)
        number = order.id
        assert order.amount == 200 and order.currency == "XTR"
        assert order.snapshot["accepted"] is True and order.snapshot["accepted_at"]
        set_setting(db, config, actors[0], "payment_terms", "Иные СИНТЕТИЧЕСКИЕ условия")
        assert buy_access(db, config, owners[1], bot_id, fresh, "test-purchase", True).id == number
        with pytest.raises(ObjectUnavailable):
            buy_access(db, config, owners[2], bot_id, fresh, "test-purchase", True)
        with pytest.raises(InvalidPayment):
            buy_access(db, config, owners[1], bot_id, fresh, "test-new-stale", True)
        assert order.snapshot["offer"]["terms"] == offer["terms"]


@pytest.mark.integration
def test_robokassa_quote_request_categories_consent_and_foreign_checkout(
    purchasing_fixture, request
):
    engine, config, actors, owners, bot_id = purchasing_fixture
    fixture = request.getfixturevalue("service_fixture")
    with Session(engine) as db, db.begin():
        service_request = submit(db, fixture, bot_id=bot_id)
        args = (db, config, actors[0], bot_id, service_request.id, Decimal("100"), fiscal_receipt())
        with pytest.raises(InvalidPayment):
            quote_request(*args, "digital_service", "test-digital-service")
        order = quote_request(*args, "physical_service", "test-request-invoice")
        with pytest.raises(ObjectUnavailable):
            accept_service_checkout(
                db, config, actors[2], bot_id, order.id, order.snapshot["terms_version"], True
            )
        with pytest.raises(InvalidPayment):
            accept_service_checkout(
                db, config, actors[1], bot_id, order.id, order.snapshot["terms_version"], False
            )
        url = accept_service_checkout(
            db, config, actors[1], bot_id, order.id, order.snapshot["terms_version"], True
        )
        assert url.startswith("https://auth.robokassa.ru/") and "IsTest=1" in url
        service_request.consent_revoked_at = datetime.now(UTC)
        with pytest.raises(InvalidPayment):
            accept_service_checkout(
                db, config, actors[1], bot_id, order.id, order.snapshot["terms_version"], True
            )


@pytest.mark.integration
@pytest.mark.parametrize("invoice_failure", [False, True])
def test_stars_telegram_purchase_precheckout_and_payment_ignore_rate_limit(
    purchasing_fixture, invoice_failure
):
    engine, config, actors, owners, bot_id = purchasing_fixture

    async def scenario():
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        bot = Bot("123456:TEST-TOKEN-ONLY", session=AiohttpSession(api=TEST))
        user = User(id=actors[1], is_bot=False, first_name="Synthetic buyer")
        bot_user = User(id=bot_id, is_bot=True, first_name="Synthetic bot")
        calls = []
        sequence = 0

        async def api(_, method, **kwargs):
            calls.append(method)
            if isinstance(method, SendInvoice) and invoice_failure:
                raise TelegramNetworkError(method=method, message="Synthetic uncertain send")
            if isinstance(method, GetMe):
                return bot_user
            return Message(
                message_id=10,
                date=datetime.now(UTC),
                chat=Chat(id=user.id, type="private"),
                from_user=bot_user,
            )

        async def action(code):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=f"synthetic-{sequence}",
                        from_user=user,
                        chat_instance="synthetic",
                        data=code,
                        message=Message(
                            message_id=10,
                            date=datetime.now(UTC),
                            chat=Chat(id=user.id, type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        async def checkout(number, amount=100, payer=None):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    pre_checkout_query=PreCheckoutQuery(
                        id=f"synthetic-check-{sequence}",
                        from_user=payer or user,
                        currency="XTR",
                        total_amount=amount,
                        invoice_payload=f"access:{number}",
                    ),
                ),
            )
            return calls[-1]

        key = StorageKey(bot.id, user.id, user.id)
        await storage.set_data(
            key, {"mode": "expert", "active_tool": "calc", "calc_draft": {"value": "keep"}}
        )
        try:
            with (
                patch.object(bot.session, "make_request", side_effect=api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await action("access:renew")
                purchase = (await storage.get_data(key))["purchase"]
                assert "100 Stars" in calls[-2].text and "СИНТЕТИЧЕСКИЕ" in calls[-2].text
                await action("purchase:buy:stale")
                assert not any(isinstance(method, SendInvoice) for method in calls)
                await action(f"purchase:buy:{purchase['nonce']}")
                invoices = [method for method in calls if isinstance(method, SendInvoice)]
                assert len(invoices) == 1 and invoices[0].prices[0].amount == 100
                await action(f"purchase:buy:{purchase['nonce']}")
                assert len([m for m in calls if isinstance(m, SendInvoice)]) == 1
                number = (await storage.get_data(key))["purchase"]["order_id"]
                assert (await storage.get_data(key))["calc_draft"] == {"value": "keep"}
                restored = await PostgresStorage(engine).get_data(key)
                assert restored["purchase"]["order_id"] == number
                if invoice_failure:
                    assert restored["purchase"]["invoice_state"] == "uncertain"
                    with Session(engine) as db:
                        assert db.get(PaymentOrder, number).status == "pending"
                    return
                await action("menu")
                await action(f"purchase:buy:{purchase['nonce']}")
                assert calls[-1].show_alert
                assert len([m for m in calls if isinstance(m, SendInvoice)]) == 1
                assert isinstance(await checkout(number), AnswerPreCheckoutQuery)
                assert calls[-1].ok
                with Session(engine) as db, db.begin():
                    set_setting(db, config, actors[0], "stars_access_price", 0)
                assert not (await checkout(number)).ok
                with Session(engine) as db, db.begin():
                    set_setting(db, config, actors[0], "stars_access_price", 200)
                assert (await checkout(number)).ok  # Honor the frozen positive price.
                assert not (await checkout(number, 101)).ok
                foreign = User(id=actors[2], is_bot=False, first_name="Other synthetic buyer")
                assert not (await checkout(number, payer=foreign)).ok
                with Session(engine) as db, db.begin():
                    db.get(PaymentOrder, number).created_at = datetime.now(UTC) - timedelta(hours=2)
                assert not (await checkout(number)).ok
                config.stars_enabled = False  # Historical paid events still must be recorded.
                await storage.set_data(key, {"mode": "recipient"})
                event = Message(
                    message_id=15,
                    date=datetime.now(UTC),
                    chat=Chat(id=user.id, type="private"),
                    from_user=user,
                    successful_payment=SuccessfulPayment(
                        currency="XTR",
                        total_amount=100,
                        invoice_payload=f"access:{number}",
                        telegram_payment_charge_id="synthetic-ui-charge",
                        provider_payment_charge_id="",
                    ),
                )
                with patch("leedbox.telegram.safety.allow", return_value=False):
                    for index in [101, 102]:
                        await dispatcher.feed_update(bot, Update(update_id=index, message=event))
                assert "Оплата подтверждена" in calls[-1].text
                assert calls[-1].reply_markup is None
                with Session(engine) as db:
                    assert db.get(PaymentOrder, number).status == "paid"
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(AccessGrant)
                            .where(AccessGrant.action_key == f"payment:{number}")
                        )
                        == 0
                    )
                await action(f"purchase:order:{number}")
                assert calls[-1].show_alert
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "environment,test_api", [("test", False), ("development", False), ("production", True)]
)
def test_stars_config_distinguishes_test_bot_from_test_api(environment, test_api):
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://fixture@localhost/fixture_test",
            bot_token="123456:fixture_token_0123456789012345",
            stars_enabled=True,
            environment=environment,
            telegram_test_api=test_api,
        )


def test_runtime_uses_explicit_telegram_test_server_without_network():
    async def check():
        settings = Settings(
            database_url="postgresql+psycopg://fixture@localhost/fixture_test",
            bot_token="123456:fixture_token_0123456789012345",
            telegram_test_api=True,
        )
        bot = create_bot(settings)
        try:
            assert bot.session.api == TEST
        finally:
            await bot.session.close()

    asyncio.run(check())


@pytest.mark.integration
def test_payment_inbox_recovers_after_crash_and_keeps_refunds_for_review(purchasing_fixture):
    engine, config, actors, owners, bot_id = purchasing_fixture
    with Session(engine) as db, db.begin():
        offer = access_offer(db, config, bot_id)
        order_id = buy_access(db, config, owners[1], bot_id, offer, "test-inbox-recovery", True).id
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=actors[1], type="private"),
        from_user=User(id=actors[1], is_bot=False, first_name="Private name not persisted"),
        successful_payment=SuccessfulPayment(
            currency="XTR",
            total_amount=100,
            invoice_payload=f"access:{order_id}",
            telegram_payment_charge_id="synthetic-recovery",
            provider_payment_charge_id="",
        ),
    )
    update = Update(update_id=9001, message=message)
    inbox_id = capture(engine, bot_id, True, update)
    assert capture(engine, bot_id, True, update) == inbox_id
    with (
        pytest.raises(OperationalError),
        patch(
            "leedbox.application.payment_inbox.confirm_stars_payment",
            side_effect=OperationalError("fixture", {}, Exception("private error")),
        ),
    ):
        recover(engine, bot_id, True)
    with Session(engine) as db:
        assert db.get(PaymentInbox, inbox_id).status == "pending"
        assert "Private name" not in str(db.get(PaymentInbox, inbox_id).payload)
        assert db.get(PaymentOrder, order_id).status == "pending"
    assert recover(engine, bot_id, True) == 1
    assert recover(engine, bot_id, True) == 0
    refunded = message.model_copy(
        update={
            "successful_payment": None,
            "refunded_payment": RefundedPayment(
                currency="XTR",
                total_amount=100,
                invoice_payload=f"access:{order_id}",
                telegram_payment_charge_id="synthetic-recovery",
            ),
        }
    )
    refund_id = capture(engine, bot_id, True, Update(update_id=9002, message=refunded))
    assert recover(engine, bot_id, True) == 1
    with Session(engine) as db:
        assert db.get(PaymentInbox, refund_id).status == "processed"
        assert db.get(PaymentOrder, order_id).status == "refunded"


def test_financial_polling_does_not_advance_offset_before_persistence():
    async def scenario():
        events = []
        bot = Bot("123456:TEST-TOKEN-ONLY")
        dispatcher = create_payment_dispatcher(object())

        async def source(cls, *args, **kwargs):
            yield Update(update_id=1)
            events.append("ack")
            yield Update(update_id=2)

        attempts = 0

        def persist(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                events.append("failed")
                raise OperationalError("fixture", {}, Exception("private"))
            events.append("persisted")

        try:
            with (
                patch.object(Dispatcher, "_listen_updates", classmethod(source)),
                patch("leedbox.telegram.payment_polling.capture", side_effect=persist),
                patch("leedbox.telegram.payment_polling.asyncio.sleep", new=AsyncMock()),
            ):
                reader = dispatcher._listen_updates(bot)
                assert (await anext(reader)).update_id == 1
                assert events == ["failed", "persisted"]
                assert (await anext(reader)).update_id == 2
                assert events == ["failed", "persisted", "ack", "persisted"]
                await reader.aclose()
        finally:
            await bot.session.close()

    asyncio.run(scenario())
