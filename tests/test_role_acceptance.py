"""End-to-end role switching through the real dispatcher, without real Bot API calls."""
# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

import asyncio
from datetime import UTC, datetime
from secrets import randbelow
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, GetMe, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import ai_tokens
from leedbox.application.materials import publish
from leedbox.application.profile import delete_my_data
from leedbox.domain.materials import Content
from leedbox.infrastructure.models import AccessGrant, AIAccount, AIOperation, DeliveryEvent
from leedbox.infrastructure.models import User as DbUser
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage


@pytest.mark.integration
@pytest.mark.parametrize("role", ["owner", "expert", "new_recipient"])
@pytest.mark.parametrize("kind", ["text", "url", "document", "photo", "video"])
def test_same_identity_receives_gifts_and_works_as_expert(service_fixture, role, kind):
    engine, config, actors, owners, projects, *_ = service_fixture
    telegram = (
        actors[0]
        if role == "owner"
        else actors[2]
        if role == "expert"
        else 2_000_000_000 + randbelow(10_000_000)
    )
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    contents = {
        "text": "Полезный материал",
        "url": "https://example.com/gift",
        "document": "fixture_document",
        "photo": "fixture_photo",
        "video": "fixture_video",
    }
    with Session(engine) as db, db.begin():
        material = publish(
            db,
            owners[1],
            projects[1],
            bot_id,
            Content("Подарок другого эксперта", "", kind, contents[kind]),
            "everyone",
            None,
        )
        slug, material_id = material.slug, material.id

    async def run():
        bot = Bot(config.bot_token.get_secret_value())
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        actor = User(id=telegram, is_bot=False, first_name="Role QA")
        bot_user = User(id=bot.id, is_bot=True, first_name="Test", username="role_qa_bot")
        chat = Chat(id=telegram, type="private")
        calls, sequence = [], 0
        screen_id = 1000
        key = StorageKey(bot.id, telegram, telegram)

        async def api(_bot, method, timeout=None):
            nonlocal screen_id
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            screen_id = getattr(method, "message_id", None) or len(calls) + 1000
            return Message(
                message_id=screen_id, date=datetime.now(UTC), chat=chat, from_user=bot_user
            )

        async def message(text):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    message=Message(
                        message_id=sequence,
                        date=datetime.now(UTC),
                        chat=chat,
                        from_user=actor,
                        text=text,
                    ),
                ),
            )

        async def click(code):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        chat_instance="role-qa",
                        from_user=actor,
                        data=code,
                        message=Message(
                            message_id=screen_id,
                            date=datetime.now(UTC),
                            chat=chat,
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        def identity():
            with Session(engine) as db:
                return db.scalar(select(DbUser.id).where(DbUser.telegram_id == telegram))

        try:
            with patch.object(bot.session, "make_request", side_effect=api):
                if role != "new_recipient":
                    await message("/start")
                    assert (await storage.get_data(key))["mode"] == "expert"
                initial_id = identity()
                gift_start = len(calls)
                await message("/start " + slug)
                recipient_data = await storage.get_data(key)
                assert recipient_data["mode"] == "recipient"
                assert identity() == initial_id
                gift_calls = calls[gift_start:]
                assert not any(
                    isinstance(call, SendPhoto)
                    and call.caption
                    and "Выберите раздел" in call.caption
                    for call in gift_calls
                )
                assert not any(
                    "section:" in str(getattr(call, "reply_markup", "")) for call in gift_calls
                )
                assert not any(
                    "consent:" in str(getattr(call, "reply_markup", "")) for call in gift_calls
                )

                # Expert callbacks cannot escape the recipient flow, even for the service owner.
                for code in (
                    "section:owner",
                    "section:agents",
                    "section:wallet",
                    "m:new",
                    "ip:begin",
                    "wp:open",
                ):
                    await click(code)
                    assert (await storage.get_data(key))["mode"] == "recipient"
                if role == "new_recipient":
                    await click("menu")
                    assert (await storage.get_data(key))["mode"] == "recipient"
                    assert identity() is None

                # Becoming an expert is an explicit /start, not a side effect of receiving a gift.
                await message("/start")
                user_id = identity()
                assert user_id is not None
                if initial_id is not None:
                    assert user_id == initial_id
                await message("/start")
                with Session(engine) as db, db.begin():
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(AccessGrant)
                            .where(AccessGrant.user_id == user_id, AccessGrant.kind == "trial")
                        )
                        == 1
                    )
                    ai_tokens.grant(db, user_id, bot.id, True, f"role-qa:{telegram}", 3210)

                await click("m:new")
                draft = (await storage.get_data(key))["material_draft"]
                await click(f"mw:{draft['revision']}:kind:text")
                await message("Черновик собственного подарка")
                before_gift = (await storage.get_data(key))["material_draft"]
                assert before_gift["step"] > 0
                await message("/start " + slug)
                assert (await storage.get_data(key))["mode"] == "recipient"
                await click("menu")
                assert (await storage.get_data(key))["mode"] == "expert"
                restored = PostgresStorage(engine)
                assert (await restored.get_data(key))["material_draft"] == before_gift
                await click("m:resume")
                assert (await storage.get_data(key))["material_draft"] == before_gift
                with Session(engine) as db:
                    assert ai_tokens.available(db, user_id, bot.id, True) == 3210
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(DeliveryEvent)
                            .where(
                                DeliveryEvent.material_id == material_id,
                                DeliveryEvent.telegram_user_id == telegram,
                                DeliveryEvent.status == "sent",
                            )
                        )
                        == 1
                    )
                    assert identity() == user_id
                await click(f"mat:{material_id}")
                assert isinstance(calls[-1], AnswerCallbackQuery) and calls[-1].show_alert
                assert not any(
                    "Что-то пошло не так" in (getattr(call, "text", "") or "") for call in calls
                )
        finally:
            await storage.set_state(key, None)
            await storage.set_data(key, {})
            await bot.session.close()
            await storage.close()

    try:
        asyncio.run(run())
    finally:
        with Session(engine) as db, db.begin():
            user_id = db.scalar(select(DbUser.id).where(DbUser.telegram_id == telegram))
            if user_id is not None:
                db.execute(
                    delete(AIOperation).where(
                        AIOperation.owner_user_id == user_id, AIOperation.bot_id == bot_id
                    )
                )
                db.execute(
                    delete(AIAccount).where(
                        AIAccount.owner_user_id == user_id, AIAccount.bot_id == bot_id
                    )
                )
            if role == "new_recipient":
                delete_my_data(db, telegram)
