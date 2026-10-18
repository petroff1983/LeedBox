"""Owner-scoped library projections. No Telegram calls or ORM objects escape sessions."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import can_check_subscriptions
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import (
    Channel,
    DeliveryEvent,
    Material,
    MaterialVersion,
    ProductEvent,
)


@dataclass(frozen=True)
class MaterialCard:
    id: int
    slug: str
    title: str
    description: str
    kind: str
    mode: str
    archived: bool
    version: int
    deliveries: int
    recipients: int
    starts: int
    refusals: tuple[tuple[str, int], ...]
    channel: str
    warning: str


@dataclass(frozen=True)
class LibraryPage:
    items: tuple[tuple[int, str], ...]
    page: int
    total: int


def library_page(
    session: Session, owner_id: int, bot_id: int, archived: bool, page: int
) -> LibraryPage:
    if page < 0:
        raise ValueError("Некорректная страница")
    filters = (
        Material.owner_user_id == owner_id,
        Material.bot_id == bot_id,
        Material.archived.is_(archived),
    )
    total = session.scalar(select(func.count()).select_from(Material).where(*filters)) or 0
    page = min(page, max(0, (total - 1) // 5))
    rows = session.execute(
        select(Material.id, MaterialVersion.title)
        .join(
            MaterialVersion,
            (MaterialVersion.material_id == Material.id)
            & (MaterialVersion.number == Material.current_version),
        )
        .where(*filters)
        .order_by(Material.id.asc())
        .offset(page * 5)
        .limit(5)
    )
    return LibraryPage(tuple((id, title) for id, title in rows), page, total)


def material_card(session: Session, owner_id: int, bot_id: int, material_id: int) -> MaterialCard:
    row = session.execute(
        select(Material, MaterialVersion)
        .join(
            MaterialVersion,
            (MaterialVersion.material_id == Material.id)
            & (MaterialVersion.number == Material.current_version),
        )
        .where(
            Material.id == material_id,
            Material.owner_user_id == owner_id,
            Material.bot_id == bot_id,
        )
    ).one_or_none()
    if row is None:
        raise ObjectUnavailable
    material, version = row
    deliveries, recipients = session.execute(
        select(func.count(), func.count(func.distinct(DeliveryEvent.telegram_user_id))).where(
            DeliveryEvent.material_id == material_id, DeliveryEvent.status == "sent"
        )
    ).one()
    starts = (
        session.scalar(
            select(func.count())
            .select_from(ProductEvent)
            .where(ProductEvent.material_id == material_id, ProductEvent.name == "user_started")
        )
        or 0
    )
    reason = ProductEvent.details["reason"].astext
    refusals = tuple(
        session.execute(
            select(reason, func.count())
            .where(ProductEvent.material_id == material_id, ProductEvent.name == "delivery_denied")
            .group_by(reason)
        ).tuples()
    )
    channel = session.get(Channel, material.channel_id) if material.channel_id else None
    checking = can_check_subscriptions(session, owner_id)
    mode = material.delivery_mode
    warning = ""
    if material.delivery_mode == "subscribers":
        if not checking:
            mode = "everyone"
            warning = (
                "⚠️ Проверка подписки отключена: нет оплаченного доступа. "
                "Подарок выдаётся всем по ссылке."
            )
        elif channel and channel.bot_rights == "no_rights":
            warning = "⚠️ Нет прав у бота"
    return MaterialCard(
        material.id,
        material.slug,
        version.title,
        version.description,
        version.content_type,
        mode,
        material.archived,
        material.current_version,
        deliveries,
        recipients,
        starts,
        refusals,
        channel.title if channel else "без канала",
        warning,
    )
