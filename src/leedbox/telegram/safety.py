import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application.limits import take
from leedbox.application.settings import product_event, setting


def allow(engine: Engine, bot_id: int, user_id: int, subscription: bool) -> bool:
    with Session(engine) as session, session.begin():
        key = f"{bot_id}:{user_id}"
        ok = take(
            session,
            key + ":requests",
            int(setting(session, "rate_requests")),
            int(setting(session, "rate_window_seconds")),
        )
        if ok and subscription:
            ok = take(
                session,
                key + ":subscription",
                1,
                int(setting(session, "subscription_cooldown_seconds")),
            )
        if not ok and take(session, key + ":warning", 1, 60):
            product_event(session, bot_id, user_id, "suspicious_activity", reason="rate_limited")
        return ok


class LimitMiddleware(BaseMiddleware):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)) or event.from_user is None:
            return await handler(event, data)
        if isinstance(event, Message) and (event.successful_payment or event.refunded_payment):
            # Provider service events must never be dropped by user rate limits.
            return await handler(event, data)
        # Negative chat IDs indicate groups/channels; this service only accepts private scenarios.
        if isinstance(event, Message) and event.chat.type != "private":
            return None
        if (
            isinstance(event, CallbackQuery)
            and isinstance(event.message, Message)
            and event.message.chat.type != "private"
        ):
            return None
        bot = data["bot"]
        subscription = isinstance(event, CallbackQuery) and (event.data or "").startswith(
            ("get:", "again:")
        )
        if await asyncio.to_thread(allow, self.engine, bot.id, event.from_user.id, subscription):
            return await handler(event, data)
        text = "Слишком много запросов. Подождите немного и попробуйте снова"
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None


async def safe_error(event: ErrorEvent) -> bool:
    id = uuid4().hex[:12]
    logging.getLogger(__name__).error("telegram_handler_error", extra={"event_id": id})
    text = f"Что-то пошло не так. Номер события: {id}. Попробуйте ещё раз"
    try:
        if event.update.callback_query:
            await event.update.callback_query.answer(text, show_alert=True)
        elif event.update.message:
            await event.update.message.answer(text)
    except Exception:
        logging.getLogger(__name__).error("error_response_failed", extra={"event_id": id})
    return True
