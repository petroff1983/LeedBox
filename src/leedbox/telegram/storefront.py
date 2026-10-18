"""Explicit expert-only product catalog and consultation contact screen."""

import asyncio
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, Message
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application.settings import setting
from leedbox.application.storefront import products
from leedbox.domain.services import owner_contact_url, storefront_url
from leedbox.infrastructure.banner_assets import consultation_source
from leedbox.infrastructure.config import Settings
from leedbox.telegram.material_ui import next_message, retire
from leedbox.telegram.material_wizard import buttons
from leedbox.telegram.navigation import pause_input
from leedbox.telegram.texts import ui_text


def product_note(item: dict[str, Any]) -> str:
    if not item["enabled"]:
        return ""
    if all(item[key] for key in ("advertiser", "inn", "erid", "marking_text")):
        return (
            f"{item['name']}: {item['marking_text']}\n"
            f"Рекламодатель: {item['advertiser']} · ИНН {item['inn']} · erid {item['erid']}"
        )
    return f"{item['name']}: партнёрская ссылка."


def product_pages(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = [[]]
    size = 0
    for item in entries:
        length = len(product_note(item)) + 2
        if pages[-1] and (size + length > 1300 or len(pages[-1]) >= 20):
            pages.append([])
            size = 0
        pages[-1].append(item)
        size += length
    return pages


def build_storefront_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("store:") | (F.data == "section:consultations"))
    async def screen(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Откройте /start для инструментов", show_alert=True)
            return
        action = (
            "consultations"
            if callback.data == "section:consultations"
            else (callback.data or "")[6:]
        )
        if action in {"contact", "channel", "blog"}:
            await callback.answer(
                "Ссылка пока не настроена владельцем. Попробуйте позже.", show_alert=True
            )
            return
        page = 0
        if action.startswith("products:"):
            number = action.split(":", 1)[1]
            if not number.isascii() or not number.isdecimal() or len(number) > 4:
                await callback.answer("Экран устарел. Откройте товары заново.", show_alert=True)
                return
            page, action = int(number), "products"
        if action not in {"products", "consultations"}:
            await callback.answer("Экран устарел. Откройте раздел заново.", show_alert=True)
            return
        await pause_input(state)
        if action == "products":
            preview = config.is_owner(callback.from_user.id)

            def read_products() -> list[dict[str, Any]]:
                with Session(engine) as db:
                    return products(db, preview)

            entries = await asyncio.to_thread(read_products)
            pages = product_pages(entries)
            page = page if page < len(pages) else 0
            visible = pages[page]
            text = (await asyncio.to_thread(ui_text, engine, "products_intro"))[:600]
            if not entries:
                text += "\n\nПока нет опубликованных товаров."
            if any(not item["enabled"] for item in visible):
                text += "\n\n🔒 Черновики видны только вам. Публикация — в админке."
            notes = [product_note(item) for item in visible if product_note(item)]
            if notes:
                text += "\n\n" + "\n\n".join(notes)
            markup = buttons([])
            for index in range(0, len(visible), 2):
                markup.inline_keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=(("🔒 " if not item["enabled"] else "") + item["name"])[:64],
                            url=item["url"],
                        )
                        for item in visible[index : index + 2]
                    ]
                )
            nav = []
            if page:
                nav.append(("←", f"store:products:{page - 1}"))
            if page + 1 < len(pages):
                nav.append(("→", f"store:products:{page + 1}"))
            if nav:
                text += f"\n\nСтраница {page + 1}/{len(pages)}"
                markup.inline_keyboard.extend(buttons([nav]).inline_keyboard)
            markup.inline_keyboard.extend(
                buttons([[("⬅️ Назад", "section:services")]]).inline_keyboard
            )
            await next_message(
                callback.message,
                text,
                reply_markup=markup,
            )
        else:

            def read_contact() -> tuple[
                bool, str | None, str | None, str | None, Path | str | None
            ]:
                with Session(engine) as db:
                    return (
                        setting(db, "consultations_enabled"),
                        storefront_url(str(setting(db, "consultation_contact_url")), contact=True)
                        or owner_contact_url(str(setting(db, "owner_contact"))),
                        storefront_url(str(setting(db, "consultation_channel_url")), contact=True),
                        storefront_url(str(setting(db, "consultation_blog_url"))),
                        consultation_source(
                            str(setting(db, "consultation_photo")), config.owner_media_directory
                        ),
                    )

            enabled, url, channel, blog, photo = await asyncio.to_thread(read_contact)
            markup = buttons([[("⬅️ Назад", "menu")]])
            markup.inline_keyboard.insert(
                0,
                [
                    InlineKeyboardButton(text="📢 Мой канал", url=channel)
                    if channel
                    else InlineKeyboardButton(text="📢 Мой канал", callback_data="store:channel"),
                    InlineKeyboardButton(text="✍️ Мой блог", url=blog)
                    if blog
                    else InlineKeyboardButton(text="✍️ Мой блог", callback_data="store:blog"),
                ],
            )
            if enabled:
                markup.inline_keyboard.insert(
                    0,
                    [
                        InlineKeyboardButton(text="💬 Написать", url=url, style="success")
                        if url
                        else InlineKeyboardButton(
                            text="💬 Написать", callback_data="store:contact", style="success"
                        )
                    ],
                )
            greeting = await asyncio.to_thread(ui_text, engine, "consultations_greeting")
            body = (
                await asyncio.to_thread(ui_text, engine, "consultations_intro")
                if enabled
                else "🎓 Консультации и обучение\n\nЗапись временно закрыта."
            )
            combined = greeting + "\n\n" + body
            await retire(bot, callback.message.chat.id, callback.message.message_id)
            if photo:
                try:
                    fits = len(combined.encode("utf-16-le")) // 2 <= 1024
                    await callback.message.answer_photo(
                        FSInputFile(photo) if isinstance(photo, Path) else photo,
                        caption=combined if fits else greeting,
                        parse_mode=None,
                        reply_markup=markup if fits else None,
                    )
                    if fits:
                        await callback.answer()
                        return
                    combined = body
                except (TelegramBadRequest, OSError):
                    # A rejected or missing photo must not hide text and contact actions.
                    pass
            # 1900 codepoints always fit Telegram's 4096 UTF-16-unit message limit.
            chunks = [combined[index : index + 1900] for index in range(0, len(combined), 1900)]
            for index, chunk in enumerate(chunks):
                await callback.message.answer(
                    chunk,
                    parse_mode=None,
                    reply_markup=markup if index == len(chunks) - 1 else None,
                )
        await callback.answer()

    return router
