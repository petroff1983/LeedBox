from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import require_access
from leedbox.application.projects import get_project
from leedbox.application.settings import product_event, setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import Content, http_url
from leedbox.infrastructure.models import Channel, Material, MaterialVersion, User


def connect_channel(
    session: Session, owner_user_id: int, telegram_id: int, title: str, join_url: str
) -> Channel:
    # Only called after Telegram gateway has confirmed both sets of privileges.
    if telegram_id >= 0:
        raise ValueError("Нужен числовой ID Telegram-канала")
    require_access(session, owner_user_id)
    http_url(join_url)
    # Lock user to serialize connection and publication limits for this owner.
    session.execute(select(User).where(User.id == owner_user_id).with_for_update())
    channel = session.scalar(select(Channel).where(Channel.telegram_id == telegram_id))
    if channel:
        if channel.owner_user_id != owner_user_id:
            raise ObjectUnavailable
        channel.title, channel.join_url = title, join_url
    else:
        channel = Channel(
            owner_user_id=owner_user_id, telegram_id=telegram_id, title=title, join_url=join_url
        )
        session.add(channel)
    session.flush()
    channel.bot_rights = "ok"
    return channel


def owned_material(session: Session, owner_user_id: int, material_id: int) -> Material:
    material = session.scalar(
        select(Material)
        .where(Material.id == material_id, Material.owner_user_id == owner_user_id)
        .with_for_update()
    )
    if material is None:
        raise ObjectUnavailable
    return material


def publish(
    session: Session,
    owner_user_id: int,
    project_id: int,
    bot_id: int,
    content: Content,
    mode: str,
    channel_id: int | None,
    limit: int | None = None,
    publication_key: str | None = None,
) -> Material:
    content.validate()
    require_access(session, owner_user_id)
    if not setting(session, "materials_enabled"):
        raise ValueError("Публикация материалов временно отключена")
    limit = limit if limit is not None else int(setting(session, "active_material_limit"))
    project = get_project(session, owner_user_id, project_id)
    if project.archived:
        raise ValueError("Проект в архиве")
    if mode not in {"everyone", "subscribers"}:
        raise ValueError("Неизвестный режим выдачи")
    if channel_id is not None:
        channel = session.scalar(
            select(Channel).where(Channel.id == channel_id, Channel.owner_user_id == owner_user_id)
        )
        if channel is None:
            raise ObjectUnavailable
    elif mode == "subscribers":
        raise ValueError("Для выдачи подписчикам нужен канал")
    session.execute(select(User).where(User.id == owner_user_id).with_for_update())
    if publication_key:
        existing = session.scalar(
            select(Material).where(Material.publication_key == publication_key)
        )
        if existing:
            if existing.owner_user_id != owner_user_id or existing.bot_id != bot_id:
                raise ObjectUnavailable
            return existing
    active = session.scalars(
        select(Material.id).where(
            Material.owner_user_id == owner_user_id, Material.archived.is_(False)
        )
    ).all()
    if len(active) >= limit:
        raise ValueError("Достигнут лимит активных материалов")
    material = Material(
        owner_user_id=owner_user_id,
        project_id=project_id,
        bot_id=bot_id,
        channel_id=channel_id,
        slug=f"lm_{uuid4().hex[:12]}",
        delivery_mode=mode,
        publication_key=publication_key,
    )
    session.add(material)
    session.flush()
    session.add(
        MaterialVersion(
            material_id=material.id,
            number=1,
            title=content.title,
            description=content.description,
            content_type=content.kind,
            content=content.value,
        )
    )
    session.flush()
    product_event(
        session, bot_id, None, "material_published", material.id, mode=mode, kind=content.kind
    )
    return material


def revise(session: Session, owner_user_id: int, material_id: int, content: Content) -> None:
    content.validate()
    require_access(session, owner_user_id)
    material = owned_material(session, owner_user_id, material_id)
    material.current_version += 1
    session.add(
        MaterialVersion(
            material_id=material.id,
            number=material.current_version,
            title=content.title,
            description=content.description,
            content_type=content.kind,
            content=content.value,
        )
    )
    session.flush()


def archive(
    session: Session,
    owner_user_id: int,
    material_id: int,
    archived: bool = True,
    limit: int | None = None,
) -> None:
    limit = limit if limit is not None else int(setting(session, "active_material_limit"))
    session.execute(select(User).where(User.id == owner_user_id).with_for_update())
    material = owned_material(session, owner_user_id, material_id)
    if not archived and material.archived:
        require_access(session, owner_user_id)
        active = session.scalars(
            select(Material.id).where(
                Material.owner_user_id == owner_user_id, Material.archived.is_(False)
            )
        ).all()
        if len(active) >= limit:
            raise ValueError("Достигнут лимит активных материалов")
    material.archived = archived
    session.flush()


def revise_material(
    session: Session,
    owner_user_id: int,
    material_id: int,
    bot_id: int,
    expected_version: int,
    content: Content,
    mode: str,
    channel_id: int | None,
) -> str:
    require_access(session, owner_user_id)
    content.validate()
    session.execute(select(User).where(User.id == owner_user_id).with_for_update())
    material = owned_material(session, owner_user_id, material_id)
    if material.bot_id != bot_id:
        raise ObjectUnavailable
    if material.current_version != expected_version:
        raise ValueError("Материал уже изменён. Откройте карточку и начните редактирование заново")
    if mode not in {"everyone", "subscribers"}:
        raise ValueError("Неизвестный режим выдачи")
    if channel_id is not None:
        channel = session.scalar(
            select(Channel).where(
                Channel.id == channel_id,
                Channel.owner_user_id == owner_user_id,
            )
        )
        if channel is None:
            raise ObjectUnavailable
    elif mode == "subscribers":
        raise ValueError("Для выдачи подписчикам нужен канал")
    revise(session, owner_user_id, material.id, content)
    material.delivery_mode, material.channel_id = mode, channel_id
    session.flush()
    return material.slug
