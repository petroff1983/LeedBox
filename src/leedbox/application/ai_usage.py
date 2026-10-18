"""Persisted per-session usage, including free and unlimited runs, without new AI calls."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from leedbox.application import ai_tokens
from leedbox.infrastructure.models import AIOperation, ContentTask


def operation_receipt(db: Session, row: AIOperation) -> str:
    return ai_tokens.receipt(row)


def task_total(db: Session, task: ContentTask) -> int:
    steps: Iterable[list[dict[str, Any]]]
    if task.payload.get("generation_only") or task.payload.get("phase") == "request":
        root = task.payload.get("analysis_token", task.token)
        steps = db.scalars(
            select(ContentTask.steps).where(
                ContentTask.owner_user_id == task.owner_user_id,
                ContentTask.bot_id == task.bot_id,
                ContentTask.test_mode == task.test_mode,
                or_(
                    ContentTask.token == root, ContentTask.payload["analysis_token"].astext == root
                ),
            )
        )
    else:
        steps = [task.steps]
    # A generation child includes the cached analysis with zero usage; never charge/count it twice.
    return sum(step.get("usage", {}).get("total_tokens", 0) for group in steps for step in group)


def task_receipt(db: Session, task: ContentTask) -> str:
    operation = db.get(AIOperation, task.operation_id) if task.operation_id else None
    return ai_tokens.receipt(operation) if operation else ""
