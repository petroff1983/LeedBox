import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import access_for
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import User
from leedbox.telegram.navigation import AGENT_ACTIVE_TOOLS, AGENT_CALLBACK_PREFIXES


class AccessMiddleware(BaseMiddleware):
    """Mark expert tools requires_access; recipient routes must never carry this flag."""

    def __init__(self, engine: Engine, config: Settings | None = None) -> None:
        self.engine = engine
        self.config = config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not get_flag(data, "requires_access") or not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        state = data["state"]
        if (await state.get_data()).get("mode") != "expert":
            return await handler(event, data)
        # Cancelling never requires a subscription and does not change published materials.
        if isinstance(event, CallbackQuery) and (event.data or "").endswith(":cancel"):
            return await handler(event, data)
        if event.from_user is None:
            return None
        telegram_id = event.from_user.id
        if self.config and self.config.is_owner(telegram_id):
            return await handler(event, data)

        def allowed() -> bool:
            with Session(self.engine) as session:
                user_id = session.scalar(select(User.id).where(User.telegram_id == telegram_id))
                if user_id is not None and self.config:
                    is_agent = (
                        (
                            isinstance(event, Message)
                            and active_tool in AGENT_ACTIVE_TOOLS
                            and not (event.text or "").startswith("/")
                        )
                        or (
                            isinstance(event, CallbackQuery)
                            and (
                                (event.data or "").startswith(AGENT_CALLBACK_PREFIXES)
                                or event.data == "section:youtube"
                            )
                        )
                        or (
                            isinstance(event, Message)
                            and (event.text or "").partition(" ")[0].split("@", 1)[0] == "/youtube"
                        )
                    )
                    if is_agent:
                        # Agent trials and token balances are independent of the core subscription.
                        return True
                return user_id is not None and access_for(session, user_id).allowed

        active_tool = (await state.get_data()).get("active_tool")
        if await asyncio.to_thread(allowed):
            return await handler(event, data)
        text = "Доступ закончился. Откройте «Мой доступ» в профиле для продления"
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None
