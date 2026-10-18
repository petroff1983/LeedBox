"""Owner-scoped attribution for material links. No messaging or advertising."""

from secrets import token_hex

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import require_access
from leedbox.application.materials import owned_material
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import http_url
from leedbox.infrastructure.models import LinkEvent, Material, Recipient, TrackedLink, User


def material_for(session: Session, owner_id: int, bot_id: int, material_id: int) -> Material:
    material = owned_material(session, owner_id, material_id)
    if material.bot_id != bot_id or material.archived:
        raise ObjectUnavailable
    return material


def create_source(
    session: Session,
    owner_id: int,
    bot_id: int,
    material_id: int,
    platform: str,
    placement: str,
    publication_url: str,
    username: str,
    action_key: str,
) -> TrackedLink:
    require_access(session, owner_id)
    session.execute(select(User).where(User.id == owner_id).with_for_update())
    material = material_for(session, owner_id, bot_id, material_id)
    platform, placement = platform.strip(), placement.strip()
    if not 1 <= len(platform) <= 40 or not 1 <= len(placement) <= 200:
        raise ValueError("Площадка: до 40 символов; название размещения: до 200 символов")
    if any(ord(char) < 32 for char in platform + placement):
        raise ValueError("Введите название в одну строку")
    publication_url = http_url(publication_url) if publication_url else ""
    if len(publication_url) > 2000:
        raise ValueError("Адрес публикации слишком длинный")
    existing = session.scalar(select(TrackedLink).where(TrackedLink.action_key == action_key))
    if existing:
        if (
            existing.owner_user_id != owner_id
            or existing.bot_id != bot_id
            or existing.material_id != material_id
            or existing.kind != "material"
        ):
            raise ObjectUnavailable
        return existing
    code = token_hex(6)
    payload = f"{material.slug}_{code}"
    if len(payload) > 64:
        raise ValueError("Идентификатор материала слишком длинный для Telegram-ссылки")
    link = TrackedLink(
        owner_user_id=owner_id,
        project_id=material.project_id,
        bot_id=bot_id,
        material_id=material.id,
        kind="material",
        code=code,
        name=f"{platform} · {placement}"[:60],
        source=platform,
        campaign=placement,
        external_url=publication_url or None,
        payload=payload,
        destination=f"https://t.me/{username}?start={payload}",
        action_key=action_key,
    )
    session.add(link)
    session.flush()
    return link


def source_page(
    session: Session, owner_id: int, bot_id: int, material_id: int, page: int = 0
) -> list[TrackedLink]:
    material_for(session, owner_id, bot_id, material_id)
    if not 0 <= page <= 10000:
        raise ValueError("Некорректная страница")
    return list(
        session.scalars(
            select(TrackedLink)
            .where(
                TrackedLink.owner_user_id == owner_id,
                TrackedLink.bot_id == bot_id,
                TrackedLink.material_id == material_id,
                TrackedLink.kind == "material",
            )
            .order_by(TrackedLink.id.desc())
            .offset(page * 5)
            .limit(6)
        )
    )


def source_for(session: Session, owner_id: int, bot_id: int, link_id: int) -> TrackedLink:
    link = session.scalar(
        select(TrackedLink).where(
            TrackedLink.id == link_id,
            TrackedLink.owner_user_id == owner_id,
            TrackedLink.bot_id == bot_id,
            TrackedLink.kind == "material",
        )
    )
    if link is None or link.material_id is None:
        raise ObjectUnavailable
    material_for(session, owner_id, bot_id, link.material_id)
    return link


def source_counts(session: Session, link: TrackedLink) -> tuple[int, int]:
    launches, people = session.execute(
        select(
            func.count(LinkEvent.id), func.count(func.distinct(LinkEvent.telegram_user_id))
        ).where(LinkEvent.link_id == link.id, LinkEvent.kind == "start")
    ).one()
    return int(launches), int(people)


def people_page(session: Session, link: TrackedLink, page: int = 0) -> list[tuple[int, str]]:
    if not 0 <= page <= 10000:
        raise ValueError("Некорректная страница")
    people = session.scalars(
        select(LinkEvent.telegram_user_id)
        .where(
            LinkEvent.link_id == link.id,
            LinkEvent.kind == "start",
            LinkEvent.telegram_user_id.is_not(None),
        )
        .group_by(LinkEvent.telegram_user_id)
        .order_by(func.max(LinkEvent.created_at).desc(), LinkEvent.telegram_user_id)
        .offset(page * 5)
        .limit(6)
    ).all()
    result = []
    for person in people:
        assert person is not None
        history = (
            select(TrackedLink.source, TrackedLink.campaign, LinkEvent.created_at)
            .join(LinkEvent, LinkEvent.link_id == TrackedLink.id)
            .where(
                TrackedLink.owner_user_id == link.owner_user_id,
                TrackedLink.bot_id == link.bot_id,
                TrackedLink.material_id == link.material_id,
                TrackedLink.kind == "material",
                LinkEvent.telegram_user_id == person,
                LinkEvent.kind == "start",
            )
        )
        delivered = (
            session.scalar(
                select(Recipient.id).where(
                    Recipient.material_id == link.material_id, Recipient.telegram_user_id == person
                )
            )
            is not None
        )
        first = session.execute(history.order_by(LinkEvent.created_at, LinkEvent.id).limit(1)).one()
        last = session.execute(
            history.order_by(LinkEvent.created_at.desc(), LinkEvent.id.desc()).limit(1)
        ).one()
        result.append(
            (
                person,
                f"👤 Telegram ID: {person}\n"
                f"Первый отмеченный источник: {first.source} · {first.campaign}\n"
                f"Последний: {last.source} · {last.campaign}\n"
                f"Первый отмеченный запуск: {first.created_at:%d.%m.%Y %H:%M} UTC\n"
                f"🎁 {'Подарок получен' if delivered else 'Выдача пока не подтверждена'}",
            )
        )
    return result
