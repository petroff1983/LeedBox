import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_payments_postgres import payments_fixture  # noqa: F401
from test_purchasing import purchasing_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401
from test_wallet import wallet_fixture  # noqa: F401

from leedbox.application import account_overview, agents, ai_tokens, wallet
from leedbox.application.entitlements import can_check_subscriptions, policy, require_access
from leedbox.application.settings import set_setting
from leedbox.domain.payments import InvalidPayment
from leedbox.infrastructure.models import (
    AccessGrant,
    AIAccount,
    AIOperation,
    WalletAccount,
    WalletEntry,
)
from leedbox.telegram.account import account_rows, build_account_router


@pytest.fixture
def account_fixture(request):
    fixture = request.getfixturevalue("wallet_fixture")
    engine, _, _, owners, _ = fixture
    try:
        yield fixture
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(AIOperation).where(AIOperation.owner_user_id.in_(owners)))
            db.execute(delete(AIAccount).where(AIAccount.owner_user_id.in_(owners)))


@pytest.mark.integration
def test_token_purchase_exact_atomic_idempotent_and_independent(request):
    engine, config, _, owners, bot = request.getfixturevalue("account_fixture")
    key = f"one-token-purchase:{owners[1]}"
    with Session(engine) as db, db.begin():
        db.add(
            WalletAccount(
                owner_user_id=owners[1], bot_id=bot, test_mode=True, balance=Decimal("500")
            )
        )
        offer = agents.token_offer(db, config, Decimal("200"))
        assert offer["tokens"] == 666666
        assert offer["unit_price_rub"] == "0.0003"
        before = db.scalar(
            select(func.count()).select_from(AccessGrant).where(AccessGrant.user_id == owners[1])
        )
    with pytest.raises(RuntimeError), Session(engine) as db, db.begin():
        agents.buy_tokens(db, config, owners[1], bot, "abort-token-purchase", offer)
        raise RuntimeError("dummy crash")
    with Session(engine) as db:
        assert wallet.balance(db, owners[1], bot, True) == 500
        assert ai_tokens.available(db, owners[1], bot, True) == 0

    def buy(_):
        with Session(engine) as db, db.begin():
            return agents.buy_tokens(db, config, owners[1], bot, key, offer).id

    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(buy, range(2)))
    assert ids[0] == ids[1]
    with Session(engine) as db:
        assert wallet.balance(db, owners[1], bot, True) == 300
        assert ai_tokens.available(db, owners[1], bot, True) == 666666
        assert wallet.balance(db, owners[1], bot, False) == 0
        assert ai_tokens.available(db, owners[1], bot, False) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(AccessGrant)
                .where(AccessGrant.user_id == owners[1])
            )
            == before
        )
        assert (
            len(
                db.scalars(
                    select(WalletEntry).where(
                        WalletEntry.owner_user_id == owners[1],
                        WalletEntry.action_key == "ai:" + key,
                    )
                ).all()
            )
            == 1
        )


@pytest.mark.integration
def test_token_price_minimum_insufficient_and_stale_offer(request):
    engine, config, actors, owners, bot = request.getfixturevalue("account_fixture")
    with Session(engine) as db:  # rollback leaves settings untouched
        for value in ["0", "199.99", "NaN", "-200", "100001"]:
            with pytest.raises(ValueError):
                agents.token_offer(db, config, Decimal(value))
        offer = agents.token_offer(db, config, Decimal("200"))
        with pytest.raises(ValueError, match="Недостаточно"):
            agents.buy_tokens(db, config, owners[1], bot, "poor-buyer", offer)
        set_setting(db, config, actors[0], "ai_token_price_rub", "0.0004")
        with pytest.raises(ValueError, match="изменились"):
            agents.buy_tokens(db, config, owners[1], bot, "stale-price", offer)
        for key in ["ai_token_price_rub", "ai_token_purchase_min_rub", "wallet_topup_min_rub"]:
            with pytest.raises(ValueError):
                set_setting(db, config, actors[0], key, "NaN")
        with pytest.raises(InvalidPayment, match="Минимальное"):
            wallet.topup_offer(db, config, bot, Decimal("199.99"))


@pytest.mark.integration
def test_underpriced_external_api_search_is_blocked_before_spending(request):
    engine, config, actors, owners, bot = request.getfixturevalue("account_fixture")
    with Session(engine) as db:
        with pytest.raises(ValueError, match="Цена платного анализа"):
            agents.ensure_search_margin(db, "content_search", 1000)
        with pytest.raises(ValueError, match="Цена платного анализа"):
            agents.ensure_search_margin(db, "dzen_search", 1000)
        agents.ensure_search_margin(db, "content_search", 300000)
        agents.ensure_search_margin(db, "dzen_search", 4000)
        set_setting(db, config, actors[0], "ai_content_search_tokens", 400000)
        ai_tokens.grant(db, owners[1], bot, True, f"large-search:{owners[1]}", 500000)
        op = ai_tokens.reserve(
            db,
            owners[1],
            bot,
            True,
            f"search:{owners[1]}",
            400000,
            {"purpose": "content_search", "model": "apify"},
            kind="search",
        )
        assert op.reserve_tokens == 400000


