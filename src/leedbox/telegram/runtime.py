"""Polling entry point; business handlers are added in stage 1."""

import asyncio
import hashlib
from contextlib import suppress

from aiogram import BaseMiddleware, Bot, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import PRODUCTION, TEST
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import Update
from sqlalchemy import text
from sqlalchemy.engine import make_url

from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.logging import configure_logging
from leedbox.telegram.button_styles import ButtonStyleMiddleware
from leedbox.telegram.content_agent import background as content_background
from leedbox.telegram.handlers import build_router
from leedbox.telegram.payment_polling import create_payment_dispatcher
from leedbox.telegram.storage import PostgresStorage
from leedbox.telegram.system_proxy import SystemProxySession
from leedbox.telegram.worker import background


class LocalSandboxActorMiddleware(BaseMiddleware):
    """Keep a local payment/AI acceptance run private to its designated tester."""

    def __init__(self, actor_id: int) -> None:
        if actor_id <= 0:
            raise ValueError("A positive local sandbox actor is required")
        self.actor_id = actor_id

    @staticmethod
    def _actor_id(update: Update) -> int | None:
        # The local sandbox is strictly private-chat only. In particular, do
        # not let the allowlisted tester accidentally exercise routes in groups.
        if update.message is not None:
            if update.message.chat.type != "private":
                return None
            return update.message.from_user.id if update.message.from_user else None
        if update.callback_query is not None:
            message = update.callback_query.message
            if message is None or getattr(message, "chat", None) is None:
                return None
            if message.chat.type != "private":
                return None
            return update.callback_query.from_user.id

        # Other user-originated updates are allowed only from the selected ID;
        # updates without a user are discarded in the isolated sandbox.
        for name in (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "business_message",
            "edited_business_message",
            "callback_query",
            "inline_query",
            "chosen_inline_result",
            "shipping_query",
            "pre_checkout_query",
            "poll_answer",
            "my_chat_member",
            "chat_member",
            "chat_join_request",
        ):
            event = getattr(update, name, None)
            if event is None:
                continue
            user = getattr(event, "from_user", None) or getattr(event, "user", None)
            if user is not None:
                return user.id
        return None

    async def __call__(self, handler, event, data):
        if not isinstance(event, Update) or self._actor_id(event) != self.actor_id:
            return None
        return await handler(event, data)


def _validate_local_sandbox(settings: Settings, actor_id: int) -> None:
    url = make_url(settings.database_url.get_secret_value())
    if not (
        settings.environment == "development"
        and settings.robokassa_enabled
        and settings.robokassa_test
        and not settings.stars_enabled
        and url.host in {"localhost", "127.0.0.1"}
        and url.port == 5434
        and (url.database or "").startswith("leedbox_robo_sandbox_")
        and (url.database or "").endswith("_test")
        and actor_id > 0
        and not settings.is_owner(actor_id)
    ):
        raise ValueError("An isolated local sandbox and a non-owner tester are required")


def create_bot(settings: Settings) -> Bot:
    if settings.telegram_system_proxy and settings.environment != "development":
        raise ValueError("System proxy transport is allowed only in local development")
    session_type = SystemProxySession if settings.telegram_system_proxy else AiohttpSession
    bot = Bot(
        settings.bot_token.get_secret_value(),
        session=session_type(api=TEST if settings.telegram_test_api else PRODUCTION),
    )
    bot.session.middleware(ButtonStyleMiddleware())
    return bot


async def run(
    settings: Settings,
    *,
    local_test_router: Router | None = None,
    local_test_actor: int | None = None,
) -> None:
    if local_test_router is not None and settings.environment != "development":
        raise ValueError("Local test routes are allowed only in development")
    if (local_test_router is None) != (local_test_actor is None):
        raise ValueError("A local test router and its designated actor must be provided together")
    if local_test_router is not None:
        _validate_local_sandbox(settings, local_test_actor or 0)
    token = settings.bot_token.get_secret_value()
    if not token:
        raise ValueError("BOT_TOKEN is required for polling")
    lock_key = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], signed=True)
    engine = create_database_engine(settings)
    try:
        # Session-level advisory lock prevents two polling instances across hosts.
        with engine.connect() as connection:
            locked = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
            if not locked:
                raise RuntimeError("A polling instance is already running")
            connection.commit()
            try:
                async with create_bot(settings) as bot:
                    info = await bot.get_webhook_info()
                    if info.url:
                        raise RuntimeError("Remove the existing webhook before starting polling")
                    dispatcher = create_payment_dispatcher(
                        engine,
                        storage=PostgresStorage(engine),
                        events_isolation=SimpleEventIsolation(),
                    )
                    if local_test_router is not None:
                        dispatcher.update.outer_middleware(
                            LocalSandboxActorMiddleware(local_test_actor or 0)
                        )
                        dispatcher.include_router(local_test_router)
                    dispatcher.include_router(build_router(engine, settings))
                    worker = asyncio.create_task(background(bot, engine, settings))
                    content_worker = asyncio.create_task(content_background(bot, engine, settings))
                    try:
                        await dispatcher.start_polling(
                            bot, allowed_updates=dispatcher.resolve_used_update_types()
                        )
                    finally:
                        worker.cancel()
                        content_worker.cancel()
                        with suppress(asyncio.CancelledError):
                            await worker
                        with suppress(asyncio.CancelledError):
                            await content_worker
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                connection.commit()
    finally:
        engine.dispose()


def main() -> None:
    configure_logging()
    asyncio.run(run(Settings()))


if __name__ == "__main__":
    main()
