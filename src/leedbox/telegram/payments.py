"""Expert purchase extension, using Telegram's existing native controls.

THESIS: A price and period first; consent before invoice, never card-entry UI.
OWN-WORLD: Plain Telegram messages and one-column inline buttons, no new theme.
STORY: Read frozen terms, request a personal invoice, pay, inspect the saved status.
FIRST VIEWPORT: Price, period and one-off payment explanation above consent/back.
FORM: Local extension of profile/access and service requests; no concept roll.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish
review, the verdict, DESIGN.md, and every shipping raster carrying its provenance.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.client.telegram import TEST
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    Message,
    PreCheckoutQuery,
    TelegramObject,
    Update,
)
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from leedbox.application.entitlements import policy
from leedbox.application.payment_inbox import capture, process_one
from leedbox.application.purchasing import (
    accept_service_checkout,
    access_offer,
    buy_access,
    buyer_order,
)
from leedbox.application.settings import setting
from leedbox.application.stars import invoice_fields, validate_stars_order
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.payments import InvalidPayment, invoice_id
from leedbox.domain.services import owner_contact_url
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import PaymentOrder, User
from leedbox.telegram.material_wizard import buttons
from leedbox.telegram.services import edit

STATUS = {
    "refunded": "Полный возврат подтверждён",
    "pending": "Ожидает оплаты",
    "paid": "Оплата подтверждена",
    "cancelled": "Отменён",
    "review": "Требует проверки владельцем",
}


def test_api(bot: Bot) -> bool:
    return bot.session.api == TEST


def order_text(order: PaymentOrder) -> str:
    label = "Stars" if order.currency == "XTR" else order.currency
    return (
        f"Заказ №{order.id}\n\n{STATUS[order.status]}\n"
        + ("Пополнение баланса\n" if order.kind == "topup" else "")
        + f"Сумма: {order.amount:g} {label}\n"
        + (f"Период: {order.period_days} дней\n" if order.kind == "access" else "")
        + ("Тестовый платёж — платный доступ не начисляется.\n" if order.test_mode else "")
    )


class PurchaseNavigationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and (event.successful_payment or event.refunded_payment):
            return await handler(event, data)
        state = data.get("state")
        if state is not None:
            code = event.data or "" if isinstance(event, CallbackQuery) else ""
            updates: dict[str, Any] = {}
            if not code.startswith("purchase:buy:"):
                updates["purchase"] = None
            if not code.startswith("purchase:service:"):
                updates["service_purchase"] = None
            if updates:
                await state.update_data(**updates)
        return await handler(event, data)


def install_payments(router: Router, engine: Engine, config: Settings) -> None:
    router.message.middleware(PurchaseNavigationMiddleware())
    router.callback_query.middleware(PurchaseNavigationMiddleware())

    @router.callback_query(F.data == "access:renew")
    async def renew(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return

        def read() -> tuple[dict[str, Any] | None, tuple[str, str | None] | None]:
            with Session(engine) as db:
                try:
                    return access_offer(db, config, bot.id), None
                except InvalidPayment as error:
                    return None, (str(error), owner_contact_url(str(setting(db, "owner_contact"))))

        offer, unavailable = await asyncio.to_thread(read)
        if not offer or test_api(bot) != config.telegram_test_api:
            await state.update_data(purchase=None)
            reason, contact = unavailable or ("Покупка пока недоступна", None)
            markup = buttons([[("Назад", "profile:access")], [("В меню", "menu")]])
            if contact:
                markup.inline_keyboard.insert(
                    0, [InlineKeyboardButton(text="Написать владельцу", url=contact)]
                )
            await callback.message.edit_text(reason, reply_markup=markup, parse_mode=None)
            await callback.answer()
            return
        nonce = uuid4().hex[:16]
        await state.update_data(
            paused=True,
            purchase={
                "nonce": nonce,
                "offer": offer,
                "message_id": callback.message.message_id,
                "invoice_state": "new",
                "order_id": None,
            },
        )
        await edit(
            callback.message,
            f"Продление доступа\n\n{offer['price']} Stars за {offer['period_days']} дней.\n"
            "Разовый платёж, без автоматических списаний. "
            "Текущий непрерывный срок доступа сохраняется.\n\n"
            + ("ТЕСТОВАЯ СРЕДА: платный доступ не начисляется.\n\n" if offer["test_mode"] else "")
            + f"Условия покупки и возврата:\n{offer['terms']}\n\n"
            "Нажимая кнопку ниже, вы принимаете эти условия. "
            "Затем Telegram покажет счёт; сейчас списания нет.",
            [
                [("Согласен, получить счёт", f"purchase:buy:{nonce}")],
                [("Назад", "profile:access")],
                [("В меню", "menu")],
            ],
            offer["support"],
            "Платёжная поддержка",
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("purchase:buy:"))
    async def buy(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        purchase = data.get("purchase")
        if (
            data.get("mode") != "expert"
            or not isinstance(callback.message, Message)
            or not purchase
            or callback.data != f"purchase:buy:{purchase['nonce']}"
            or callback.message.message_id != purchase["message_id"]
            or callback.message.chat.id != callback.from_user.id
            or test_api(bot) != config.telegram_test_api
            or not config.stars_enabled
        ):
            await callback.answer("Экран устарел. Откройте покупку заново", show_alert=True)
            return
        if purchase["invoice_state"] != "new":
            await callback.answer(
                "Счёт уже запрошен. Проверьте чат и раздел «Мои заказы»", show_alert=True
            )
            return
        confirmed_purchase = purchase

        def create() -> tuple[int, dict[str, Any]]:
            with Session(engine) as db, db.begin():
                user_id = db.scalar(
                    select(User.id).where(User.telegram_id == callback.from_user.id)
                )
                if user_id is None:
                    raise ObjectUnavailable
                order = buy_access(
                    db,
                    config,
                    user_id,
                    bot.id,
                    confirmed_purchase["offer"],
                    f"purchase:{callback.from_user.id}:{confirmed_purchase['nonce']}",
                    True,
                )
                return order.id, invoice_fields(order)

        try:
            number, fields = await asyncio.to_thread(create)
        except (InvalidPayment, ObjectUnavailable) as error:
            await callback.answer(str(error) or "Заказ недоступен", show_alert=True)
            return
        purchase = {**purchase, "invoice_state": "sending", "order_id": number}
        await state.update_data(purchase=purchase)
        await callback.answer()
        try:
            await bot.send_invoice(chat_id=callback.from_user.id, **fields)
        except TelegramAPIError:
            # Unknown send outcomes are not blindly retried. The order remains pending.
            await state.update_data(purchase={**purchase, "invoice_state": "uncertain"})
            await callback.message.answer(
                "Не удалось подтвердить отправку счёта. Проверьте чат. "
                "Статус заказа доступен в «Моих заказах».",
                reply_markup=buttons([[("Мои заказы", "purchase:orders:0")]]),
            )
            return
        await state.update_data(purchase={**purchase, "invoice_state": "sent"})
        await edit(
            callback.message,
            f"Счёт для заказа №{number} отправлен в этот чат.\n"
            "Доступ изменится только после подтверждённой оплаты.",
            [[("Проверить статус", f"purchase:order:{number}")], [("В меню", "menu")]],
        )

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
        def check() -> bool:
            if not config.stars_enabled or test_api(bot) != config.telegram_test_api:
                return False
            with Session(engine) as db:
                if policy(db).mode != "paid" or setting(db, "stars_access_price") <= 0:
                    return False
                order = validate_stars_order(
                    db,
                    bot.id,
                    query.from_user.id,
                    query.invoice_payload,
                    query.currency,
                    query.total_amount,
                    test_api(bot),
                )
                return (
                    order.status == "pending"
                    and order.snapshot.get("accepted") is True
                    and order.created_at >= datetime.now(UTC) - timedelta(hours=1)
                )

        try:
            ok = await asyncio.wait_for(asyncio.to_thread(check), timeout=5)
        except (InvalidPayment, SQLAlchemyError, TimeoutError):
            ok = False
        await query.answer(
            ok=ok,
            error_message=None
            if ok
            else "Счёт недоступен, истёк или уже оплачен. Откройте покупку заново.",
        )

    @router.message(F.successful_payment | F.refunded_payment)
    async def successful(message: Message, bot: Bot, event_update: Update) -> None:
        if (
            not message.from_user
            or message.chat.type != "private"
            or not (message.successful_payment or message.refunded_payment)
        ):
            return
        payer_id = message.from_user.id

        def confirm() -> str:
            inbox_id = capture(engine, bot.id, test_api(bot), event_update)
            assert inbox_id is not None
            status = process_one(engine, inbox_id)
            if status == "review":
                return (
                    "Платёжное событие сохранено для проверки владельцем. Обратитесь в /paysupport."
                )
            payment = message.successful_payment
            assert payment is not None
            with Session(engine) as db:
                order = buyer_order(
                    db,
                    payer_id,
                    bot.id,
                    invoice_id(payment.invoice_payload.removeprefix("access:")),
                )
                return order_text(order)

        try:
            text = await asyncio.to_thread(confirm)
        except (InvalidPayment, SQLAlchemyError):
            logging.getLogger(__name__).error("payment_confirmation_requires_review")
            await message.answer(
                "Не удалось завершить обработку платежа. Не оплачивайте повторно; "
                "обратитесь в /paysupport с номером заказа из счёта."
            )
            return
        await message.answer(text, parse_mode=None)

    @router.message(Command("paysupport", "terms"))
    async def support(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or message.chat.type != "private":
            return
        expert = (await state.get_data()).get("mode") == "expert"
        payer_id = message.from_user.id

        def read() -> tuple[str, str | None] | None:
            with Session(engine) as db:
                if (
                    not expert
                    and db.scalar(
                        select(PaymentOrder.id)
                        .join(User)
                        .where(
                            PaymentOrder.bot_id == bot.id,
                            User.telegram_id == payer_id,
                        )
                        .limit(1)
                    )
                    is None
                ):
                    return None
                contact = owner_contact_url(str(setting(db, "owner_contact")))
                terms = str(setting(db, "payment_terms")).strip()
                return (terms or "Условия покупки пока не опубликованы.", contact)

        result = await asyncio.to_thread(read)
        if result is None:
            return
        text, contact = result
        markup = buttons(
            [[("Мои заказы", "purchase:orders:0")], [("В меню", "menu")]] if expert else []
        )
        if contact:
            markup.inline_keyboard.insert(
                0, [InlineKeyboardButton(text="Платёжная поддержка", url=contact)]
            )
        await message.answer(
            text + "\n\nВопросы об оплате и возврате решает владелец сервиса, "
            "не поддержка Telegram." + (" Контакт пока не настроен." if not contact else ""),
            reply_markup=markup,
            parse_mode=None,
        )

    @router.callback_query(F.data.startswith("purchase:orders:"))
    async def orders(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        try:
            page = int((callback.data or "").split(":")[-1])
            if not 0 <= page <= 10000:
                raise ValueError
        except ValueError:
            await callback.answer("Экран устарел", show_alert=True)
            return

        def read() -> list[tuple[int, str]]:
            with Session(engine) as db:
                records = db.scalars(
                    select(PaymentOrder)
                    .join(User)
                    .where(PaymentOrder.bot_id == bot.id, User.telegram_id == callback.from_user.id)
                    .order_by(PaymentOrder.id.desc())
                    .offset(page * 5)
                    .limit(6)
                ).all()
                return [(r.id, STATUS[r.status]) for r in records]

        records = await asyncio.to_thread(read)
        rows = [
            [(f"№{number} · {label}", f"purchase:order:{number}")] for number, label in records[:5]
        ]
        if page:
            rows.append([("Предыдущие", f"purchase:orders:{page - 1}")])
        if len(records) > 5:
            rows.append([("Следующие", f"purchase:orders:{page + 1}")])
        await state.update_data(paused=True)
        await edit(
            callback.message,
            "Мои заказы" + ("\n\nЗаказов пока нет." if not records else ""),
            rows + [[("Назад", "section:profile")], [("В меню", "menu")]],
        )
        await callback.answer()

    @router.callback_query(
        F.data.startswith("purchase:order:") | F.data.startswith("purchase:service:")
    )
    async def order_card(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        try:
            number = invoice_id((callback.data or "").split(":")[-1])
            screen_id = callback.message.message_id

            def read() -> tuple[str, dict[str, Any], str, str | None]:
                with Session(engine) as db, db.begin():
                    row = buyer_order(db, callback.from_user.id, bot.id, number)
                    url = None
                    if (callback.data or "").startswith("purchase:service:"):
                        pending = data.get("service_purchase") or {}
                        if pending.get("id") != number or pending.get("message_id") != screen_id:
                            raise InvalidPayment("Экран устарел. Откройте заказ заново")
                        url = accept_service_checkout(
                            db,
                            config,
                            callback.from_user.id,
                            bot.id,
                            number,
                            pending["terms_version"],
                            True,
                        )
                    return order_text(row), dict(row.snapshot), row.status, url

            data = await state.get_data()
            text, snapshot, status, url = await asyncio.to_thread(read)
        except (InvalidPayment, ObjectUnavailable) as error:
            await callback.answer(str(error) or "Заказ недоступен", show_alert=True)
            return
        rows = [
            [("Обновить статус", f"purchase:order:{number}")],
            [("Мои заказы", "purchase:orders:0")],
            [("В меню", "menu")],
        ]
        if status == "pending" and snapshot.get("category") == "physical_service" and not url:
            text += "\nУсловия покупки и возврата:\n" + snapshot["terms"]
            text += (
                "\n\nНажимая кнопку ниже, вы принимаете условия. "
                "Оплата откроется на стороне Robokassa."
            )
            rows.insert(0, [("Согласен, перейти к оплате", f"purchase:service:{number}")])
            await state.update_data(
                service_purchase={
                    "id": number,
                    "terms_version": snapshot["terms_version"],
                    "message_id": callback.message.message_id,
                }
            )
        if status == "pending" and snapshot.get("offer", {}).get("method"):
            rows.insert(0, [("💳 Продолжить пополнение", f"wallet:checkout:{number}")])
        markup = buttons(rows)
        support_url = snapshot.get("support") or snapshot.get("offer", {}).get("support")
        if support_url:
            markup.inline_keyboard.append(
                [InlineKeyboardButton(text="Платёжная поддержка", url=support_url)]
            )
        if url:
            markup.inline_keyboard.insert(
                0, [InlineKeyboardButton(text="Оплатить на Robokassa", url=url)]
            )
            text += (
                "\nОплатите на странице Robokassa, затем обновите статус. "
                "Переход по ссылке не подтверждает оплату."
            )
        await callback.message.edit_text(text, reply_markup=markup, parse_mode=None)
        await callback.answer()
