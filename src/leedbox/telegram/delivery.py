import asyncio
from dataclasses import dataclass
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.entitlements import can_check_subscriptions
from leedbox.application.settings import product_event
from leedbox.infrastructure.models import (
    Channel,
    DeliveryEvent,
    Material,
    MaterialVersion,
    Project,
    Recipient,
)
from leedbox.telegram.channels import Subscription, subscription


@dataclass(frozen=True)
class DeliveryMaterial:
    id: int
    version_id: int
    slug: str
    mode: str
    content_type: str
    content: str
    title: str
    channel_tg_id: int | None
    join_url: str | None
    contact: str | None
    bypassed: bool = False


def find_material(engine: Engine, bot_id: int, payload: str) -> DeliveryMaterial | None:
    with Session(engine) as session:
        # Match full permanent slug first; attributed links use a separate short code suffix.
        material = session.scalar(
            select(Material).where(
                Material.slug == payload, Material.bot_id == bot_id, Material.archived.is_(False)
            )
        )
        if material is None and payload.count("_") == 2:
            material = session.scalar(
                select(Material).where(
                    Material.slug == payload.rsplit("_", 1)[0],
                    Material.bot_id == bot_id,
                    Material.archived.is_(False),
                )
            )
        if material is None:
            return None
        version = session.scalar(
            select(MaterialVersion).where(
                MaterialVersion.material_id == material.id,
                MaterialVersion.number == material.current_version,
            )
        )
        project = session.get(Project, material.project_id)
        channel = session.get(Channel, material.channel_id) if material.channel_id else None
        if version is None or project is None:
            return None
        mode = material.delivery_mode
        if mode == "subscribers" and not can_check_subscriptions(session, material.owner_user_id):
            # The expert pays for subscription enforcement, never the recipient for a gift.
            # Unpaid/expired access must not hide an already published lead magnet.
            mode = "everyone"
        return DeliveryMaterial(
            material.id,
            version.id,
            material.slug,
            mode,
            version.content_type,
            version.content,
            version.title,
            channel.telegram_id if channel else None,
            channel.join_url if channel else None,
            project.public_contact,
            material.delivery_mode == "subscribers" and mode == "everyone",
        )


def action_key(material: DeliveryMaterial, user_id: int, token: str | None) -> str:
    if token is not None:
        if len(token) != 12 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError("Invalid repeat action")
        return f"repeat:{material.id}:{user_id}:{token}"
    return f"initial:{material.id}:{material.version_id}:{user_id}"


def claim(
    engine: Engine, material: DeliveryMaterial, user_id: int, token: str | None = None
) -> bool:
    with Session(engine) as session, session.begin():
        # Initial delivery is unique per material/version/recipient.
        key = action_key(material, user_id, token)
        # Recheck archive and version immediately before claiming the action.
        current = session.scalar(
            select(Material).where(Material.id == material.id).with_for_update()
        )
        if current is None or current.archived:
            return False
        current_version_id = session.scalar(
            select(MaterialVersion.id).where(
                MaterialVersion.material_id == current.id,
                MaterialVersion.number == current.current_version,
            )
        )
        if current_version_id != material.version_id:
            return False
        result = session.scalar(
            insert(DeliveryEvent)
            .values(
                material_id=material.id,
                version_id=material.version_id,
                telegram_user_id=user_id,
                action_key=key,
                status="pending",
                subscription_bypassed=material.bypassed,
            )
            .on_conflict_do_nothing(index_elements=[DeliveryEvent.action_key])
            .returning(DeliveryEvent.id)
        )
        if result is not None:
            return True
        existing = session.scalar(select(DeliveryEvent).where(DeliveryEvent.action_key == key))
        # Only a confirmed API rejection is safe to retry. Network failures may have delivered.
        if existing is not None and existing.status == "failed":
            existing.status = "pending"
            existing.version_id = material.version_id
            existing.subscription_bypassed = material.bypassed
            return True
        return False


def finish(
    engine: Engine,
    material: DeliveryMaterial,
    user_id: int,
    status: str,
    message_id: int | None = None,
    token: str | None = None,
) -> None:
    with Session(engine) as session, session.begin():
        event = session.scalar(
            select(DeliveryEvent).where(
                DeliveryEvent.action_key == action_key(material, user_id, token)
            )
        )
        if event:
            previous = event.status
            event.status, event.message_id = status, message_id
            if status == "sent" and previous != "sent":
                session.execute(
                    insert(Recipient)
                    .values(material_id=material.id, telegram_user_id=user_id)
                    .on_conflict_do_nothing()
                )
                bot_id = (
                    session.scalar(select(Material.bot_id).where(Material.id == material.id)) or 0
                )
                product_event(session, bot_id, user_id, "delivery_succeeded", material.id)


