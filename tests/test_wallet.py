import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_payments_postgres import fiscal_receipt, payments_fixture  # noqa: F401
from test_purchasing import purchasing_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import wallet
from leedbox.application.entitlements import policy
from leedbox.application.payment_operations import confirm_refund
from leedbox.application.payments import confirm_payment
from leedbox.application.profile import delete_my_data
from leedbox.application.sections import visible_sections
from leedbox.application.settings import set_setting
from leedbox.domain.payments import InvalidPayment, signature
from leedbox.infrastructure.models import AccessGrant, Setting, WalletAccount, WalletEntry
from leedbox.telegram.handlers import build_router, menu
from leedbox.web.app import create_app


@pytest.fixture
def wallet_fixture(request):
    engine, config, actors, owners, bot = request.getfixturevalue("purchasing_fixture")
    values = {
        "wallet_visible": True,
        "wallet_enabled": True,
        "wallet_topup_enabled": True,
        "wallet_receipt_template": fiscal_receipt(),
        "wallet_sbp_alias": "FixtureSbp",
    }
    with Session(engine) as db, db.begin():
        saved = {k: row.value for k in values if (row := db.get(Setting, k)) is not None}
        for key, value in values.items():
            set_setting(db, config, actors[0], key, value)
    try:
        yield engine, config, actors, owners, bot
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(WalletEntry).where(WalletEntry.owner_user_id.in_(owners)))
            db.execute(delete(WalletAccount).where(WalletAccount.owner_user_id.in_(owners)))
            for key in values:
                db.execute(delete(Setting).where(Setting.key == key))
                if key in saved:
                    db.add(Setting(key=key, value=saved[key]))


def topup(fixture, amount="1000", test=True):
    engine, config, _, owners, bot = fixture
    config.robokassa_test = test
    with Session(engine) as db, db.begin():
        offer = wallet.topup_offer(db, config, bot, Decimal(amount))
        order = wallet.create_topup(db, config, owners[1], bot, offer, "wallet-fixture-topup", True)
        number = order.id
    return number


@pytest.mark.integration
def test_topup_signed_result_duplicate_rollback_hidden(wallet_fixture):
    engine, config, actors, owners, bot = wallet_fixture
    number = topup(wallet_fixture)
    with Session(engine) as db:
        url = wallet.topup_checkout(db, config, owners[1], bot, number)
        assert "IsTest=1" in url and "IncCurrLabel=FixtureSbp" in url
        assert wallet.balance(db, owners[1], bot, True) == 0
        with pytest.raises(InvalidPayment):
            wallet.topup_checkout(db, config, owners[2], bot, number)
    custom = {"Shp_bot": str(bot), "Shp_mode": "test"}
    form = {"OutSum": "1000.00", "InvId": str(number), **custom}
    form["SignatureValue"] = signature(
        [form["OutSum"], str(number)], "fixture-pass2", custom, "md5"
    )
    client = create_app(config, engine).test_client()
    assert client.get("/payments/robokassa/success").status_code == 200
    assert (
        client.post("/payments/robokassa/result", data={**form, "OutSum": "1001"}).status_code
        == 400
    )
    for _ in range(2):
        assert client.post("/payments/robokassa/result", data=form).text == f"OK{number}"
    with Session(engine) as db, db.begin():
        assert wallet.balance(db, owners[1], bot, True) == 1000
        assert wallet.balance(db, owners[1], bot, False) == 0
        assert db.scalar(select(func.count()).select_from(WalletEntry)) >= 1
        assert len(db.scalars(select(WalletEntry).where(WalletEntry.order_id == number)).all()) == 1
        set_setting(db, config, actors[0], "wallet_visible", False)
        shown = visible_sections(db)
        assert "wallet" not in shown
        assert all(
            b.callback_data != "section:wallet"
            for r in menu(config, actors[1], visible=shown).inline_keyboard
            for b in r
        )
        with pytest.raises(InvalidPayment):
            wallet.access_offer(db)
        assert wallet.balance(db, owners[1], bot, True) == 1000
        with pytest.raises(ValueError, match="поддержку"):
            delete_my_data(db, actors[1])


