import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.infrastructure.models import ToolResult
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage


@pytest.mark.integration
def test_ca_to_content_reuse_recovery_and_recipient_guard(request):
    engine, config, actors, owners, *_ = request.getfixturevalue("service_fixture")

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        bot_user = User(id=bot.id, first_name="Synthetic", is_bot=True)
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        key = StorageKey(bot.id, actors[1], actors[1])
        calls, sequence, screen = [], 0, 1000

        async def api(_bot, method, timeout=None):
            nonlocal screen
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            if isinstance(method, SendMessage):
                screen += 1
            return Message(
                message_id=getattr(method, "message_id", None) or screen,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def send(value, callback=False, actor=actors[1]):
            nonlocal sequence
            sequence += 1
            user = User(id=actor, first_name="Synthetic", is_bot=False)
            message = Message(
                message_id=screen if callback else sequence + 2000,
                date=datetime.now(UTC),
                chat=Chat(id=actor, type="private"),
                from_user=bot_user if callback else user,
                text=None if callback else value,
            )
            update = (
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        chat_instance="test",
                        data=value,
                        message=message,
                        from_user=user,
                    ),
                )
                if callback
                else Update(update_id=sequence, message=message)
            )
            await dispatcher.feed_update(bot, update)

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await send("/start")
                await send("ct:new:ca", True)
                await send("Курс Excel для бухгалтеров")
                data = await storage.get_data(key)
                await send(f"tw:{data['text_draft']['revision']}:skip", True)
                fresh = PostgresStorage(engine)
                assert (await fresh.get_data(key))["text_draft"]["step"] == 2
                await send("Для контент-плана")
                source = (await storage.get_data(key))["text_draft"]["result_id"]
                await send(f"cs:from:{source}", True)
                draft = (await storage.get_data(key))["text_draft"]
                assert (
                    draft["step"] == 3
                    and draft["values"]["ca_subject"] == "Курс Excel для бухгалтеров"
                )
                await send("Telegram, один пост в день")
                await send("Экспертный; запись на консультацию")
                await send("Из анализа: боятся сложных формул. " + "я" * 3900)
                draft = (await storage.get_data(key))["text_draft"]
                with Session(engine) as db:
                    result = db.get(ToolResult, draft["result_id"])
                    assert result.tool == "content_strategy"
                    assert "боятся сложных формул" in result.result
                    assert "Курс Excel" in result.result
                await send(f"tw:{draft['revision']}:back", True)
                answer_screen = [call for call in calls if isinstance(call, SendMessage)][-1]
                assert "Показан фрагмент" in answer_screen.text
                assert len(answer_screen.text.encode("utf-16-le")) // 2 <= 4096
                await send("/start", actor=actors[0])
                await send(f"cs:from:{source}", True, actors[0])
                assert calls[-1].show_alert
                await send("/start lm_missing", actor=actors[2])
                await send(f"cs:from:{source}", True, actors[2])
                assert calls[-1].show_alert
                await send("notify:set:advertising:1", True)
                from leedbox.application.broadcasts import preferences

                with Session(engine) as db, db.begin():
                    assert not preferences(db, actors[1], bot.id).advertising
                await send("notify:consent:1", True)
                with Session(engine) as db, db.begin():
                    assert preferences(db, actors[1], bot.id).advertising
                await send("notify:set:advertising:0", True)
                with Session(engine) as db, db.begin():
                    assert not preferences(db, actors[1], bot.id).advertising
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())
