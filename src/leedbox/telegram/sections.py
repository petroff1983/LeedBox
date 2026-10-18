"""Live section visibility gate for expert tools, never for recipient or payment events."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application.sections import SECTIONS, visible_sections
from leedbox.telegram.material_ui import CREATE, HOME, LIBRARY

PREFIX_SECTIONS = {
    **dict.fromkeys(
        ("m", "me", "mw", "lib", "mat", "ma", "mc", "mn", "mq", "md", "aud"), "materials"
    ),
    **dict.fromkeys(("calc", "cw", "cr"), "calc"),
    **dict.fromkeys(("ln", "lw", "li", "ur", "lr", "ut", "sl"), "links"),
    **dict.fromkeys(("ct", "tw", "cs", "ap", "oc", "ci", "ob", "obc"), "texts"),
    **dict.fromkeys(("sn", "sf", "kb"), "services"),
    **dict.fromkeys(("project", "pr", "rec"), "profile"),
    "yt": "youtube",
    "az": "agents",
    "wp": "agents",
    "ca": "agents",
    "lm": "agents",
}
ACTIVE_SECTIONS = {
    "material": "materials",
    "calc": "calc",
    "links": "links",
    "utm": "links",
    "smart_links": "links",
    "texts": "texts",
    "objections": "texts",
    "services": "services",
    "project": "profile",
    "profile": "profile",
    "youtube": "youtube",
    "agents": "agents",
    "content_agent": "agents",
    "lead_magnet_agent": "agents",
}
COMMAND_SECTIONS = {
    "calc": "calc",
    "utm": "links",
    "offer": "texts",
    "utp": "texts",
    "youtube": "youtube",
}


def event_section(event: Message | CallbackQuery, state_data: dict[str, Any]) -> str | None:
    if isinstance(event, CallbackQuery):
        code = event.data or ""
        # Privacy, unsubscribe, refunds and financial records remain accessible.
        if code.endswith(":cancel") or code.startswith(
            ("notify:", "delete:", "sw:", "purchase:", "access:")
        ):
            return None
        if code.startswith("section:"):
            section = code.split(":", 1)[1]
            return section if section in SECTIONS else None
        if code.startswith("store:"):
            return (
                "consultations"
                if code in {"store:consultations", "store:contact", "store:channel", "store:blog"}
                else "services"
            )
        return PREFIX_SECTIONS.get(code.split(":", 1)[0])
    if event.successful_payment or event.refunded_payment:
        return None
    text = event.text or ""
    if text == HOME:
        return None
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0][1:].split("@", 1)[0].lower()
        return COMMAND_SECTIONS.get(command)
    if text in {CREATE, LIBRARY}:
        return "materials"
    return (
        None if state_data.get("paused") else ACTIVE_SECTIONS.get(state_data.get("active_tool", ""))
    )


class SectionVisibilityMiddleware(BaseMiddleware):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return await handler(event, data)
        state = data["state"]
        state_data = await state.get_data()
        if state_data.get("mode") != "expert":
            return await handler(event, data)
        section = event_section(event, state_data)
        if section is None:
            return await handler(event, data)

        def enabled() -> bool:
            with Session(self.engine) as db:
                return section in visible_sections(db)

        if await asyncio.to_thread(enabled):
            return await handler(event, data)
        await state.update_data(paused=True)
        text = "Раздел временно скрыт владельцем. Откройте /menu. Сохранённые ответы не удалены."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return None
