"""Single customer account entry with compatible old access buttons."""

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application import account_overview, agents, ai_tokens, credits, wallet
from leedbox.application.settings import setting
from leedbox.domain.payments import payment_amount
from leedbox.infrastructure.config import Settings
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.material_wizard import buttons


def account_rows(value: dict[str, Any]) -> list[list[tuple[str, str]]]:
    rows = []
    if value["topup"]:
        rows.append([("➕ Пополнить баланс", "wallet:topup")])
    if value["packages"]:
        rows.append(
            [
                (
                    "✨ Купить AI-кредиты" if value.get("credits") else "✨ Купить AI-токены",
                    "account:tokens",
                )
            ]
        )
    if value["subscribe"]:
        rows.append([("🔑 Купить / продлить подписку", "wallet:buy")])
    elif value["stars"]:
        rows.append([("🔑 Подписка за Stars", "access:renew")])
    rows += [
        [("🎁 Бесплатные попытки", "account:trials")],
        [("📒 Покупки и расход", "account:history:0")],
        [("🔄 Обновить", "account:open"), ("⬅️ Профиль", "section:profile")],
    ]
    return rows


def build_account_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    @router.callback_query(
        F.data.startswith("account:") | F.data.in_({"profile:access", "ait:packages"})
    )
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (
            (await state.get_data()).get("mode") != "expert"
            or not isinstance(callback.message, Message)
            or callback.message.chat.type != "private"
        ):
            await callback.answer("Откройте /start для инструментов эксперта", show_alert=True)
            return
        code = callback.data or ""
        if code in {"account:open", "profile:access", "account:tokens", "ait:packages"}:
            await state.update_data(token_money_offer=None, ai_package_offer={})
        if code in {"account:tokens", "ait:packages"}:
            with Session(engine) as db:
                credit = credits.enabled(db)
                price = (
                    credits.unit_price(db)
                    if credit
                    else Decimal(str(setting(db, "ai_token_price_rub")))
                )
                minimum = Decimal(str(setting(db, "ai_token_purchase_min_rub")))
                value = account_overview.overview(db, config, callback.from_user.id, bot.id)
            if not value["packages"]:
                await callback.answer("Покупка токенов пока выключена владельцем", show_alert=True)
                return
            await state.set_state("ai_token_purchase_amount")
            await state.update_data(
                paused=False, active_tool="token_purchase", token_money_offer=None
            )
            await next_message(
                callback.message,
                (
                    f"✨ Купить AI-кредиты\n\nЦена: {price:f} ₽ за кредит.\n"
                    if credit
                    else f"✨ Купить AI-токены\n\nЦена: {price:f} ₽ за токен "
                    f"({price * 1000000:.2f} ₽ за 1 млн).\n"
                )
                + f"Рублёвый баланс: {value['rubles']:.2f} ₽\n\n"
                f"Введите сумму от {minimum:.2f} ₽. Перед списанием будет подтверждение.\n"
                "Подписка покупается отдельно.",
                reply_markup=buttons([[("⬅️ Мой аккаунт", "account:open")]]),
            )
            await callback.answer()
            return
        if code.startswith("account:buytokens:"):
            saved = (await state.get_data()).get("token_money_offer") or {}
            if code != "account:buytokens:" + str(saved.get("nonce", "")) or not saved.get("offer"):
                await callback.answer(
                    "Подтверждение устарело. Выберите сумму заново", show_alert=True
                )
                return
            try:
                with Session(engine) as db, db.begin():
                    value = account_overview.overview(db, config, callback.from_user.id, bot.id)
                    op = agents.buy_tokens(
                        db,
                        config,
                        value["owner"],
                        bot.id,
                        "amount:" + saved["nonce"],
                        saved["offer"],
                    )
                    left = op.balance_after
                    assert left is not None
                    rubles = wallet.balance(db, value["owner"], bot.id, saved["offer"]["test"])
            except ValueError as error:
                await callback.answer(str(error), show_alert=True)
                return
            await next_message(
                callback.message,
                f"✅ Зачислено {account_overview.number(saved['offer']['tokens'])} "
                + ("AI-кредитов.\n" if saved["offer"].get("unit") == "credits" else "AI-токенов.\n")
                + f"Списано: {saved['offer']['price_rub']} ₽\n"
                f"Рублёвый баланс: {rubles:.2f} ₽\n"
                f"Остаток AI-баланса: {account_overview.number(left)}\n"
                "Подписка и её срок не изменились."
                + ("\n🧪 Тестовая покупка." if saved["offer"]["test"] else ""),
                reply_markup=buttons([[("👤 Мой аккаунт", "account:open")]]),
            )
            await callback.answer()
            return

        def read() -> tuple[str, list[list[tuple[str, str]]]]:
            with Session(engine) as db:
                value = account_overview.overview(db, config, callback.from_user.id, bot.id)
                if code.startswith("account:history:"):
                    raw = code.removeprefix("account:history:")
                    if not raw.isascii() or not raw.isdecimal() or len(raw) > 6:
                        raise ValueError("Неверная страница истории")
                    page = int(raw)
                    text, more = account_overview.history(
                        db, config, callback.from_user.id, bot.id, page
                    )
                    rows = []
                    if page:
                        rows.append([("← Новее", f"account:history:{page - 1}")])
                    if more:
                        rows.append([("Старее →", f"account:history:{page + 1}")])
                    rows.append([("⬅️ Мой аккаунт", "account:open")])
                    return text, rows
                if code == "account:trials":
                    return (
                        "🎁 Бесплатные попытки\n\n"
                        + "\n".join(
                            f"{label}: {value['trials'][task]}"
                            for task, label in ai_tokens.TRIAL_LABELS.items()
                        )
                        + "\n\nЭто отдельные попытки, а не купленные токены."
                        + (
                            "\nУ вас безлимитное использование агентов."
                            if value["unlimited"]
                            else ""
                        ),
                        [[("⬅️ Мой аккаунт", "account:open")]],
                    )
                text = "👤 Мой аккаунт\n\n" + value["summary"]
                text += (
                    "\n\nAI-баланс — для агентов. Подписка — для проверки подписки "
                    "на канал при выдаче подарка."
                )
                if value["test"]:
                    text += "\n🧪 Тестовые балансы. Настоящий доступ ими не активируется."
                return text, account_rows(value)

        try:
            text, rows = await asyncio.to_thread(read)
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await state.update_data(paused=True)
        await state.set_state(None)
        await next_message(callback.message, text, reply_markup=buttons(rows))
        await callback.answer()

    @router.message(StateFilter("ai_token_purchase_amount"), ~F.text.startswith("/"))
    async def amount(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if (
            not message.from_user
            or message.chat.type != "private"
            or data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "token_purchase"
        ):
            return
        try:
            value = payment_amount((message.text or "").replace(" ", "").replace(",", "."), "RUB")
            with Session(engine) as db:
                current = account_overview.overview(db, config, message.from_user.id, bot.id)
                offer = agents.token_offer(db, config, value)
            if current["rubles"] < value:
                raise ValueError("Недостаточно рублей. Пополните баланс или введите меньшую сумму")
        except ValueError as error:
            await message.answer(
                str(error),
                reply_markup=buttons(
                    [[("➕ Пополнить баланс", "wallet:topup")], [("⬅️ Мой аккаунт", "account:open")]]
                ),
            )
            return
        nonce = uuid4().hex[:16]
        await state.set_state(None)
        await state.update_data(paused=True, token_money_offer={"nonce": nonce, "offer": offer})
        credit = offer.get("unit") == "credits"
        unit = "кредитов" if credit else "токенов"
        paid = Decimal(offer["price_rub"])
        await next_message(
            message,
            f"✨ Купить AI-{unit}\n\nСписать: {offer['price_rub']} ₽\n"
            f"Зачислить: {account_overview.number(offer['tokens'])} {unit}\n"
            f"Баланс после покупки: {current['rubles'] - paid:.2f} ₽\n"
            f"Цена единицы: {offer['unit_price_rub']} ₽\nПодписка не изменится.\n\n"
            f"Условия: {offer['terms']}\nПоддержка: {offer['support']}"
            + ("\n🧪 Тестовая покупка." if offer["test"] else ""),
            reply_markup=buttons(
                [
                    [
                        (
                            "✅ Купить кредиты" if credit else "✅ Купить токены",
                            "account:buytokens:" + nonce,
                        )
                    ],
                    [("Изменить сумму", "account:tokens")],
                    [("⬅️ Мой аккаунт", "account:open")],
                ]
            ),
        )

    return router
