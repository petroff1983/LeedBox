"""Claim invite API effects in PostgreSQL before calling Telegram."""

import asyncio

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramServerError
from aiogram.types import ChatMemberUpdated, Update
from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.invites import (
    InviteRecord,
    claim_revocation,
    finish_revocation,
    observe_membership,
    snapshot,
)
from leedbox.domain.invites import member_status
from leedbox.infrastructure.models import Channel, OutboxMessage, TrackedLink, User


async def revoke_invite(bot: Bot, engine: Engine, owner_id: int, link_id: int) -> InviteRecord:
    def read() -> InviteRecord:
        with Session(engine) as session:
            return snapshot(session, owner_id, bot.id, link_id)

    def claim() -> bool:
        with Session(engine) as session, session.begin():
            return claim_revocation(session, owner_id, bot.id, link_id)

    record = await asyncio.to_thread(read)
    if not await asyncio.to_thread(claim):
        return await asyncio.to_thread(read)
    status = "revoked"
    try:
        response = await bot.revoke_chat_invite_link(
            record.channel_telegram_id,
            record.destination,
            request_timeout=15,
        )
        if not response.is_revoked or response.is_primary:
            status = "revoke_uncertain"
    except (TelegramNetworkError, TelegramServerError):
        status = "revoke_uncertain"
    except TelegramAPIError:
        status = "revoke_failed"

    def finish() -> None:
        with Session(engine) as session, session.begin():
            finish_revocation(session, owner_id, bot.id, link_id, status)

    await asyncio.to_thread(finish)
    return await asyncio.to_thread(read)


def build_membership_router(engine: Engine) -> Router:
    router = Router()

    @router.chat_member()
    async def membership(event: ChatMemberUpdated, bot: Bot, event_update: Update) -> None:
        if event.chat.type != "channel":
            return
        before, after = event.old_chat_member, event.new_chat_member
        joined = (
            not member_status(before.status, getattr(before, "is_member", False))
            and member_status(after.status, getattr(after, "is_member", False))
            and not after.user.is_bot
        )

        def record() -> None:
            with Session(engine) as session, session.begin():
                observe_membership(
                    session,
                    bot.id,
                    event_update.update_id,
                    event.chat.id,
                    event.date,
                    after.user.id,
                    event.invite_link.invite_link if event.invite_link else None,
                    joined,
                )

        await asyncio.to_thread(record)

    @router.my_chat_member()
    async def own_membership(event: ChatMemberUpdated, bot: Bot, event_update: Update) -> None:
        if event.chat.type != "channel" or event.new_chat_member.user.id != bot.id:
            return

        def record() -> None:
            with Session(engine) as session, session.begin():
                channel = session.scalar(
                    select(Channel).where(Channel.telegram_id == event.chat.id).with_for_update()
                )
                if (
                    channel is None
                    or session.scalar(
                        select(TrackedLink.id)
                        .where(
                            TrackedLink.channel_id == channel.id,
                            TrackedLink.bot_id == bot.id,
                        )
                        .limit(1)
                    )
                    is None
                ):
                    return
                ok = event.new_chat_member.status in {"administrator", "creator"}
                previous = channel.bot_rights
                channel.bot_rights = "ok" if ok else "no_rights"
                if not ok:
                    channel.membership_gap = True
                    user = session.get(User, channel.owner_user_id)
                    if user is not None and previous != "no_rights":
                        session.execute(
                            insert(OutboxMessage)
                            .values(
                                bot_id=bot.id,
                                telegram_user_id=user.telegram_id,
                                purpose="channel_rights",
                                text=(
                                    f"Бот потерял права в канале {channel.title}. "
                                    "Проверьте настройки администраторов; "
                                    "наблюдение вступлений может быть неполным."
                                ),
                                dedupe_key=f"my_rights:{bot.id}:{event_update.update_id}",
                            )
                            .on_conflict_do_nothing()
                        )

        await asyncio.to_thread(record)

    return router
