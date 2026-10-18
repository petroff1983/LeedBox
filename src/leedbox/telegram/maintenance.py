import asyncio
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import Engine, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.entitlements import access_for
from leedbox.application.invites import recover_operations
from leedbox.application.settings import product_event
from leedbox.infrastructure.models import (
    Channel,
    DeliveryEvent,
    Material,
    OutboxMessage,
    Setting,
    TrackedLink,
    User,
)


def access_notices(engine: Engine, bot_id: int) -> None:
    now = datetime.now(UTC)
    day = now.strftime("%Y-%m-%d")
    with Session(engine) as session, session.begin():
        for user in session.scalars(select(User).where(User.expert_started_at.is_not(None))):
            result = access_for(session, user.id, now)
            state_key = f"access_state:{user.id}"
            old = session.get(Setting, state_key)
            previous = old.value if old else None
            if previous != result.state:
                session.execute(
                    insert(Setting)
                    .values(key=state_key, value=result.state)
                    .on_conflict_do_update(
                        index_elements=[Setting.key], set_={"value": result.state}
                    )
                )
                product_event(
                    session,
                    bot_id,
                    user.telegram_id,
                    "access_state_changed",
                    state=result.state,
                    previous=previous,
                )
            if result.state == "free":
                continue
            remaining = (result.ends_at - now).days if result.ends_at else 0
            text = ""
            if result.state in {"trial", "active"} and remaining in {7, 3, 1}:
                text = (
                    f"Ваш доступ заканчивается через {remaining} дн. "
                    + "Откройте «Мой доступ» в профиле."
                )
            elif result.state == "grace":
                text = (
                    "Основной срок доступа закончился. Действует льготный период. "
                    "Откройте «Мой доступ» для продления."
                )
            elif result.state == "expired":
                count = (
                    session.scalar(
                        select(func.count())
                        .select_from(DeliveryEvent)
                        .join(Material, Material.id == DeliveryEvent.material_id)
                        .where(
                            Material.owner_user_id == user.id,
                            Material.bot_id == bot_id,
                            DeliveryEvent.status == "sent",
                            DeliveryEvent.subscription_bypassed.is_(True),
                            DeliveryEvent.created_at
                            >= now.replace(hour=0, minute=0, second=0, microsecond=0)
                            - timedelta(days=1),
                            DeliveryEvent.created_at
                            < now.replace(hour=0, minute=0, second=0, microsecond=0),
                        )
                    )
                    or 0
                )
                text = (
                    f"Доступ закончился. Выдач без проверки подписки за вчера (UTC): {count}. "
                    "Для продления откройте «Мой доступ» в профиле."
                )
            if text:
                session.execute(
                    insert(OutboxMessage)
                    .values(
                        bot_id=bot_id,
                        telegram_user_id=user.telegram_id,
                        purpose="access_notice",
                        text=text,
                        dedupe_key=f"access_notice:{bot_id}:{user.id}:{day}",
                    )
                    .on_conflict_do_nothing()
                )


async def maintain(bot: Bot, engine: Engine) -> None:
    def recover() -> None:
        with Session(engine) as session, session.begin():
            recover_operations(session, bot.id)

    await asyncio.to_thread(recover)
    await asyncio.to_thread(access_notices, engine, bot.id)
    now = datetime.now(UTC)

    def due() -> list[tuple[int, int]]:
        with Session(engine) as session:
            return list(
                session.execute(
                    select(Channel.id, Channel.telegram_id)
                    .where(
                        or_(
                            Channel.id.in_(
                                select(Material.channel_id).where(Material.bot_id == bot.id)
                            ),
                            Channel.id.in_(
                                select(TrackedLink.channel_id).where(
                                    TrackedLink.bot_id == bot.id, TrackedLink.kind == "invite"
                                )
                            ),
                        ),
                        (Channel.rights_checked_at.is_(None))
                        | (Channel.rights_checked_at < now - timedelta(hours=6)),
                    )
                    .limit(5)
                ).tuples()
            )

    for id, telegram_id in await asyncio.to_thread(due):
        try:
            member = await bot.get_chat_member(telegram_id, bot.id, request_timeout=10)
            status = "ok" if member.status in {"administrator", "creator"} else "no_rights"
        except TelegramForbiddenError:
            status = "no_rights"
        except TelegramAPIError:
            status = "unknown"

        def save(channel_id: int, rights: str) -> None:
            with Session(engine) as session, session.begin():
                channel = session.get(Channel, channel_id)
                if not channel:
                    return
                channel.rights_checked_at = now
                if rights == "unknown":
                    return
                previous, channel.bot_rights = channel.bot_rights, rights
                if rights == "no_rights":
                    channel.membership_gap = True
                user = session.get(User, channel.owner_user_id)
                if rights == "no_rights" and previous != rights and user:
                    session.execute(
                        insert(OutboxMessage)
                        .values(
                            bot_id=bot.id,
                            telegram_user_id=user.telegram_id,
                            purpose="channel_rights",
                            text=(
                                f"У бота нет прав администратора канала {channel.title}. "
                                "Выдача подписчикам требует восстановления прав."
                            ),
                            dedupe_key=f"rights:{channel_id}:{now.isoformat()}",
                        )
                        .on_conflict_do_nothing()
                    )

        await asyncio.to_thread(save, id, status)
