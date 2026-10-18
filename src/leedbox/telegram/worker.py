"""Transactional PostgreSQL outbox. Unknown network outcomes are never blindly resent."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.client.telegram import TEST
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from leedbox.application.broadcasts import can_deliver
from leedbox.application.payment_inbox import recover
from leedbox.application.youtube import purge_cache
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    Consent,
    ExpertNotificationPreference,
    Material,
    OutboxMessage,
    OwnerBroadcast,
    ServiceRequest,
    User,
)


@dataclass(frozen=True)
class PendingMessage:
    id: int
    user_id: int
    material_id: int | None
    purpose: str
    text: str
    service_request_id: int | None = None
    request_revision: int | None = None
    request_status: str | None = None
    broadcast_id: int | None = None


def next_message(engine: Engine, bot_id: int, now: datetime | None = None) -> PendingMessage | None:
    now = now or datetime.now(UTC)
    with Session(engine) as session, session.begin():
        # A previous process could have sent and crashed before recording success.
        session.execute(
            update(OutboxMessage)
            .where(
                OutboxMessage.bot_id == bot_id,
                OutboxMessage.status == "processing",
                OutboxMessage.locked_at < now - timedelta(minutes=5),
            )
            .values(status="uncertain")
        )
        row = session.scalar(
            select(OutboxMessage)
            .where(
                OutboxMessage.bot_id == bot_id,
                OutboxMessage.status == "pending",
                OutboxMessage.ready_at <= now,
            )
            .order_by(OutboxMessage.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        if row.purpose == "owner_broadcast" and not can_deliver(session, row):
            row.status = "cancelled"
            return None
        request = None
        if row.purpose == "service_request":
            request = (
                session.get(ServiceRequest, row.service_request_id)
                if row.service_request_id
                else None
            )
            if not request or request.bot_id != bot_id or request.consent_revoked_at is not None:
                row.status = "cancelled"
                return None
        if row.purpose == "author_updates":
            consent = session.scalar(
                select(Consent.id).where(
                    Consent.material_id == row.material_id,
                    Consent.telegram_user_id == row.telegram_user_id,
                    Consent.purpose == "author_updates",
                    Consent.revoked_at.is_(None),
                )
            )
            material = session.get(Material, row.material_id) if row.material_id else None
            if not consent or not material or material.archived or material.bot_id != bot_id:
                row.status = "cancelled"
                return None
        row.status, row.locked_at, row.attempts = "processing", now, row.attempts + 1
        return PendingMessage(
            row.id,
            row.telegram_user_id,
            row.material_id,
            row.purpose,
            row.text,
            row.service_request_id,
            request.revision if request else None,
            request.status if request else None,
            row.broadcast_id,
        )


def complete(
    engine: Engine, id: int, status: str, retry_after: int = 0, revoke: bool = False
) -> None:
    with Session(engine) as session, session.begin():
        row = session.get(OutboxMessage, id)
        if row:
            row.status = status
            if retry_after:
                row.ready_at = datetime.now(UTC) + timedelta(seconds=retry_after)
            if revoke and row.purpose == "author_updates":
                consent = session.scalar(
                    select(Consent).where(
                        Consent.material_id == row.material_id,
                        Consent.telegram_user_id == row.telegram_user_id,
                        Consent.purpose == "author_updates",
                    )
                )
                if consent:
                    consent.revoked_at = datetime.now(UTC)
            if revoke and row.purpose == "owner_broadcast":
                user = session.scalar(select(User).where(User.telegram_id == row.telegram_user_id))
                preference = (
                    session.get(ExpertNotificationPreference, (user.id, row.bot_id))
                    if user
                    else None
                )
                if preference:
                    preference.notices = preference.advertising = False
                    preference.advertising_revoked_at = datetime.now(UTC)


async def send_one(bot: Bot, engine: Engine, settings: Settings | None = None) -> bool:
    row = await asyncio.to_thread(next_message, engine, bot.id)
    if row is None:
        return False
    markup = None
    if row.purpose == "provider_alert" and (settings is None or not settings.is_owner(row.user_id)):
        await asyncio.to_thread(complete, engine, row.id, "cancelled")
        return True
    if row.purpose == "owner_broadcast":

        def broadcast_buttons() -> list[list[InlineKeyboardButton]] | None:
            with Session(engine) as session:
                queued = session.get(OutboxMessage, row.id)
                item = session.get(OwnerBroadcast, row.broadcast_id) if row.broadcast_id else None
                if not queued or not item or not can_deliver(session, queued):
                    return None
                result = []
                if item.payload.get("button_url"):
                    result.append(
                        [
                            InlineKeyboardButton(
                                text=item.payload["button_label"], url=item.payload["button_url"]
                            )
                        ]
                    )
                result.append(
                    [
                        InlineKeyboardButton(
                            text="Отключить рекламу"
                            if item.kind == "advertising"
                            else "Отключить новости",
                            callback_data=f"notify:set:{item.kind}:0",
                        )
                    ]
                )
                return result

        campaign_buttons = await asyncio.to_thread(broadcast_buttons)
        if campaign_buttons is None:
            await asyncio.to_thread(complete, engine, row.id, "cancelled")
            return True
        markup = InlineKeyboardMarkup(inline_keyboard=campaign_buttons)
    if row.purpose == "service_request":
        if settings is None or not settings.is_owner(row.user_id):
            await asyncio.to_thread(complete, engine, row.id, "cancelled")
            return True
        buttons = [
            [
                InlineKeyboardButton(
                    text="Открыть заявку", callback_data=f"ows:open:{row.service_request_id}"
                )
            ]
        ]
        if row.request_status == "new":
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Взять в работу",
                        callback_data=f"ows:change:{row.service_request_id}:{row.request_revision}:working",
                    )
                ]
            )
        if row.request_status != "closed":
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Закрыть",
                        callback_data=f"ows:change:{row.service_request_id}:{row.request_revision}:closed",
                    )
                ]
            )
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if row.purpose == "author_updates":
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Отозвать согласие", callback_data=f"consent:{row.material_id}:revoke"
                    )
                ]
            ]
        )
    try:
        await bot.send_message(
            row.user_id, row.text, reply_markup=markup, request_timeout=15, parse_mode=None
        )
    except TelegramRetryAfter as error:
        await asyncio.to_thread(complete, engine, row.id, "pending", error.retry_after)
        if row.purpose == "owner_broadcast":
            retry_seconds = error.retry_after

            def postpone() -> None:
                with Session(engine) as session, session.begin():
                    session.execute(
                        update(OutboxMessage)
                        .where(
                            OutboxMessage.bot_id == bot.id,
                            OutboxMessage.purpose == "owner_broadcast",
                            OutboxMessage.status == "pending",
                        )
                        .values(ready_at=datetime.now(UTC) + timedelta(seconds=retry_seconds))
                    )

            await asyncio.to_thread(postpone)
            return False
    except TelegramNetworkError:
        await asyncio.to_thread(complete, engine, row.id, "uncertain")
    except TelegramForbiddenError:
        await asyncio.to_thread(complete, engine, row.id, "failed", 0, True)
    except TelegramAPIError:
        await asyncio.to_thread(complete, engine, row.id, "failed")
    else:
        await asyncio.to_thread(complete, engine, row.id, "sent")
    return True


async def background(bot: Bot, engine: Engine, settings: Settings | None = None) -> None:
    from leedbox.telegram.maintenance import maintain

    cycle = 0
    while True:
        try:
            await asyncio.to_thread(recover, engine, bot.id, bot.session.api == TEST)
            if settings is not None:
                from leedbox.application import youtube_billing
                from leedbox.application.ai_failures import recover_stale
                from leedbox.application.provider_balance import check
                from leedbox.application.provider_limits import refresh
                from leedbox.telegram.payment_operations import run_one

                await run_one(bot, engine, settings)
                await asyncio.to_thread(recover_stale, engine, settings, bot.id)
                await asyncio.to_thread(youtube_billing.recover_stale, engine, settings, bot.id)
                await asyncio.to_thread(check, engine, settings, bot.id)
                await asyncio.to_thread(refresh, engine, settings, bot.id)
            if cycle % 12 == 0:
                await asyncio.to_thread(purge_cache, engine)
                await maintain(bot, engine)
            for _ in range(10):
                if not await send_one(bot, engine, settings):
                    break
            cycle += 1
        except Exception:
            logging.getLogger(__name__).error("background_error")
        await asyncio.sleep(5)
