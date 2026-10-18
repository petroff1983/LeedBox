"""Expert consent gate, separate from gift delivery and optional advertising."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application import legal
from leedbox.infrastructure.config import Settings
from leedbox.telegram.navigation import pause_input


def document_button(label: str, config: Settings, slug: str, number: int) -> InlineKeyboardButton:
    url = legal.public_url(config, slug, number)
    if urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}:
        return InlineKeyboardButton(text="📋 " + label, copy_text=CopyTextButton(text=url))
    return InlineKeyboardButton(text=label, url=url)


async def prompt(
    message: Message,
    state: FSMContext,
    engine: Engine,
    config: Settings,
    actor: int,
    bot: int,
    *,
    onboarding: bool,
) -> None:
    with Session(engine) as db:
        docs = [(slug, doc) for slug in legal.DOCUMENTS if (doc := legal.current(db, slug))]
        accepted = (
            legal.accepted(db, actor, bot) if any(s == "personal-data" for s, _ in docs) else False
        )
        last = legal.last_event(db, actor, bot)
    await pause_input(state)
    nonce = uuid4().hex[:12]
    rows = [[document_button(doc.data["title"], config, slug, doc.number)] for slug, doc in docs]
    lookup = dict(docs)
    ready = "privacy" in lookup and "personal-data" in lookup
    if ready and not accepted:
        await state.update_data(
            legal_offer={
                "nonce": nonce,
                "document": lookup["personal-data"].id,
                "policy": lookup["privacy"].id,
                "onboarding": onboarding,
            }
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Даю согласие на обработку данных", callback_data="legal:grant:" + nonce
                )
            ]
        )
        rows.append([InlineKeyboardButton(text="Не согласен", callback_data="legal:decline")])
    else:
        await state.update_data(legal_offer=None)
    if last and last.action == "grant":
        rows.append([InlineKeyboardButton(text="Отозвать согласие", callback_data="legal:revoke")])
    if not onboarding:
        if (await state.get_data()).get("mode") == "expert":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="👤 Профиль и удаление данных", callback_data="section:profile"
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text="← Назад",
                    callback_data="menu"
                    if (await state.get_data()).get("mode") == "expert"
                    else "legal:close",
                )
            ]
        )
    text = (
        "📄 Перед началом работы\n\nОзнакомьтесь с политикой обработки персональных данных "
        "и отдельным согласием по кнопкам ниже. Для продолжения подтвердите согласие.\n\n"
        "LeedBox сохраняет настройки, вводные и результаты. Материалы для генерации "
        "передаются внешнему ИИ. Не отправляйте пароли, платёжные данные "
        "и чужие персональные данные.\n\n"
        "Рекламная рассылка не включается этим действием."
        if onboarding
        else "📄 Документы и согласия\n\n"
        + (
            "Актуальное согласие подтверждено."
            if accepted
            else "Актуальное согласие не подтверждено."
        )
        + "\nСогласие на рекламу управляется отдельно в уведомлениях. "
        "Получение подарка не зависит от рекламы."
    )
    if not ready:
        text = "📄 Документы пока не опубликованы полностью. Обратитесь к владельцу сервиса."
    if not config.public_base_url and docs:
        text += "\n\nЛокальный режим: кнопки копируют адрес для браузера этого компьютера."
    await message.answer(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    )


class LegalMiddleware(BaseMiddleware):
    def __init__(self, engine: Engine, config: Settings) -> None:
        self.engine, self.config = engine, config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, (Message, CallbackQuery)) or event.from_user is None:
            return await handler(event, data)
        message = event if isinstance(event, Message) else event.message
        if not isinstance(message, Message) or message.chat.type != "private":
            return await handler(event, data)
        state, bot = data["state"], data["bot"]
        if isinstance(event, Message):
            command, _, args = (event.text or "").partition(" ")
            command = command.split("@", 1)[0]
            if (
                event.successful_payment
                or command
                in {"/documents", "/privacy", "/help", "/terms", "/paysupport", "/cancel"}
                or (command == "/start" and args.strip().startswith("lm_"))
            ):
                return await handler(event, data)
            if (await state.get_data()).get("mode") == "recipient" and not (
                event.text or ""
            ).startswith("/"):
                return await handler(event, data)
        else:
            callback = event.data or ""
            if (
                callback.startswith(("legal:", "get:", "again:", "consent:", "notify:", "delete:"))
                or callback in {"help:open", "profile:delete", "section:profile", "account:open"}
                or callback.startswith(("account:history:", "purchase:orders:"))
            ):
                return await handler(event, data)

        actor_id = event.from_user.id

        def check() -> bool:
            with Session(self.engine) as db:
                return legal.needed(db, self.config, actor_id, bot.id)

        if not await asyncio.to_thread(check):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer()
        await prompt(
            message, state, self.engine, self.config, event.from_user.id, bot.id, onboarding=True
        )
        return None


def install_legal(
    router: Router,
    engine: Engine,
    config: Settings,
    resume: Callable[[CallbackQuery, FSMContext], Awaitable[None]],
) -> None:
    @router.message(Command("documents", "privacy"))
    async def documents(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user and message.chat.type == "private":
            await prompt(
                message, state, engine, config, message.from_user.id, bot.id, onboarding=False
            )

    @router.callback_query(F.data.startswith("legal:"))
    async def legal_callback(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
            return
        code, actor = callback.data or "", callback.from_user.id
        message_id = callback.message.message_id
        if code == "legal:decline":
            await state.update_data(legal_offer=None)
            await callback.answer()
            await callback.message.answer(
                "Согласие не дано. Новые задачи эксперта не запускаются, пока включена проверка. "
                "Документы доступны по /documents, помощь — /help. Получить подарок "
                "по его ссылке можно без согласия на рекламу."
            )
            return
        if code in {"legal:open", "legal:close"}:
            await callback.answer()
            if code == "legal:close":
                await callback.message.answer(
                    "Для получения подарка откройте его ссылку; для инструментов — /start."
                )
                return
            await prompt(callback.message, state, engine, config, actor, bot.id, onboarding=False)
            return
        try:
            if code.startswith("legal:grant:"):
                offer = (await state.get_data()).get("legal_offer")
                if not offer or code != "legal:grant:" + offer["nonce"]:
                    raise ValueError("Экран устарел. Откройте /documents или /start.")

                def save() -> None:
                    with Session(engine) as db, db.begin():
                        legal.grant(
                            db,
                            config,
                            actor,
                            bot.id,
                            offer["document"],
                            offer["policy"],
                            "grant:" + callback.id,
                            message_id,
                        )

                await asyncio.to_thread(save)
                await state.update_data(legal_offer=None)
                await callback.answer("Согласие сохранено")
                if offer["onboarding"]:
                    await resume(callback, state)
                else:
                    await prompt(
                        callback.message, state, engine, config, actor, bot.id, onboarding=False
                    )
            elif code == "legal:revoke":
                token = uuid4().hex[:12]
                await state.update_data(legal_revoke=token)
                await callback.answer()
                await callback.message.answer(
                    "Отозвать согласие на обработку данных? Новые задачи эксперта потребуют "
                    "повторного подтверждения, если проверка включена. "
                    "Это не удаляет данные автоматически и не отменяет обязательное "
                    "хранение финансовых документов. Для удаления — раздел профиля "
                    "или поддержка. Для отключения рекламы — уведомления.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Подтверждаю отзыв",
                                    callback_data="legal:withdraw:" + token,
                                )
                            ],
                            [InlineKeyboardButton(text="Отмена", callback_data="legal:open")],
                        ]
                    ),
                )
            elif code.startswith("legal:withdraw:"):
                withdraw_token = (await state.get_data()).get("legal_revoke")
                if not withdraw_token or code != "legal:withdraw:" + withdraw_token:
                    raise ValueError("Экран устарел. Откройте /documents.")
                with Session(engine) as db, db.begin():
                    legal.revoke(
                        db, actor, bot.id, "revoke:" + callback.id, callback.message.message_id
                    )
                await state.update_data(legal_revoke=None, legal_offer=None)
                await callback.answer("Согласие отозвано", show_alert=True)
                await prompt(
                    callback.message, state, engine, config, actor, bot.id, onboarding=False
                )
            else:
                raise ValueError("Откройте /documents.")
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
