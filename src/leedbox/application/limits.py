from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.infrastructure.models import RateBucket


def release(session: Session, key: str) -> None:
    """Return one personal quota slot after a confirmed service-side failure.

    The caller must perform this once, in the failure-settlement transaction.
    Global request limits intentionally are not released.
    """
    session.execute(
        update(RateBucket)
        .where(RateBucket.key == key, RateBucket.count > 0)
        .values(count=RateBucket.count - 1)
    )


def take(session: Session, key: str, limit: int, seconds: int, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    session.execute(
        insert(RateBucket).values(key=key, started_at=now, count=0).on_conflict_do_nothing()
    )
    bucket = session.scalar(select(RateBucket).where(RateBucket.key == key).with_for_update())
    assert bucket is not None
    if now >= bucket.started_at + timedelta(seconds=seconds):
        bucket.started_at, bucket.count = now, 0
    if bucket.count >= limit:
        return False
    bucket.count += 1
    return True
