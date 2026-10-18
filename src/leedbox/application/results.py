from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import require_access
from leedbox.application.projects import get_project
from leedbox.application.settings import product_event
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import ToolResult, User


def save_result(
    session: Session,
    owner_id: int,
    project_id: int,
    bot_id: int,
    tool: str,
    inputs: dict[str, Any],
    text: str,
    action_key: str,
    template_version: str = "calculations-1",
) -> ToolResult:
    require_access(session, owner_id)
    get_project(session, owner_id, project_id)
    session.execute(select(User).where(User.id == owner_id).with_for_update())
    previous = session.scalar(select(ToolResult).where(ToolResult.action_key == action_key))
    if previous:
        if previous.owner_user_id != owner_id or previous.bot_id != bot_id:
            raise ObjectUnavailable
        return previous
    value = ToolResult(
        owner_user_id=owner_id,
        project_id=project_id,
        bot_id=bot_id,
        tool=tool,
        template_version=template_version,
        inputs=inputs,
        result=text,
        action_key=action_key,
    )
    session.add(value)
    session.flush()
    product_event(session, bot_id, None, "tool_completed", tool=tool, version=template_version)
    return value


def get_result(session: Session, owner_id: int, bot_id: int, id: int) -> ToolResult:
    if not 0 < id <= 2**63 - 1:
        raise ObjectUnavailable
    value = session.scalar(
        select(ToolResult).where(
            ToolResult.id == id, ToolResult.owner_user_id == owner_id, ToolResult.bot_id == bot_id
        )
    )
    if value is None:
        raise ObjectUnavailable
    return value


def history_page(
    session: Session, owner_id: int, bot_id: int, project_id: int | None = None, page: int = 0
) -> list[ToolResult]:
    if not 0 <= page <= 10000:
        raise ValueError("Некорректная страница")
    query = select(ToolResult).where(
        ToolResult.owner_user_id == owner_id, ToolResult.bot_id == bot_id
    )
    if project_id is not None:
        get_project(session, owner_id, project_id)
        query = query.where(ToolResult.project_id == project_id)
    return list(session.scalars(query.order_by(ToolResult.id.desc()).offset(page * 5).limit(6)))


def delete_result(session: Session, owner_id: int, bot_id: int, id: int) -> None:
    value = get_result(session, owner_id, bot_id, id)
    session.delete(value)
