from secrets import token_hex
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import require_access
from leedbox.application.projects import get_project
from leedbox.application.results import save_result
from leedbox.application.settings import product_event
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.links import (
    PRESETS,
    UTM_KEYS,
    csv_export,
    label,
    text_field,
    utm_url,
)
from leedbox.domain.smart_links import app_target
from leedbox.infrastructure.models import (
    LinkEvent,
    ToolResult,
    TrackedLink,
    User,
)


def create_smart_link(
    session: Session,
    owner_id: int,
    project_id: int,
    bot_id: int,
    destination: str,
    kind: str,
    action_key: str,
    *,
    emit_event: bool = True,
) -> TrackedLink:
    if kind not in {"short", "deeplink"}:
        raise ValueError("Выберите короткую ссылку или DeepLink")
    target = app_target(destination)
    require_access(session, owner_id)
    get_project(session, owner_id, project_id)
    session.execute(select(User).where(User.id == owner_id).with_for_update())
    previous = session.scalar(select(TrackedLink).where(TrackedLink.action_key == action_key))
    if previous:
        if previous.owner_user_id != owner_id or previous.bot_id != bot_id:
            raise ObjectUnavailable
        return previous
    link = TrackedLink(
        owner_user_id=owner_id,
        project_id=project_id,
        bot_id=bot_id,
        kind=kind,
        code=token_hex(6),
        name=target.name,
        source="",
        campaign="",
        destination=target.destination,
        action_key=action_key,
    )
    session.add(link)
    session.flush()
    if emit_event:
        product_event(session, bot_id, None, "link_created", kind=kind)
    return link


def utm_batch(urls: str, tags: dict[str, str | None], *, replace: bool = False) -> list[str]:
    values = [value.strip() for value in urls.splitlines() if value.strip()]
    if not 1 <= len(values) <= 20:
        raise ValueError("Укажите от 1 до 20 ссылок, каждую с новой строки")
    return [utm_url(value, tags, replace=replace) for value in values]


def save_utm(
    session: Session,
    owner_id: int,
    project_id: int,
    bot_id: int,
    values: dict[str, Any],
    action_key: str,
) -> ToolResult:
    if values["platform"] not in PRESETS:
        raise ValueError("Выберите платформу")
    text_field(values["name"], limit=60)
    tags = {key: values.get(key) for key in UTM_KEYS}
    urls = utm_batch(values["urls"], tags, replace=values.get("replace") is True)
    session.execute(select(User).where(User.id == owner_id).with_for_update())
    previous = session.scalar(select(ToolResult.id).where(ToolResult.action_key == action_key))
    result = save_result(
        session,
        owner_id,
        project_id,
        bot_id,
        "utm",
        values,
        "\n\n".join(urls),
        action_key,
        "utm-1",
    )
    if previous is None:
        product_event(session, bot_id, None, "link_created", kind="utm")
    return result


def utm_csv(result: ToolResult) -> bytes:
    if result.tool != "utm":
        raise ObjectUnavailable
    tags = [label(result.inputs.get(key) or "", required=key in UTM_KEYS[:3]) for key in UTM_KEYS]
    urls = utm_batch(
        result.inputs["urls"],
        {key: result.inputs.get(key) for key in UTM_KEYS},
        replace=result.inputs.get("replace") is True,
    )
    return csv_export(
        [
            ["url", *[f"utm_{key}" for key in UTM_KEYS]],
            *[[url, *tags] for url in urls],
        ]
    )


def get_link(session: Session, owner_id: int, bot_id: int, link_id: int) -> TrackedLink:
    link = session.scalar(
        select(TrackedLink).where(
            TrackedLink.id == link_id,
            TrackedLink.owner_user_id == owner_id,
            TrackedLink.bot_id == bot_id,
        )
    )
    if link is None:
        raise ObjectUnavailable
    return link


def set_enabled(session: Session, owner_id: int, bot_id: int, link_id: int, enabled: bool) -> None:
    link = get_link(session, owner_id, bot_id, link_id)
    if link.kind == "invite":
        raise ValueError("Пригласительная ссылка должна быть отозвана через Telegram API")
    link.enabled = enabled


def record_start(
    session: Session,
    bot_id: int,
    payload: str,
    telegram_id: int,
    event_key: str,
) -> bool | None:
    """None = canonical/untracked link, False = explicitly disabled tracked link."""
    link = session.scalar(
        select(TrackedLink)
        .where(
            TrackedLink.payload == payload,
            TrackedLink.bot_id == bot_id,
        )
        .with_for_update()
    )
    if link is None:
        return None
    if not link.enabled:
        return False
    if session.scalar(select(LinkEvent.id).where(LinkEvent.event_key == event_key)) is None:
        session.add(
            LinkEvent(
                link_id=link.id, kind="start", telegram_user_id=telegram_id, event_key=event_key
            )
        )
    return True


def stats(session: Session, link_id: int) -> tuple[int, int, int]:
    clicks = (
        session.scalar(
            select(func.count())
            .select_from(LinkEvent)
            .where(
                LinkEvent.link_id == link_id,
                LinkEvent.kind == "click",
            )
        )
        or 0
    )
    starts = (
        session.scalar(
            select(func.count())
            .select_from(LinkEvent)
            .where(
                LinkEvent.link_id == link_id,
                LinkEvent.kind == "start",
            )
        )
        or 0
    )
    unique = (
        session.scalar(
            select(func.count(func.distinct(LinkEvent.telegram_user_id))).where(
                LinkEvent.link_id == link_id,
                LinkEvent.kind == "start",
            )
        )
        or 0
    )
    return clicks, starts, unique


def search_links(
    session: Session, owner_id: int, bot_id: int, query: str = "", page: int = 0
) -> list[TrackedLink]:
    query = text_field(query, required=False, limit=100)
    if not 0 <= page <= 10000:
        raise ValueError("Некорректная страница")
    selection = select(TrackedLink).where(
        TrackedLink.owner_user_id == owner_id, TrackedLink.bot_id == bot_id
    )
    if query:
        selection = selection.where(
            or_(
                TrackedLink.name.icontains(query, autoescape=True),
                TrackedLink.source.icontains(query, autoescape=True),
                TrackedLink.campaign.icontains(query, autoescape=True),
            )
        )
    return list(
        session.scalars(selection.order_by(TrackedLink.id.desc()).offset(page * 5).limit(6))
    )
