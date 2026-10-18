import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_payments_postgres import payments_fixture  # noqa: F401
from test_purchasing import purchasing_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401
from test_wallet import wallet_fixture  # noqa: F401

from leedbox.application import agents
from leedbox.application.payments import confirm_payment
from leedbox.application.wallet import access_offer, balance
from leedbox.domain.payments import InvalidPayment
from leedbox.domain.services import owner_contact_url
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AccessGrant, AIAccount, AIOperation, PaymentOrder, Setting
from leedbox.telegram.payment_sandbox import TEST_SUPPORT_USERNAME, build_payment_sandbox_router
from leedbox.telegram.runtime import LocalSandboxActorMiddleware, run


def sandbox_config(config):
    return config.model_copy(
        update={
            "environment": "development",
            "stars_enabled": False,
            "database_url": SecretStr(
                "postgresql+psycopg://dummy:dummy@localhost:5434/leedbox_robo_sandbox_dummy_test"
            ),
        }
    )


def test_actual_sandbox_support_setting_is_a_username_not_a_url():
    assert owner_contact_url(TEST_SUPPORT_USERNAME) == "https://t.me/" + TEST_SUPPORT_USERNAME


@pytest.mark.integration
def test_invalid_purchase_support_is_a_controlled_error(request):
    engine, *_ = request.getfixturevalue("wallet_fixture")
    with Session(engine) as db, db.begin():
        previous = db.get(Setting, "owner_contact").value
        try:
            db.merge(Setting(key="owner_contact", value="https://t.me/dummy_support"))
            db.flush()
            with pytest.raises(InvalidPayment, match="Контакт поддержки настроен неверно"):
                access_offer(db)
            db.merge(Setting(key="owner_contact", value=TEST_SUPPORT_USERNAME))
            db.flush()
            assert access_offer(db)["support"] == "https://t.me/" + TEST_SUPPORT_USERNAME
        finally:
            db.merge(Setting(key="owner_contact", value=previous))


