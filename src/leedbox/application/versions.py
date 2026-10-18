"""Immutable configuration history. Restoration is another validated change."""

import hashlib
import json
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from leedbox.infrastructure.models import ContentVersion


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def lock_content(session: Session, entity: str, key: str) -> None:
    number = int.from_bytes(
        hashlib.sha256(f"content:{entity}:{key}".encode()).digest()[:8], signed=True
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def record_version(
    session: Session,
    entity: str,
    key: str,
    actor: int | None,
    after: dict[str, Any],
    before: dict[str, Any] | None = None,
) -> None:
    lock_content(session, entity, key)
    last = (
        session.scalar(
            select(func.max(ContentVersion.number)).where(
                ContentVersion.entity == entity, ContentVersion.object_key == key
            )
        )
        or 0
    )
    if not last and before is not None:
        session.add(
            ContentVersion(
                entity=entity, object_key=key, number=1, data=before, actor_telegram_id=None
            )
        )
        last = 1
    session.add(
        ContentVersion(
            entity=entity, object_key=key, number=last + 1, data=after, actor_telegram_id=actor
        )
    )
    session.flush()