@pytest.mark.integration
@pytest.mark.parametrize("test", [True, False])
def test_wallet_purchase_atomic_idempotent_concurrent_and_free(wallet_fixture, test):
    engine, config, actors, owners, bot = wallet_fixture
    number = topup(wallet_fixture, test=test)
    config.environment = "production" if not test else "test"
    with Session(engine) as db, db.begin():
        confirm_payment(
            db,
            order_id=number,
            bot_id=bot,
            provider="robokassa",
            account=config.robokassa_merchant,
            test_mode=test,
            currency="RUB",
            amount=Decimal("1000"),
            charge_id=str(number),
        )
        offer = wallet.access_offer(db)
    with pytest.raises(RuntimeError), Session(engine) as db, db.begin():
        wallet.buy_access(db, config, owners[1], bot, offer, "rollback-buy", True)
        raise RuntimeError("Simulated crash")
    with Session(engine) as db:
        assert wallet.balance(db, owners[1], bot, test) == 1000

    def buy():
        with Session(engine) as db, db.begin():
            return wallet.buy_access(db, config, owners[1], bot, offer, "fixture-buy", True).id

    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(lambda _: buy(), range(2)))
    assert ids[0] == ids[1]
    with Session(engine) as db, db.begin():
        assert wallet.balance(db, owners[1], bot, test) == 0
        grants = db.scalars(
            select(AccessGrant).where(
                AccessGrant.user_id == owners[1], AccessGrant.source == "wallet_payment"
            )
        ).all()
        assert len(grants) == (0 if test else 1)
        with pytest.raises(InvalidPayment, match="Недостаточно"):
            wallet.buy_access(db, config, owners[1], bot, offer, "second-buy", True)
        with pytest.raises(InvalidPayment):
            wallet.buy_access(db, config, owners[2], bot, offer, "fixture-buy", True)
        policy(db).mode = "free"
        with pytest.raises(InvalidPayment, match="бесплатный"):
            wallet.access_offer(db)
        # An externally confirmed refund after spending records debt and blocks purchases.
        for _ in range(2):
            confirm_refund(
                db,
                number=number,
                bot_id=bot,
                provider="robokassa",
                account=config.robokassa_merchant,
                test_mode=test,
                amount=Decimal("1000"),
                charge_id=str(number),
            )
        assert wallet.balance(db, owners[1], bot, test) == -1000


@pytest.mark.integration
def test_wallet_configuration_and_stale_price(wallet_fixture):
    engine, config, actors, owners, bot = wallet_fixture
    with Session(engine) as db, db.begin():
        with pytest.raises(InvalidPayment):
            wallet.topup_offer(db, config, bot, Decimal("100001"))
        offer = wallet.access_offer(db)
        policy(db).price = Decimal("900")
        with pytest.raises(InvalidPayment, match="изменились"):
            wallet.buy_access(db, config, owners[1], bot, offer, "changed-price", True)
        set_setting(db, config, actors[0], "wallet_receipt_template", "")
        with pytest.raises(InvalidPayment, match="чек"):
            wallet.topup_offer(db, config, bot, Decimal("200"))
        assert wallet.balance(db, owners[1], bot, True) == 0


@pytest.mark.integration
def test_wallet_telegram_flow_and_recipient_isolation(wallet_fixture):
    engine, config, actors, owners, bot_id = wallet_fixture

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(build_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Synthetic buyer")
        source = Message(
            message_id=1, date=datetime.now(UTC), chat=Chat(id=actor.id, type="private")
        )
        state = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await state.update_data(mode="expert", paused=True)
        calls = []

        async def api(_, method, **kwargs):
            calls.append(method)
            if isinstance(method, SendMessage):
                return Message(message_id=len(calls), date=datetime.now(UTC), chat=source.chat)
            return True

        async def action(code):
            await dp.feed_update(
                bot,
                Update(
                    update_id=len(calls) + 1,
                    callback_query=CallbackQuery(
                        id=str(len(calls) + 1),
                        from_user=actor,
                        chat_instance="fixture",
                        message=source,
                        data=code,
                    ),
                ),
            )

        try:
            with (
                patch.object(bot.session, "make_request", side_effect=api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await action("section:wallet")
                assert any(
                    isinstance(c, SendMessage) and "Тестовый баланс" in c.text for c in calls
                )
                await action("wallet:topup")
                assert await state.get_state() == "wallet_amount"
                await dp.feed_update(
                    bot,
                    Update(
                        update_id=20,
                        message=Message(
                            message_id=20,
                            date=datetime.now(UTC),
                            chat=source.chat,
                            from_user=actor,
                            text="500,50",
                        ),
                    ),
                )
                saved = await state.get_data()
                assert saved["wallet_offer"]["amount"] == "500.50"
                await action("wallet:confirm:" + saved["wallet_nonce"])
                sent = [c for c in calls if isinstance(c, SendMessage)][-1]
                assert sent.reply_markup.inline_keyboard[0][0].url.startswith(
                    "https://auth.robokassa.ru/"
                )
                assert "IsTest=1" in sent.reply_markup.inline_keyboard[0][0].url
                with Session(engine) as db:
                    assert wallet.balance(db, owners[1], bot_id, True) == 0
                await state.update_data(mode="recipient")
                calls.clear()
                await action("wallet:open")
                assert not any(isinstance(c, SendMessage) for c in calls)
                assert isinstance(calls[-1], AnswerCallbackQuery) and calls[-1].show_alert
        finally:
            await dp.storage.close()
            await bot.session.close()

    asyncio.run(scenario())
