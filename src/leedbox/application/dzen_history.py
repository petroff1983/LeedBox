"""Owner-scoped immutable search snapshots and links to already-paid headline results."""

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import AIOperation, DzenSearch


def save(
    db: Session, owner: int, bot: int, test: bool, token: str, report: dict[str, Any]
) -> DzenSearch:
    if not token.isascii() or not token.isalnum() or not 1 <= len(token) <= 32:
        raise ValueError("Неверный код подборки")
    posts = report.get("posts")
    if (
        not isinstance(posts, list)
        or len(posts) > 5
        or any(not isinstance(p, dict) or not isinstance(p.get("title"), str) for p in posts)
    ):
        raise ValueError("Неполная подборка")
    db.execute(
        insert(DzenSearch)
        .values(owner_user_id=owner, bot_id=bot, test_mode=test, token=token, report=report)
        .on_conflict_do_nothing()
    )
    row = get(db, owner, bot, test, token)
    if row.report != report:
        raise ValueError("Сохранённую подборку нельзя перезаписать")
    return row


def get(db: Session, owner: int, bot: int, test: bool, token: str) -> DzenSearch:
    row = db.scalar(
        select(DzenSearch).where(
            DzenSearch.owner_user_id == owner,
            DzenSearch.bot_id == bot,
            DzenSearch.test_mode == test,
            DzenSearch.token == token,
        )
    )
    if row is None:
        raise ObjectUnavailable
    return row


def latest(db: Session, owner: int, bot: int, test: bool, limit: int = 20) -> list[DzenSearch]:
    return list(
        db.scalars(
            select(DzenSearch)
            .where(
                DzenSearch.owner_user_id == owner,
                DzenSearch.bot_id == bot,
                DzenSearch.test_mode == test,
            )
            .order_by(DzenSearch.id.desc())
            .limit(limit)
        )
    )


def headlines(
    db: Session, owner: int, bot: int, test: bool, token: str, selected: dict[str, Any]
) -> AIOperation | None:
    return db.scalar(
        select(AIOperation)
        .where(
            AIOperation.owner_user_id == owner,
            AIOperation.bot_id == bot,
            AIOperation.test_mode == test,
            AIOperation.status == "completed",
            AIOperation.kind == "generation",
            or_(
                AIOperation.snapshot["search_token"].astext == token,
                AIOperation.snapshot["search_token"].astext.is_(None),
            ),
            AIOperation.snapshot["inputs"]["selected"] == selected,
        )
        .order_by(AIOperation.id.desc())
        .limit(1)
    )
