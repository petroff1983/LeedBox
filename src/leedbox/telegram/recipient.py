"""Recipient routes never import expert menus, advertising or payment screens."""

import asyncio

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.notifications import consent_for
from leedbox.application.settings import product_event, setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import Material
from leedbox.telegram.delivery import deliver, find_material


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=code) for text, code in row]
            for row in rows
        ]
    )


async def result(
    bot: Bot, engine: Engine, user_id: int, payload: str, token: str | None = None
) -> None:
    text = await deliver(bot, engine, user_id, payload, token)
    if text is None:
        return  # The gift already carries its signature and actions.
    material = await asyncio.to_thread(find_material, engine, bot.id, payload)
    if material:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                *(
                    [[InlineKeyboardButton(text="📢 Подписаться на канал", url=material.join_url)]]
                    if material.mode == "subscribers" and material.join_url
                    else []
                ),
                [
                    InlineKeyboardButton(
                        text="✅ Проверить ещё раз", callback_data=f"get:{material.slug}"
                    )
                ],
            ]
        )
        await bot.send_message(user_id, text, reply_markup=markup)
        return
    await bot.send_message(user_id, text)


def record_start(
    engine: Engine, bot_id: int, user_id: int, material_id: int | None, payload: str
) -> None:
    with Session(engine) as session, session.begin():
        unavailable = material_id is None
        reason = "unavailable"
        if unavailable:
            slug = payload.rsplit("_", 1)[0] if payload.count("_") == 2 else payload
            material = session.scalar(
                select(Material).where(Material.bot_id == bot_id, Material.slug == slug)
            )
            if material:
                material_id = material.id
                reason = "archived" if material.archived else "access_expired"
        product_event(session, bot_id, user_id, "user_started", material_id, kind="recipient")
        if unavailable:
            product_event(session, bot_id, user_id, "delivery_denied", material_id, reason=reason)


async def recipient_start(bot: Bot, engine: Engine, message: Message, payload: str) -> None:
    if not message.from_user:
        return
    material = await asyncio.to_thread(find_material, engine, bot.id, payload)
    if material:
        from leedbox.application import legal

        with Session(engine) as db:
            policy = legal.current(db, "privacy")
        if policy:
            await message.answer(
                "Для выдачи подарка бот обрабатывает Telegram ID и событие получения. "
                "Условия обработки доступны в документах. Согласие на рекламу не требуется.",
                reply_markup=buttons([[("📄 Документы и согласия", "legal:open")]]),
            )

    await asyncio.to_thread(
        record_start,
        engine,
        bot.id,
        message.from_user.id,
        material.id if material else None,
        payload,
    )
    if material is None:
        # Keep the neutral unavailable message for archived links.
        await message.answer("Материал сейчас недоступен")
    elif material.mode == "everyone":
        await result(bot, engine, message.from_user.id, payload)
    else:
        await message.answer(
            f"Привет! 👋\n\n🎁 Ваш подарок: {material.title}\n\n"
            "Чтобы получить его:\n1. Подпишитесь на канал по кнопке ниже.\n"
            "2. Вернитесь сюда и нажмите «Проверить подписку».\n\n"
            "Если уже подписаны — сразу нажмите проверку.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📢 Подписаться на канал", url=material.join_url)],
                    [
                        InlineKeyboardButton(
                            text="✅ Проверить подписку", callback_data=f"get:{material.slug}"
                        )
                    ],
                ]
            ),
        )


def build_recipient_router(engine: Engine) -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("get:") | F.data.startswith("again:"))
    async def get(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "recipient":
            await callback.answer("Откройте ссылку материала заново", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != (3 if parts[0] == "again" else 2):
            await callback.answer("Экран устарел", show_alert=True)
            return
        token = parts[2] if parts[0] == "again" else None
        if token is not None and (
            len(token) != 12 or any(char not in "0123456789abcdef" for char in token)
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        await callback.answer()
        await result(bot, engine, callback.from_user.id, parts[1], token)

    @router.callback_query(F.data.startswith("consent:"))
    async def consent(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        parts = (callback.data or "").split(":")
        if (
            (
                (await state.get_data()).get("mode") != "recipient"
                and (len(parts) < 3 or parts[2] != "revoke")
            )
            or len(parts) != 3
            or not parts[1].isdecimal()
            or parts[2] not in {"show", "grant", "revoke"}
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        if parts[2] == "show":

            def text() -> tuple[str, str]:
                with Session(engine) as session:
                    return str(setting(session, "author_consent_text")), str(
                        setting(session, "author_consent_version")
                    )

            consent_text, version = await asyncio.to_thread(text)
            await state.update_data(consent_offer={"material_id": parts[1], "version": version})

            await bot.send_message(
                callback.from_user.id,
                consent_text,
                reply_markup=buttons([[("Согласен", f"consent:{parts[1]}:grant")]]),
            )
            await callback.answer()
            return

        offer = (await state.get_data()).get("consent_offer")
        if parts[2] == "grant" and (not offer or offer["material_id"] != parts[1]):
            await callback.answer("Сначала откройте текст согласия", show_alert=True)
            return

        def save() -> None:
            with Session(engine) as session, session.begin():
                consent_for(
                    session,
                    bot.id,
                    int(parts[1]),
                    callback.from_user.id,
                    parts[2] == "grant",
                    expected_text_version=offer["version"]
                    if parts[2] == "grant" and offer
                    else None,
                )

        try:
            await asyncio.to_thread(save)
        except (ObjectUnavailable, ValueError) as error:
            await callback.answer(
                str(error) if isinstance(error, ValueError) else "Сначала получите материал",
                show_alert=True,
            )
            return
        await callback.answer(
            "Согласие сохранено" if parts[2] == "grant" else "Согласие отозвано", show_alert=True
        )

    return router
