import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import SecretStr
from sqlalchemy import create_engine

from leedbox.infrastructure.config import Settings
from leedbox.telegram.handlers import build_router, menu


def test_owner_menu_hidden_and_consultations_public() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://user:password@localhost/leedbox_test"),
        owner_ids=frozenset({101}),
    )
    for actor in (101, 202):
        actions = {
            button.callback_data for row in menu(settings, actor).inline_keyboard for button in row
        }
        assert "section:owner" not in actions
        assert "section:consultations" in actions


def test_recipient_does_not_receive_expert_menu_or_old_callbacks() -> None:
    async def scenario() -> None:
        settings = Settings(
            database_url=SecretStr("postgresql+psycopg://user:password@localhost/leedbox_test")
        )
        engine = create_engine(settings.database_url.get_secret_value())
        storage = MemoryStorage()
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, settings))
        bot = Bot("123456:ABCDEF-test-token")
        user = User(id=101, is_bot=False, first_name="Test")
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=101, type="private"),
            from_user=user,
            text="/start lm_abcdefg",
        )
        response = Message(message_id=2, date=datetime.now(UTC), chat=message.chat)
        request = AsyncMock(return_value=response)
        try:
            with (
                patch.object(bot.session, "make_request", request),
                patch("leedbox.telegram.recipient.find_material", return_value=None),
                patch("leedbox.telegram.recipient.record_start"),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await dispatcher.feed_update(bot, Update(update_id=1, message=message))
                sent = request.call_args.args[1]
                assert sent.text == "Материал сейчас недоступен"
                assert sent.reply_markup is None
                assert (await storage.get_data(StorageKey(bot.id, 101, 101)))["mode"] == "recipient"
                await dispatcher.feed_update(
                    bot,
                    Update(
                        update_id=2,
                        callback_query=CallbackQuery(
                            id="old-menu",
                            from_user=user,
                            chat_instance="test",
                            message=response,
                            data="menu",
                        ),
                    ),
                )
                answer = request.call_args.args[1]
                assert answer.show_alert
                assert "Экран устарел" in answer.text
        finally:
            await bot.session.close()
            engine.dispose()

    asyncio.run(scenario())
