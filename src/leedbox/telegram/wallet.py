"""Private expert balance. No invoice is settled from a browser return."""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application import wallet
from leedbox.application.account_overview import overview
from leedbox.application.settings import setting
from leedbox.domain.payments import InvalidPayment, payment_amount
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import User
from leedbox.telegram.account import account_rows
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.material_wizard import buttons


def build_wallet_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    def owner(db: Session, actor: int) -> int:
        value = db.scalar(
            select(User.id).where(User.telegram_id == actor, User.expert_started_at.is_not(None))
        )
        if value is None:
            raise InvalidPayment("Откройте /start для инструментов эксперта")
        return value

    async def render(message: Message, state: FSMContext, bot: Bot, actor: int) -> None:
        def read() -> dict[str, Any]:
            with Session(engine) as db:
                return overview(db, config, actor, bot.id)

        value = await asyncio.to_thread(read)
        await state.set_state(None)
        await state.update_data(paused=True, wallet_offer=None, wallet_nonce=None)
        test = (
            "\n🧪 Тестовый баланс — настоящий доступ не выдаётся.\n"
            if config.robokassa_test
            else ""
        )
        await next_message(
            message,
            "👤 Мой аккаунт\n\n"
            + value["summary"]
            + test
            + ("\nЕсть задолженность. Обратитесь в поддержку." if value["rubles"] < 0 else "")
            + "\n\nПополнение добавляет рубли. Токены и подписка покупаются отдельно.",
            reply_markup=buttons(account_rows(value)),
        )

    @router.message(Command("balance"))
    async def command(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or (await state.get_data()).get("mode") != "expert":
            return
        try:
            await render(message, state, bot, message.from_user.id)
        except InvalidPayment as error:
            await message.answer(str(error))

    @router.callback_query(F.data.startswith("wallet:") | (F.data == "section:wallet"))
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Откройте /start", show_alert=True)
            return
        code = callback.data or ""
        url: str | None = None
        try:
            if code in {"wallet:open", "section:wallet"}:
                await render(callback.message, state, bot, callback.from_user.id)
            elif code == "wallet:topup":

                def available() -> bool:
                    with Session(engine) as db:
                        return bool(
                            setting(db, "wallet_visible")
                            and setting(db, "wallet_enabled")
                            and setting(db, "wallet_topup_enabled")
                            and config.robokassa_enabled
                        )

                if not await asyncio.to_thread(available):
                    raise InvalidPayment("Пополнение пока выключено владельцем")
                await state.set_state("wallet_amount")
                await state.update_data(paused=False, active_tool="wallet", wallet_offer=None)
                await next_message(
                    callback.message,
                    "➕ Пополнение баланса\n\nВведите сумму в рублях: например 500 или 500,50.\n"
                    "Минимальное пополнение — 200 ₽, максимум — 100 000 ₽.",
                    reply_markup=buttons([[("⬅️ Назад", "wallet:open")]]),
                )
            elif code == "wallet:buy":

                def offer() -> dict[str, Any]:
                    with Session(engine) as db:
                        owner(db, callback.from_user.id)
                        return wallet.access_offer(db, config)

                value = await asyncio.to_thread(offer)
                nonce = uuid4().hex[:16]
                await state.update_data(
                    paused=True, wallet_offer=value, wallet_nonce=nonce, wallet_kind="buy"
                )
                await next_message(
                    callback.message,
                    f"🔑 Доступ на {value['days']} дней\n\nСтоимость: {value['price']} ₽\n\n"
                    f"{value['terms']}\nПоддержка: {value['support']}",
                    reply_markup=buttons(
                        [
                            [("✅ Согласен, оплатить с баланса", "wallet:confirm:" + nonce)],
                            [("⬅️ Назад", "wallet:open")],
                        ]
                    ),
                )
            elif code.startswith("wallet:checkout:"):
                from leedbox.domain.payments import invoice_id

                number = invoice_id(code.split(":")[-1])

                def checkout() -> str:
                    with Session(engine) as db:
                        uid = owner(db, callback.from_user.id)
                        return wallet.topup_checkout(db, config, uid, bot.id, number)

                url = await asyncio.to_thread(checkout)
                markup = buttons([[("🔄 Проверить баланс", "wallet:open")]])
                markup.inline_keyboard.insert(
                    0, [InlineKeyboardButton(text="💳 СБП / Robokassa", url=url, style="success")]
                )
                await next_message(
                    callback.message,
                    "Продолжить ранее подтверждённое пополнение.",
                    reply_markup=markup,
                )
            elif code.startswith("wallet:confirm:"):
                saved_offer = data.get("wallet_offer")
                saved_nonce = data.get("wallet_nonce")
                if not saved_offer or code != "wallet:confirm:" + str(saved_nonce):
                    raise InvalidPayment("Подтверждение устарело. Откройте баланс заново")

                def pay() -> str | None:
                    with Session(engine) as db, db.begin():
                        uid = owner(db, callback.from_user.id)
                        key = f"wallet:{callback.from_user.id}:{saved_nonce}"
                        if data.get("wallet_kind") == "buy":
                            wallet.buy_access(db, config, uid, bot.id, saved_offer, key, True)
                            return None
                        order = wallet.create_topup(db, config, uid, bot.id, saved_offer, key, True)
                        return wallet.topup_checkout(db, config, uid, bot.id, order.id)

                url = await asyncio.to_thread(pay)
                if url:
                    markup = buttons(
                        [[("🔄 Проверить баланс", "wallet:open")], [("⬅️ Назад", "wallet:open")]]
                    )
                    markup.inline_keyboard.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                text="💳 Перейти к СБП / Robokassa", url=url, style="success"
                            )
                        ],
                    )
                    await next_message(
                        callback.message,
                        "Счёт создан. Оплатите на странице Robokassa, "
                        "затем нажмите «Проверить баланс»."
                        + ("\n🧪 Это тестовая оплата." if config.robokassa_test else ""),
                        reply_markup=markup,
                    )
                else:
                    await next_message(
                        callback.message,
                        "✅ Тестовая покупка проверена, настоящий доступ не выдан."
                        if config.robokassa_test
                        else "✅ Подписка активирована. Оплата списана с рублёвого баланса.\n"
                        "AI-токены не изменились. Срок подписки — в «Моём аккаунте».",
                        reply_markup=buttons(
                            [[("💳 Баланс", "wallet:open")], [("🏠 В меню", "menu")]]
                        ),
                    )
            else:
                raise InvalidPayment("Экран устарел. Откройте баланс заново")
        except InvalidPayment as error:
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer()

    @router.message(StateFilter("wallet_amount"), ~F.text.startswith("/"))
    async def amount(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if (
            not message.from_user
            or data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "wallet"
        ):
            return
        actor = message.from_user.id
        try:
            try:
                value = payment_amount(
                    (message.text or "").replace(" ", "").replace(",", "."), "RUB"
                )
            except InvalidPayment:
                raise InvalidPayment(
                    "Введите положительную сумму: например 500 или 500,50"
                ) from None

            def offer() -> dict[str, Any]:
                with Session(engine) as db:
                    owner(db, actor)
                    return wallet.topup_offer(db, config, bot.id, Decimal(value))

            result = await asyncio.to_thread(offer)
            nonce = uuid4().hex[:16]
            await state.set_state(None)
            await state.update_data(
                wallet_offer=result, wallet_nonce=nonce, wallet_kind="topup", paused=True
            )
            await next_message(
                message,
                f"➕ Пополнить на {result['amount']} ₽\n\n{result['terms']}\n"
                f"Поддержка: {result['support']}",
                reply_markup=buttons(
                    [
                        [("✅ Согласен, получить ссылку СБП", "wallet:confirm:" + nonce)],
                        [("⬅️ Назад", "wallet:open")],
                    ]
                ),
            )
        except InvalidPayment as error:
            await message.answer(str(error), reply_markup=buttons([[("⬅️ Назад", "wallet:open")]]))

    return router
