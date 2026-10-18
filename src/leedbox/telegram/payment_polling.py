"""Persist financial events before aiogram advances its getUpdates offset."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.telegram import TEST
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.types import Update
from aiogram.utils.backoff import BackoffConfig
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from leedbox.application.payment_inbox import capture


def create_payment_dispatcher(engine: Engine, **kwargs: Any) -> Dispatcher:
    class PaymentDispatcher(Dispatcher):
        @classmethod
        async def _listen_updates(
            cls,
            bot: Bot,
            polling_timeout: int = 30,
            backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
            allowed_updates: list[str] | None = None,
        ) -> AsyncGenerator[Update, None]:
            async for update in Dispatcher._listen_updates(
                bot, polling_timeout, backoff_config, allowed_updates
            ):
                # Do not resume the base generator (advance its offset) before commit.
                while True:
                    try:
                        await asyncio.to_thread(
                            capture, engine, bot.id, bot.session.api == TEST, update
                        )
                        break
                    except SQLAlchemyError:
                        logging.getLogger(__name__).error("financial_update_persistence_retry")
                        await asyncio.sleep(5)
                yield update

    return PaymentDispatcher(**kwargs)
