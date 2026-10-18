"""One URL in, one clck.su short URL out. No resume UI."""

import asyncio
from html import escape
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    LinkPreviewOptions,
    Message,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application.projects import ensure_project, register_user
from leedbox.application.short_links import create_short_link
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.smart_links import app_target
from leedbox.infrastructure.clck import ShortenerError
from leedbox.infrastructure.config import Settings
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message, retire


def build_smart_link_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    @router.callback_query(F.data == "sl:new:deeplink")
    async def removed(callback: CallbackQuery) -> None:
        await callback.answer("DeepLink убран. Откройте /utm", show_alert=True)

    @router.callback_query(F.data == "sl:new:short", flags={"requires_access": True})
    async def begin(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Откройте /start для инструментов", show_alert=True)
            return
        kind = (callback.data or "").split(":")[-1]
        if not config.clck_api_key.get_secret_value():
            await callback.answer("Владелец ещё не настроил ключ API clck.su", show_alert=True)
            return

        def context() -> int:
            with Session(engine) as session, session.begin():
                user = register_user(session, callback.from_user.id)
                return ensure_project(session, user.id).id

        await retire(bot, callback.from_user.id, (data.get("smart_link_draft") or {}).get("screen"))
        sent = await next_message(
            callback.message,
            "🔗 Короткая ссылка\n\nПришлите одну ссылку или домен.\n"
            "Получите короткую ссылку clck.su и кнопку копирования.\n\n"
            "Например: example.ru/catalog?campaign=sale\n\n"
            "Ссылка будет передана сервису clck.su. Не отправляйте секретные ссылки.",
            reply_markup=keyboard([[("← Ссылки", "section:links")], [("🏠 В меню", "menu")]]),
        )
        await state.update_data(
            smart_link_draft={
                "kind": kind,
                "token": uuid4().hex,
                "project_id": await asyncio.to_thread(context),
                "screen": sent.message_id,
            },
            mode="expert",
            paused=False,
            active_tool="smart_links",
        )
        await state.set_state("smart_link_input")
        await callback.answer()

    @router.message(
        StateFilter("smart_link_input"), ~F.text.startswith("/"), flags={"requires_access": True}
    )
    async def accept(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("smart_link_draft")
        if data.get("mode") != "expert" or data.get("paused") or not draft:
            return
        if draft.get("kind") != "short":
            await state.set_state(None)
            await state.update_data(paused=True)
            await message.answer("DeepLink убран. Откройте /utm для доступных инструментов.")
            return
        try:
            target = app_target(message.text or "")

            def save() -> str:
                with Session(engine) as session, session.begin():
                    user = register_user(session, message.chat.id)
                    user_id = user.id
                return create_short_link(
                    engine,
                    user_id,
                    draft["project_id"],
                    bot.id,
                    target.destination,
                    "smart:" + draft["token"],
                    config.clck_api_key.get_secret_value(),
                )

            result = await asyncio.to_thread(save)
        except ShortenerError as error:
            await message.answer("⚠️ " + str(error))
            return
        except ValueError as error:
            await message.answer("⚠️ " + str(error) + "\n\nПришлите корректный адрес http/https.")
            return
        except ObjectUnavailable:
            await message.answer(
                "Проект недоступен. Откройте раздел «Ссылки» и создайте ссылку заново."
            )
            return
        await retire(bot, message.chat.id, draft.get("screen"))
        markup = keyboard([[("🔗 Другие ссылки", "section:links")], [("🏠 В меню", "menu")]])
        if len(result) <= 256:
            markup.inline_keyboard.insert(
                0,
                [
                    InlineKeyboardButton(
                        text="📋 Копировать ссылку",
                        copy_text=CopyTextButton(text=result),
                    )
                ],
            )
        title = "✅ Короткая ссылка готова"
        text = f"<b>{title}</b>\n\n<pre>{escape(result)}</pre>"
        sent = await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        draft["screen"] = sent.message_id
        await state.set_state(None)
        await state.update_data(smart_link_draft=draft, paused=True)

    return router
