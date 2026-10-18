"""Named owner-scoped library without paid calls or modification of AI snapshots."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import AIOperation, WritingPrompt


def ensure(db: Session, owner: int, bot: int, test: bool) -> None:
    rows = db.scalars(
        select(AIOperation)
        .outerjoin(
            WritingPrompt,
            WritingPrompt.operation_id == AIOperation.id,
        )
        .where(
            AIOperation.owner_user_id == owner,
            AIOperation.bot_id == bot,
            AIOperation.test_mode == test,
            AIOperation.status == "completed",
            AIOperation.snapshot["purpose"].astext == "writing_profile",
            WritingPrompt.id.is_(None),
        )
        .order_by(AIOperation.id)
    )
    for row in rows:
        if not isinstance((row.result or {}).get("prompt"), str):
            continue
        topic = row.snapshot.get("inputs", {}).get("brief", {}).get("topic", "Промпт для статей")
        db.execute(
            insert(WritingPrompt)
            .values(
                owner_user_id=owner,
                bot_id=bot,
                test_mode=test,
                operation_id=row.id,
                name=str(topic).strip()[:80] or "Промпт для статей",
                created_at=row.created_at,
            )
            .on_conflict_do_nothing(index_elements=["operation_id"])
        )


def page(db: Session, owner: int, bot: int, test: bool, index: int = 0) -> list[WritingPrompt]:
    if not 0 <= index <= 100000:
        raise ValueError("Неверная страница")
    return list(
        db.scalars(
            select(WritingPrompt)
            .where(
                WritingPrompt.owner_user_id == owner,
                WritingPrompt.bot_id == bot,
                WritingPrompt.test_mode == test,
                WritingPrompt.archived.is_(False),
            )
            .order_by(WritingPrompt.id.desc())
            .offset(index * 7)
            .limit(8)
        )
    )


def get(db: Session, owner: int, bot: int, test: bool, number: int) -> WritingPrompt:
    row = db.scalar(
        select(WritingPrompt).where(
            WritingPrompt.id == number,
            WritingPrompt.owner_user_id == owner,
            WritingPrompt.bot_id == bot,
            WritingPrompt.test_mode == test,
            WritingPrompt.archived.is_(False),
        )
    )
    if row is None:
        raise ObjectUnavailable
    return row


def rename(db: Session, owner: int, bot: int, test: bool, number: int, name: str) -> None:
    name = " ".join(name.split())
    if not 1 <= len(name) <= 80:
        raise ValueError("Название: от 1 до 80 символов")
    get(db, owner, bot, test, number).name = name