@pytest.mark.integration
def test_sandbox_telegram_full_flow_and_isolation(request):
    engine, original, actors, owners, bot_id = request.getfixturevalue("wallet_fixture")
    config = sandbox_config(original)
    with Session(engine) as db, db.begin():
        receipt = db.get(Setting, "wallet_receipt_template")
        data = json.loads(receipt.value)
        data["items"][0]["sum"] = 200
        receipt.value = json.dumps(data)
    with Session(engine) as db:
        previous_ai_credits = db.get(Setting, "ai_credits_enabled")
        previous_ai_credits = previous_ai_credits.value if previous_ai_credits else None
        previous_ai_images = db.get(Setting, "ai_images_enabled")
        previous_ai_images = previous_ai_images.value if previous_ai_images else None

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=MemoryStorage())
        dp.update.outer_middleware(LocalSandboxActorMiddleware(actors[1]))
        dp.include_router(build_payment_sandbox_router(engine, config, actors[1]))
        fallback = Router()
        unhandled = []

        @fallback.callback_query()
        async def ordinary_callback(callback: CallbackQuery):
            unhandled.append(callback.data)

        dp.include_router(fallback)
        state = dp.fsm.get_context(bot=bot, chat_id=actors[1], user_id=actors[1])
        await state.set_state("existing_wizard")
        await state.update_data(mode="expert", preserved="original wizard")
        calls = []

        async def api(_, method, **kwargs):
            calls.append(method)
            if isinstance(method, SendMessage):
                return Message(
                    message_id=len(calls),
                    date=datetime.now(UTC),
                    chat=Chat(id=actors[1], type="private"),
                )
            return True

        async def action(code, actor=actors[1], group=False):
            await dp.feed_update(
                bot,
                Update(
                    update_id=len(calls) + len(unhandled) + 1,
                    callback_query=CallbackQuery(
                        id=str(len(calls) + 1),
                        chat_instance="dummy",
                        from_user=User(id=actor, is_bot=False, first_name="Dummy"),
                        data=code,
                        message=Message(
                            message_id=1,
                            date=datetime.now(UTC),
                            chat=Chat(
                                id=-100 if group else actor, type="group" if group else "private"
                            ),
                        ),
                    ),
                ),
            )

        try:
            with patch.object(bot.session, "make_request", side_effect=api):
                await action("profile:access", actor=actors[2])
                await action("paytest:topup", actor=actors[2])
                await action("paytest:open", group=True)
                await action("paytest:open", group=True, actor=actors[1])
                # Updates from any other account, including group chats, are
                # dropped before they can reach the regular router or sandbox.
                assert unhandled == []
                assert calls == []
                await dp.feed_update(
                    bot,
                    Update(
                        update_id=50,
                        message=Message(
                            message_id=50,
                            date=datetime.now(UTC),
                            chat=Chat(id=actors[1], type="private"),
                            text="/testpay",
                            from_user=User(id=actors[1], is_bot=False, first_name="Dummy"),
                        ),
                    ),
                )
                assert "Рублёвый баланс: 0.00" in calls[-1].text
                await action("profile:access")
                assert "0.00" in [c for c in calls if isinstance(c, SendMessage)][-1].text
                await action("paytest:confirm:forged")
                assert isinstance(calls[-1], AnswerCallbackQuery) and calls[-1].show_alert
                with Session(engine) as db:
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(PaymentOrder)
                            .where(PaymentOrder.owner_user_id == owners[1])
                        )
                        == 0
                    )
                await action("paytest:topup")
                saved = (await state.get_data())["payment_sandbox"]
                code = "paytest:confirm:" + saved["nonce"]
                await action(code)
                sent = [c for c in calls if isinstance(c, SendMessage)][-1]
                checkout = sent.reply_markup.inline_keyboard[0][0].url
                parsed = urlsplit(checkout)
                assert parsed.netloc == "auth.robokassa.ru"
                assert parse_qs(parsed.query)["IsTest"] == ["1"]
                number = int(parse_qs(parsed.query)["InvId"][0])
                await action(code)  # repeated confirmation must not create another invoice
                with Session(engine) as db, db.begin():
                    orders = db.scalars(
                        select(PaymentOrder).where(PaymentOrder.owner_user_id == owners[1])
                    ).all()
                    assert len(orders) == 1 and orders[0].test_mode
                    assert balance(db, owners[1], bot_id, True) == 0
                    # Dummy provider confirmation in the unit test only; no external API.
                    confirm_payment(
                        db,
                        order_id=number,
                        bot_id=bot_id,
                        provider="robokassa",
                        account=config.robokassa_merchant,
                        test_mode=True,
                        currency="RUB",
                        amount=Decimal("200"),
                        charge_id=str(number),
                    )
                    before = db.scalar(
                        select(func.count())
                        .select_from(AccessGrant)
                        .where(AccessGrant.user_id == owners[1])
                    )
                    from leedbox.application.entitlements import policy

                    policy(db).price = Decimal("100")
                await action("paytest:open")
                assert "200.00" in [c for c in calls if isinstance(c, SendMessage)][-1].text
                await action("paytest:buy")
                saved = (await state.get_data())["payment_sandbox"]
                await action("paytest:confirm:" + saved["nonce"])
                await action("paytest:confirm:" + saved["nonce"])
                assert (
                    "Тестовая покупка выполнена"
                    in [c for c in calls if isinstance(c, SendMessage)][-1].text
                )
                with Session(engine) as db:
                    assert balance(db, owners[1], bot_id, True) == 100
                    assert balance(db, owners[1], bot_id, False) == 0
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(AccessGrant)
                            .where(AccessGrant.user_id == owners[1])
                        )
                        == before
                    )
                assert await state.get_state() == "existing_wizard"
                assert (await state.get_data())["preserved"] == "original wizard"
                await action("menu")
                assert unhandled[-1] == "menu"

                # Exercise the ordinary agent billing service against the same
                # sandbox DB and the same test AIAccount used by /testpay.
                with Session(engine) as db, db.begin():
                    db.merge(Setting(key="ai_credits_enabled", value=True))
                    db.merge(Setting(key="ai_images_enabled", value=True))
                await action("paytest:topup")
                saved = (await state.get_data())["payment_sandbox"]
                await action("paytest:confirm:" + saved["nonce"])
                checkout = [c for c in calls if isinstance(c, SendMessage)][-1].reply_markup
                link = checkout.inline_keyboard[0][0].url
                topup_number = int(parse_qs(urlsplit(link).query)["InvId"][0])
                with Session(engine) as db, db.begin():
                    confirm_payment(
                        db,
                        order_id=topup_number,
                        bot_id=bot_id,
                        provider="robokassa",
                        account=config.robokassa_merchant,
                        test_mode=True,
                        currency="RUB",
                        amount=Decimal("200"),
                        charge_id=str(topup_number),
                    )
                await action("paytest:tokens")
                saved = (await state.get_data())["payment_sandbox"]
                await action("paytest:confirm:" + saved["nonce"])

                from test_image_agent import article, payload, result

                with Session(engine) as db, db.begin():
                    account = db.get(AIAccount, (owners[1], bot_id, True))
                    assert account is not None and account.credit_balance == 200
                    account.free_limits = {"image_prompts": 0}
                    offer = agents.quote_images(db, article())
                    assert offer["credit_quote"]["amount"] == 10
                test_config = config.model_copy(
                    update={"deepseek_api_key": SecretStr("fixture-key-never-used")}
                )
                with patch.object(
                    agents, "deepseek_generate", return_value=payload(result())
                ) as provider:
                    operation_id = agents.generate(
                        engine,
                        test_config,
                        owners[1],
                        bot_id,
                        "sandbox-agent-credit-charge-" + uuid4().hex,
                        offer,
                    )
                    provider.assert_called_once()
                with Session(engine) as db:
                    operation = db.get(AIOperation, operation_id)
                    account = db.get(AIAccount, (owners[1], bot_id, True))
                    assert operation is not None and operation.status == "completed"
                    assert operation.charged_credits == 10 and operation.balance_after == 190
                    assert account is not None and account.credit_balance == 190
        finally:
            await dp.storage.close()
            await bot.session.close()
            with Session(engine) as db, db.begin():
                db.execute(
                    Setting.__table__.delete().where(Setting.key == "ai_credits_enabled")
                )
                db.execute(
                    Setting.__table__.delete().where(Setting.key == "ai_images_enabled")
                )
                if previous_ai_credits is not None:
                    db.add(Setting(key="ai_credits_enabled", value=previous_ai_credits))
                if previous_ai_images is not None:
                    db.add(Setting(key="ai_images_enabled", value=previous_ai_images))
                # The surrounding fixture deletes these synthetic users and
                # correctly refuses to delete users with non-zero balances.
                for account in db.scalars(
                    select(AIAccount).where(AIAccount.owner_user_id.in_(owners))
                ):
                    account.balance = 0
                    account.reserved = 0
                    account.credit_balance = 0
                    account.credit_reserved = 0
                    account.credit_value_rub = Decimal("0")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", "production"),
        ("robokassa_test", False),
        ("robokassa_enabled", False),
        ("stars_enabled", True),
        ("database_url", SecretStr("postgresql+psycopg://dummy@localhost:5432/main")),
        ("owner_ids", frozenset({123})),
    ],
)
def test_sandbox_rejects_unsafe_configuration(field, value):
    config = Settings(database_url="postgresql+psycopg://dummy@localhost:5434/main_test")
    config = (
        sandbox_config(config)
        .model_copy(
            update={
                "robokassa_enabled": True,
                "robokassa_test": True,
            }
        )
        .model_copy(update={field: value})
    )
    with pytest.raises(ValueError):
        build_payment_sandbox_router(None, config, 123)


def test_runtime_rejects_extra_routes_in_production_before_database_access():
    config = Settings(
        database_url="postgresql+psycopg://dummy@localhost:5432/main", environment="production"
    )
    with pytest.raises(ValueError, match="development"):
        asyncio.run(run(config, local_test_router=Router()))


def test_runtime_requires_sandbox_actor_with_test_router():
    config = sandbox_config(
        Settings(database_url="postgresql+psycopg://dummy:dummy@localhost:5434/main_test")
    )
    with pytest.raises(ValueError, match="router and its designated actor"):
        asyncio.run(run(config, local_test_router=Router()))


def test_sandbox_runtime_rejects_database_outside_isolated_sandbox():
    config = Settings.model_construct(
        database_url=SecretStr("postgresql+psycopg://dummy:dummy@localhost:5434/main_test"),
        environment="development",
        robokassa_enabled=True,
        robokassa_test=True,
        stars_enabled=False,
        owner_ids=frozenset(),
    )
    with pytest.raises(ValueError, match="isolated local sandbox"):
        asyncio.run(
            run(config, local_test_router=Router(), local_test_actor=456)
        )