def denied(engine: Engine, bot_id: int, user_id: int, payload: str, reason: str) -> None:
    with Session(engine) as session, session.begin():
        slug = payload.rsplit("_", 1)[0] if payload.count("_") == 2 else payload
        material_id = session.scalar(
            select(Material.id).where(Material.bot_id == bot_id, Material.slug == slug)
        )
        product_event(session, bot_id, user_id, "delivery_denied", material_id, reason=reason)


async def deliver(
    bot: Bot, engine: Engine, user_id: int, payload: str, token: str | None = None
) -> str | None:
    """Send the gift itself; return only a recoverable user-facing error on failure."""
    material = await asyncio.to_thread(find_material, engine, bot.id, payload)
    if material is None:
        await asyncio.to_thread(denied, engine, bot.id, user_id, payload, "unavailable")
        return "Материал сейчас недоступен"
    if material.mode == "subscribers":
        if material.channel_tg_id is None:
            await asyncio.to_thread(
                denied, engine, bot.id, user_id, payload, "channel_configuration"
            )
            return "Не удалось проверить подписку. Попробуйте позже"
        result = await subscription(bot, material.channel_tg_id, user_id)
        if result != Subscription.SUBSCRIBED:
            await asyncio.to_thread(denied, engine, bot.id, user_id, payload, str(result))
        if result == Subscription.NOT_SUBSCRIBED:
            return "Подписка пока не найдена. Подпишитесь и нажмите кнопку ещё раз"
        if result == Subscription.BOT_NO_RIGHTS:
            return "У бота нет прав для проверки подписки. Попробуйте позже"
        if result == Subscription.CHECK_FAILED:
            return "Не удалось проверить подписку. Попробуйте позже"
    try:
        me = await bot.me()
    except TelegramAPIError:
        return "Telegram временно недоступен. Попробуйте позже"
    if not await asyncio.to_thread(claim, engine, material, user_id, token):
        return "Запрос уже обработан или выдача ещё выполняется"
    keyboard = None
    if material.contact:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💬 Связаться с автором", url=f"https://t.me/{material.contact}"
                    )
                ]
            ]
        )
    if material.content_type != "url":
        repeat = [
            InlineKeyboardButton(
                text="🎁 Получить ещё раз",
                callback_data=f"again:{material.slug}:{uuid4().hex[:12]}",
            )
        ]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=(keyboard.inline_keyboard if keyboard else []) + [repeat]
        )
    signature = f"Выдано через @{me.username}"
    try:
        # Branding and useful actions belong to the gift, not a second success screen.
        heading = (
            "Отлично! ✅ Подписка подтверждена!" if material.mode == "subscribers" else "Готово! ✅"
        )
        caption = f"{heading}\n\n🎁 Ваш подарок: {material.title}"
        if material.content_type == "document":
            sent = await bot.send_document(
                user_id,
                material.content,
                caption=caption + "\n\n" + signature,
                reply_markup=keyboard,
            )
        elif material.content_type == "photo":
            sent = await bot.send_photo(
                user_id,
                material.content,
                caption=caption + "\n\n" + signature,
                reply_markup=keyboard,
            )
        elif material.content_type == "video":
            sent = await bot.send_video(
                user_id,
                material.content,
                caption=caption + "\n\n" + signature,
                reply_markup=keyboard,
            )
        elif material.content_type == "url":
            sent = await bot.send_message(
                user_id,
                caption + "\n\nНажмите кнопку ниже, чтобы открыть материал:\n\n" + signature,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🎁 Открыть подарок", url=material.content)]
                    ]
                    + (keyboard.inline_keyboard if keyboard else [])
                ),
            )
        else:
            sent = await bot.send_message(
                user_id, material.content + "\n\n" + signature, reply_markup=keyboard
            )
    except TelegramNetworkError:
        await asyncio.to_thread(finish, engine, material, user_id, "uncertain", None, token)
        return "Не удалось подтвердить отправку. Проверьте сообщения и попробуйте позже"
    except TelegramAPIError:
        await asyncio.to_thread(finish, engine, material, user_id, "failed", None, token)
        await asyncio.to_thread(denied, engine, bot.id, user_id, payload, "send_failed")
        return "Не удалось отправить материал. Попробуйте позже"
    await asyncio.to_thread(finish, engine, material, user_id, "sent", sent.message_id, token)
    return None
