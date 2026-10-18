from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.links import get_link
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import AuditLog, Channel, JoinEvent, TrackedLink


@dataclass(frozen=True)
class InviteRecord:
    id: int
    channel_telegram_id: int
    name: str
    expires_at: datetime | None
    member_limit: int | None
    status: str
    destination: str


def snapshot(session: Session, owner_id: int, bot_id: int, link_id: int) -> InviteRecord:
    link = get_link(session, owner_id, bot_id, link_id)
    channel = session.scalar(
        select(Channel).where(
            Channel.id == link.channel_id,
            Channel.owner_user_id == owner_id,
        )
    )
    if link.kind != "invite" or channel is None:
        raise ObjectUnavailable
    return InviteRecord(
        link.id,
        channel.telegram_id,
        link.name,
        link.expires_at,
        link.member_limit,
        link.status,
        link.destination,
    )


def locked_invite(session: Session, owner_id: int, bot_id: int, link_id: int) -> TrackedLink:
    link = session.scalar(
        select(TrackedLink)
        .where(
            TrackedLink.id == link_id,
            TrackedLink.owner_user_id == owner_id,
            TrackedLink.bot_id == bot_id,
            TrackedLink.kind == "invite",
        )
        .with_for_update()
    )
    if link is None:
        raise ObjectUnavailable
    return link


def claim_revocation(session: Session, owner_id: int, bot_id: int, link_id: int) -> bool:
    link = locked_invite(session, owner_id, bot_id, link_id)
    if link.status == "revoked":
        return False
    if not link.destination or link.status in {"creating", "uncertain"}:
        raise ValueError("Результат создания неизвестен. Проверьте приглашения в настройках канала")
    if (
        link.status == "revoking"
        and link.operation_at
        and (datetime.now(UTC) - link.operation_at < timedelta(minutes=5))
    ):
        return False
    link.status, link.enabled, link.operation_at = "revoking", False, datetime.now(UTC)
    return True


def finish_revocation(
    session: Session, owner_id: int, bot_id: int, link_id: int, status: str
) -> None:
    if status not in {"revoked", "revoke_failed", "revoke_uncertain"}:
        raise ValueError("Invalid revocation result")
    link = locked_invite(session, owner_id, bot_id, link_id)
    if link.status not in {"revoking", "revoke_uncertain"}:
        return
    link.status = status
    if status == "revoked":
        session.add(
            AuditLog(
                action="invite_revoked", subject_user_id=owner_id, details={"link_id": link.id}
            )
        )


def observe_membership(
    session: Session,
    bot_id: int,
    update_id: int,
    channel_telegram_id: int,
    at: datetime,
    telegram_id: int,
    invite_url: str | None,
    joined: bool,
) -> None:
    channel = session.scalar(
        select(Channel).where(Channel.telegram_id == channel_telegram_id).with_for_update()
    )
    if channel is None:
        return
    watched = session.scalar(
        select(TrackedLink.id)
        .where(
            TrackedLink.channel_id == channel.id,
            TrackedLink.bot_id == bot_id,
            TrackedLink.kind == "invite",
        )
        .limit(1)
    )
    if watched is None:
        return
    if channel.membership_observed_from is None or at < channel.membership_observed_from:
        channel.membership_observed_from = at
    if channel.membership_last_observed_at is None or at > channel.membership_last_observed_at:
        channel.membership_last_observed_at = at
    if not joined or invite_url is None:
        return
    link = session.scalar(
        select(TrackedLink).where(
            TrackedLink.channel_id == channel.id,
            TrackedLink.bot_id == bot_id,
            TrackedLink.kind == "invite",
            TrackedLink.destination == invite_url,
        )
    )
    # Telegram timestamps have second precision, PostgreSQL timestamps have microseconds.
    if link is None or at < link.created_at.replace(microsecond=0):
        return
    session.execute(
        insert(JoinEvent)
        .values(
            link_id=link.id,
            bot_id=bot_id,
            update_id=update_id,
            telegram_user_id=telegram_id,
            joined_at=at,
        )
        .on_conflict_do_nothing(index_elements=[JoinEvent.bot_id, JoinEvent.update_id])
    )


def join_summary(session: Session, link: TrackedLink) -> str:
    channel = session.get(Channel, link.channel_id) if link.channel_id else None
    if channel is None or channel.membership_observed_from is None:
        return "Вступления: нет данных — события членства ещё не наблюдались"
    count, unique = session.execute(
        select(func.count(), func.count(func.distinct(JoinEvent.telegram_user_id))).where(
            JoinEvent.link_id == link.id,
        )
    ).one()
    start = max(channel.membership_observed_from, link.created_at).strftime("%d.%m.%Y %H:%M UTC")
    warning = "\nНаблюдение неполное: бот терял права" if channel.membership_gap else ""
    if channel.bot_rights != "ok":
        warning += "\nСейчас права бота не подтверждены; новые события могут быть недоступны"
    return f"Наблюдаемые вступления: {count}; уникальные: {unique}\nНаблюдение с {start}{warning}"


def recover_operations(session: Session, bot_id: int, now: datetime | None = None) -> None:
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=5)
    for old, new in (("creating", "uncertain"), ("revoking", "revoke_uncertain")):
        session.execute(
            update(TrackedLink)
            .where(
                TrackedLink.bot_id == bot_id,
                TrackedLink.kind == "invite",
                TrackedLink.status == old,
                TrackedLink.operation_at < cutoff,
            )
            .values(status=new, enabled=False)
        )
