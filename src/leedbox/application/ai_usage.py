"""Persisted per-session usage, including free and unlimited runs, without new AI calls."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from leedbox.application import ai_tokens
from leedbox.infrastructure.models import AIOperation, ContentTask


def operation_receipt(db: Session, row: AIOperation) -> str:
    total = row.input_tokens + row.output_tokens
    token = row.snapshot.get("search_token")
    if token:
        total = (
            db.scalar(
                select(
                    func.coalesce(func.sum(AIOperation.input_tokens + AIOperation.output_tokens), 0)
                ).where(
                    AIOperation.owner_user_id == row.owner_user_id,
                    AIOperation.bot_id == row.bot_id,
                    AIOperation.test_mode == row.test_mode,
                    AIOperation.kind == "generation",
                    AIOperation.snapshot["search_token"].astext == token,
                )
            )
            or 0
        )
    return ai_tokens.receipt(row, total)


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
    return ai_tokens.receipt(operation, task_total(db, task)) if operation else ""
