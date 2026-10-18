import asyncio
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import access_for
from leedbox.application.settings import set_setting, setting
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import DeliveryEvent, Material, OutboxMessage, ProductEvent, User
from leedbox.telegram.library import keyboard


async def owner_panel(
    engine: Engine, settings: Settings, callback: CallbackQuery | Message, bot: Bot
) -> None:
    actor = callback.from_user
    message = callback.message if isinstance(callback, CallbackQuery) else callback
    if not actor or not settings.is_owner(actor.id) or not isinstance(message, Message):
        if isinstance(callback, CallbackQuery):
            await callback.answer("Доступ закрыт", show_alert=True)
        else:
            await callback.answer("Доступ закрыт")
        return

    def read() -> tuple[str, list[list[tuple[str, str]]]]:
        with Session(engine) as session:
            since = datetime.now(UTC) - timedelta(days=1)
            new = (
                session.scalar(
                    select(func.count()).select_from(User).where(User.created_at >= since)
                )
                or 0
            )
            deliveries = (
                session.scalar(
                    select(func.count())
                    .select_from(DeliveryEvent)
                    .join(Material, Material.id == DeliveryEvent.material_id)
                    .where(
                        Material.bot_id == bot.id,
                        DeliveryEvent.status == "sent",
                        DeliveryEvent.created_at >= since,
                    )
                )
                or 0
            )
            denied = (
                session.scalar(
                    select(func.count())
                    .select_from(ProductEvent)
                    .where(
                        ProductEvent.bot_id == bot.id,
                        ProductEvent.name == "delivery_denied",
                        ProductEvent.created_at >= since,
                    )
                )
                or 0
            )
            queued = (
                session.scalar(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(OutboxMessage.bot_id == bot.id, OutboxMessage.status == "pending")
                )
                or 0
            )
            states: dict[str, int] = {}
            for id in session.scalars(select(User.id).where(User.expert_started_at.is_not(None))):
                value = access_for(session, id).state
                states[value] = states.get(value, 0) + 1
            text = (
                f"Владелец · последние 24 часа\n\nНовых экспертов: {new}\nВыдач: {deliveries}\n"
                f"Отказов: {denied}\nВ очереди: {queued}\n\nДоступ: {states}"
            )
            rows = [
                [
                    (
                        f"{'✓' if setting(session, key) else '—'} {title}",
                        f"owner:toggle:{key}:{int(not setting(session, key))}",
                    )
                ]
                for title, key in (
                    ("Публикация", "materials_enabled"),
                    ("Услуги", "services_enabled"),
                    ("Рекомендации", "recommendations_enabled"),
                )
            ]
            rows += [
                [("Заявки на услуги", "ows:list:0")],
                [("Обновить сводку", "owner:summary")],
                [("В меню", "menu")],
            ]
            return text, rows

    text, rows = await asyncio.to_thread(read)
    if isinstance(callback, Message):
        await callback.answer(text, reply_markup=keyboard(rows))
        return
    try:
        await message.edit_text(text, reply_markup=keyboard(rows))
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.lower():
            raise
    await callback.answer()


def build_owner_router(engine: Engine, settings: Settings) -> Router:
    router = Router()

    @router.message(Command("owner"))
    async def owner_command(message: Message, state: FSMContext, bot: Bot) -> None:
        if not message.from_user or message.chat.type != "private":
            return
        if not settings.is_owner(message.from_user.id):
            await message.answer("Доступ закрыт")
            return
        await state.update_data(mode="expert", paused=True)
        await owner_panel(engine, settings, message, bot)

    @router.callback_query(F.data.startswith("owner:"))
    async def owner(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (
            not settings.is_owner(callback.from_user.id)
            or (await state.get_data()).get("mode") != "expert"
        ):
            await callback.answer("Доступ закрыт", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if (
            len(parts) == 4
            and parts[1] == "toggle"
            and parts[2] in {"materials_enabled", "services_enabled", "recommendations_enabled"}
            and parts[3] in {"0", "1"}
        ):

            def save() -> None:
                with Session(engine) as session, session.begin():
                    set_setting(session, settings, callback.from_user.id, parts[2], parts[3] == "1")

            await asyncio.to_thread(save)
        elif callback.data != "owner:summary":
            await callback.answer("Экран устарел", show_alert=True)
            return
        await owner_panel(engine, settings, callback, bot)

    return router