@pytest.mark.integration
def test_ai_tokens_do_not_require_or_activate_core_subscription(request):
    engine, config, actors, owners, bot = request.getfixturevalue("account_fixture")
    with Session(engine) as db:  # rollback all temporary policy changes
        db.execute(delete(AccessGrant).where(AccessGrant.user_id == owners[1]))
        set_setting(db, config, actors[0], "ai_dzen_enabled", True)
        set_setting(db, config, actors[0], "ai_free_dzen", 0)
        ai_tokens.grant(db, owners[1], bot, True, f"separate-ai:{owners[1]}", 10000)
        with pytest.raises(ValueError, match="Доступ закончился"):
            require_access(db, owners[1])
        agents.allowed(db, owners[1], bot, True, config=config)
        assert ai_tokens.funding(db, config, owners[1], bot, True, {"purpose": "dzen"}) == "balance"
        assert not can_check_subscriptions(db, owners[1])


@pytest.mark.integration
def test_account_status_trials_money_tokens_and_private_history(request):
    engine, config, actors, owners, bot = request.getfixturevalue("account_fixture")
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owners[1], bot, True, f"own-visible-grant:{owners[1]}", 12400)
        ai_tokens.grant(db, owners[2], bot, True, f"foreign-hidden-grant:{owners[2]}", 99999)
        first = account_overview.overview(db, config, actors[1], bot)
        assert first["tokens"] == 12400
        assert "Подписка не активирована" in first["summary"]
        assert "12 400" in first["summary"]
        db.add(
            AccessGrant(
                user_id=owners[1],
                kind="paid",
                starts_at=datetime.now(UTC),
                ends_at=datetime.now(UTC) + timedelta(days=30),
                source="dummy",
                action_key=f"dummy-core-subscription:{owners[1]}",
            )
        )
        db.flush()
        assert (
            "Подписка активна до"
            in account_overview.overview(db, config, actors[1], bot)["summary"]
        )
        assert ai_tokens.available(db, owners[1], bot, True) == 12400
        text, more = account_overview.history(db, config, actors[1], bot)
        assert "12 400" in text and "99 999" not in text and not more
        assert "deepseek" not in text and "API" not in text
        policy(db).mode = "free"
        value = account_overview.overview(db, config, actors[1], bot)
        assert "доступны бесплатно" in value["summary"]
        assert not any(
            code in {"wallet:buy", "access:renew"} for row in account_rows(value) for _, code in row
        )


@pytest.mark.integration
def test_account_telegram_arbitrary_amount_and_cancel(request):
    engine, config, actors, owners, bot_id = request.getfixturevalue("account_fixture")
    with Session(engine) as db, db.begin():
        db.add(
            WalletAccount(
                owner_user_id=owners[1], bot_id=bot_id, test_mode=True, balance=Decimal("500")
            )
        )

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(build_account_router(engine, config))
        state = dp.fsm.get_context(bot=bot, chat_id=actors[1], user_id=actors[1])
        await state.update_data(mode="expert")
        calls = []
        actor = User(id=actors[1], is_bot=False, first_name="Dummy")
        chat = Chat(id=actor.id, type="private")

        async def api(_, method, **kwargs):
            calls.append(method)
            if isinstance(method, SendMessage):
                return Message(message_id=len(calls), date=datetime.now(UTC), chat=chat)
            return True

        async def click(code):
            await dp.feed_update(
                bot,
                Update(
                    update_id=len(calls) + 1,
                    callback_query=CallbackQuery(
                        id=str(len(calls) + 1),
                        from_user=actor,
                        chat_instance="dummy",
                        data=code,
                        message=Message(message_id=1, date=datetime.now(UTC), chat=chat),
                    ),
                ),
            )

        async def say(text):
            await dp.feed_update(
                bot,
                Update(
                    update_id=len(calls) + 1,
                    message=Message(
                        message_id=len(calls) + 1,
                        date=datetime.now(UTC),
                        chat=chat,
                        from_user=actor,
                        text=text,
                    ),
                ),
            )

        try:
            with patch.object(bot.session, "make_request", side_effect=api):
                await click("account:open")
                await click("ait:packages")  # old link now opens amount input, not packages
                assert await state.get_state() == "ai_token_purchase_amount"
                await say("199")
                assert "200.00" in [x for x in calls if isinstance(x, SendMessage)][-1].text
                await say("200,00")
                draft = (await state.get_data())["token_money_offer"]
                assert "666 666" in [x for x in calls if isinstance(x, SendMessage)][-1].text
                old_code = "account:buytokens:" + draft["nonce"]
                await click("account:open")
                await click(old_code)
                assert "устарело" in calls[-1].text
                await click("account:tokens")
                await say("200")
                draft = (await state.get_data())["token_money_offer"]
                await click("account:buytokens:" + draft["nonce"])
                assert (
                    "Рублёвый баланс: 300.00"
                    in [x for x in calls if isinstance(x, SendMessage)][-1].text
                )
                await click("account:history:0")
                assert (
                    "Покупка AI-токенов"
                    in [x for x in calls if isinstance(x, SendMessage)][-1].text
                )
        finally:
            await dp.storage.close()
            await bot.session.close()

    asyncio.run(scenario())
