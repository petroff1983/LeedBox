"""Explicitly mounted local acceptance UI; never enabled by the normal entry point."""

import asyncio
import secrets
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy import Engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application import account_overview, agents, wallet
from leedbox.application.payments import create_order
from leedbox.application.settings import setting
from leedbox.domain.payments import InvalidPayment
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import User, WalletEntry
from leedbox.telegram.material_wizard import buttons
from leedbox.telegram.safety import safe_error

AMOUNT = Decimal("200.00")
TEST_SUPPORT_USERNAME = "neuropro888fi_bot"
NOTICE = (
    "🧪 Тест оплаты — настоящие деньги не списываются.\n"
    "Отдельный тестовый кошелёк; права владельца не выдаются.\n"
    "Не вводите настоящие банковские реквизиты."
)


def build_payment_sandbox_router(engine: Engine, config: Settings, actor_id: int) -> Router:
    url = make_url(config.database_url.get_secret_value())
    if not (
        config.environment == "development"
        and config.robokassa_test
        and config.robokassa_enabled
        and not config.stars_enabled
        and url.host in {"localhost", "127.0.0.1"}
        and url.port == 5434
        and (url.database or "").startswith("leedbox_robo_sandbox_")
        and (url.database or "").endswith("_test")
        and actor_id > 0
        and not config.is_owner(actor_id)
    ):
        raise ValueError("An isolated local sandbox and a non-owner tester are required")

    router = Router(name="local_payment_acceptance")
    router.errors.register(safe_error)
    router.message.filter(F.from_user.id == actor_id, F.chat.type == "private")
    router.callback_query.filter(F.from_user.id == actor_id, F.message.chat.type == "private")

    def owner(db: Session) -> int:
        uid = db.scalar(
            select(User.id).where(User.telegram_id == actor_id, User.expert_started_at.is_not(None))
        )
        if uid is None:
            raise InvalidPayment("Тестовый эксперт не подготовлен")
        return uid

    def read_balance(bot_id: int) -> dict[str, Any]:
        with Session(engine) as db:
            value = account_overview.overview(db, config, actor_id, bot_id)
            value["test_subscription_purchased"] = (
                db.scalar(
                    select(WalletEntry.id)
                    .where(
                        WalletEntry.owner_user_id == value["owner"],
                        WalletEntry.bot_id == bot_id,
                        WalletEntry.test_mode.is_(True),
                        WalletEntry.kind == "purchase",
                        WalletEntry.snapshot["purpose"].astext.is_distinct_from("ai_tokens"),
                    )
                    .limit(1)
                )
                is not None
            )
            return value

    async def render(message: Message, bot: Bot) -> None:
        value = await asyncio.to_thread(read_balance, bot.id)
        await message.answer(
            NOTICE
            + "\n\n"
            + value["summary"]
            + (
                "\n✅ Тестовая покупка подписки выполнена; настоящий доступ не изменён."
                if value["test_subscription_purchased"]
                else ""
            )
            + "\n"
            "Цена токена: 0,0003 ₽ · 300 ₽ за 1 млн.\n"
            "Тестовые AI-кредиты расходуются обычными агентами только внутри "
            "этого изолированного стенда.\n"
            "Тестовая цена доступа: 100 ₽ / 30 дней.\n"
            "Оплата здесь не включает настоящий платный доступ.\n\n"
            "Пополнение добавляет средства на баланс. Оплата доступа списывает их с баланса.\n"
            "После Robokassa нажмите «Обновить баланс».",
            reply_markup=buttons(
                [
                    [("➕ Пополнить баланс · 200 ₽", "paytest:topup")],
                    [("✨ Купить токены на 200 ₽", "paytest:tokens")],
                    [("🔑 Оплатить с баланса · 100 ₽", "paytest:buy")],
                    [("🔄 Обновить баланс", "paytest:open")],
                    [("📒 Покупки и расход", "paytest:history")],
                    [("⬅️ В главное меню", "menu")],
                ]
            ),
        )

    @router.message(Command("testpay"))
    async def command(message: Message, bot: Bot) -> None:
        await render(message, bot)

    @router.callback_query(
        F.data.startswith("paytest:")
        | F.data.in_(
            {"profile:access", "access:renew", "account:open", "ait:packages", "account:tokens"}
        )
    )
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer("Откройте /testpay", show_alert=True)
            return
        code = callback.data or ""
        try:
            if code in {"paytest:open", "profile:access", "access:renew", "account:open"}:
                await render(callback.message, bot)
            elif code == "paytest:history":
                with Session(engine) as db:
                    text, _ = account_overview.history(db, config, actor_id, bot.id)
                await callback.message.answer(
                    text, reply_markup=buttons([[("⬅️ Тестовый аккаунт", "paytest:open")]])
                )
            elif code in {
                "paytest:topup",
                "paytest:buy",
                "paytest:tokens",
                "ait:packages",
                "account:tokens",
            }:
                nonce = secrets.token_hex(12)
                offer = None
                if code == "paytest:buy":

                    def read_offer() -> dict[str, Any]:
                        with Session(engine) as db:
                            owner(db)
                            return wallet.access_offer(db, config)

                    offer = await asyncio.to_thread(read_offer)
                elif code in {"paytest:tokens", "ait:packages", "account:tokens"}:
                    with Session(engine) as db:
                        offer = agents.token_offer(db, config, AMOUNT)
                await state.update_data(
                    payment_sandbox={
                        "nonce": nonce,
                        "kind": "tokens"
                        if offer and offer.get("mode") == "amount"
                        else code.split(":")[1],
                        "offer": offer,
                    }
                )
                await callback.message.answer(
                    NOTICE
                    + (
                        "\n\nПополнить тестовый баланс на 200 ₽?"
                        if offer is None
                        else f"\n\nКупить {offer['tokens']} тестовых токенов за 200 ₽?\n"
                        "Подписка не изменится."
                        if offer.get("mode") == "amount"
                        else "\n\nСписать 100 тестовых ₽ за проверку покупки доступа?\n"
                        "Настоящий доступ и его сроки не изменятся."
                    ),
                    reply_markup=buttons(
                        [
                            [("✅ Подтвердить тест", "paytest:confirm:" + nonce)],
                            [("⬅️ Назад", "paytest:open")],
                        ]
                    ),
                )
            elif code.startswith("paytest:confirm:"):
                saved = (await state.get_data()).get("payment_sandbox")
                if not saved or code != "paytest:confirm:" + saved["nonce"]:
                    raise InvalidPayment("Подтверждение устарело. Откройте /testpay")

                def pay() -> str | None:
                    with Session(engine) as db, db.begin():
                        uid = owner(db)
                        key = f"telegram-test:{actor_id}:{saved['nonce']}"
                        if saved["kind"] == "buy":
                            wallet.buy_access(db, config, uid, bot.id, saved["offer"], key, True)
                            return None
                        if saved["kind"] == "tokens":
                            agents.buy_tokens(db, config, uid, bot.id, key, saved["offer"])
                            return None
                        order = create_order(
                            db,
                            uid,
                            bot.id,
                            kind="topup",
                            provider="robokassa",
                            account=config.robokassa_merchant,
                            test_mode=True,
                            amount=AMOUNT,
                            period_days=0,
                            action_key=key,
                            snapshot={
                                "accepted": True,
                                "terms_version": "telegram-sandbox-1",
                                "receipt": setting(db, "wallet_receipt_template"),
                                "offer": {"method": ""},
                            },
                        )
                        link = wallet.topup_checkout(db, config, uid, bot.id, order.id)
                        parsed = urlsplit(link)
                        if (
                            parsed.scheme != "https"
                            or parsed.netloc != "auth.robokassa.ru"
                            or parse_qs(parsed.query).get("IsTest") != ["1"]
                        ):
                            raise InvalidPayment("Отказ от нетестовой оплаты")
                        return link

                link = await asyncio.to_thread(pay)
                markup = buttons(
                    [[("🔄 Проверить баланс", "paytest:open")], [("⬅️ В меню", "menu")]]
                )
                if link:
                    markup.inline_keyboard.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                text="🧪 Открыть тестовую Robokassa",
                                url=link,
                            )
                        ],
                    )
                await callback.message.answer(
                    NOTICE
                    + (
                        "\n\nСчёт создан. В Robokassa выберите успешное завершение теста, "
                        "вернитесь сюда и проверьте баланс."
                        if link
                        else f"\n\n✅ Куплено {saved['offer']['tokens']} тестовых AI-токенов. "
                        "Списано 200 тестовых ₽. Подписка не изменилась."
                        if saved["kind"] == "tokens"
                        else "\n\n✅ Тестовая покупка выполнена. Списано 100 тестовых ₽.\n"
                        "Настоящий доступ не изменён."
                    ),
                    reply_markup=markup,
                )
            else:
                raise InvalidPayment("Откройте /testpay заново")
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer()

    return router
