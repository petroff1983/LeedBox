"""Shared public cache, PostgreSQL quotas and coalescing; no private competitor data."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, delete, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application import ai_tokens
from leedbox.application.entitlements import require_access
from leedbox.application.settings import setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.youtube import ChannelSnapshot, channel_reference
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import RateBucket, User, YouTubeCache
from leedbox.infrastructure.youtube import YouTubeClient, YouTubeError


def snapshot_key(config: Settings, raw: str) -> str:
    reference = channel_reference(raw)
    fingerprint = sha256(config.youtube_api_key.get_secret_value().encode()).hexdigest()
    return sha256(f"v1:{fingerprint}:{reference}:{config.youtube_scan_limit}".encode()).hexdigest()


def cached_snapshot(engine: Engine, config: Settings, raw: str) -> ChannelSnapshot:
    with Session(engine) as db:
        row = db.get(YouTubeCache, snapshot_key(config, raw))
        if row is None or row.expires_at <= datetime.now(UTC):
            raise YouTubeError("Отчёт устарел. Проверьте канал заново для свежей статистики.")
        return ChannelSnapshot.from_payload(row.payload)


def quota_day(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def purge_cache(engine: Engine) -> None:
    with Session(engine) as db, db.begin():
        db.execute(delete(YouTubeCache).where(YouTubeCache.expires_at <= datetime.now(UTC)))


def reserve(engine: Engine, key: str, limit: int) -> None:
    day = quota_day(datetime.now(UTC))
    with Session(engine) as db, db.begin():
        stmt = insert(RateBucket).values(key=key, started_at=day, count=0)
        db.execute(stmt.on_conflict_do_nothing(index_elements=[RateBucket.key]))
        row = db.get(RateBucket, key, with_for_update=True)
        assert row is not None
        if row.started_at != day:
            row.started_at, row.count = day, 0
        if row.count >= limit:
            raise YouTubeError(
                "Дневной лимит проверок исчерпан. Попробуйте после обновления квоты."
            )
        row.count += 1


def analyze(
    engine: Engine, config: Settings, owner_id: int, bot_id: int, raw: str, *, metered: bool = False
) -> tuple[ChannelSnapshot, bool]:
    reference = channel_reference(raw)
    api_key = config.youtube_api_key.get_secret_value()
    if not api_key:
        raise YouTubeError("YouTube пока не настроен. Владелец должен добавить ключ API.")
    with Session(engine) as db:
        user = db.get(User, owner_id)
        if user is None or user.expert_started_at is None:
            raise ObjectUnavailable
        unlimited = (
            ai_tokens.funding(
                db,
                config,
                owner_id,
                bot_id,
                config.environment != "production",
                {"purpose": "youtube_search"},
            )
            == "unlimited"
        )
        if not unlimited:
            require_access(db, owner_id)
    # Shared budget is deliberately NOT keyed by API key: multiple keys can belong to one project.
    # Keep all project consumers in this DB; external usage is still enforced by Google's quota.
    cache_key = snapshot_key(config, raw)
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        db.execute(delete(YouTubeCache).where(YouTubeCache.expires_at <= now))
        cached = db.get(YouTubeCache, cache_key)
        if cached:
            return ChannelSnapshot.from_payload(cached.payload), True
    # Session-level advisory lock uses its own connection and is always released before pooling.
    lock = int(cache_key[:15], 16)
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock})
        connection.commit()
        if not acquired:
            raise YouTubeError("Этот канал уже проверяется. Попробуйте через минуту.")
        slot: int | None = None
        try:
            # Leave pool capacity for quota reservations, the bot worker and ordinary tools.
            # Fixed PostgreSQL slots also apply across application processes and API keys.
            for candidate in range(731910000000000001, 731910000000000004):
                if connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": candidate}):
                    slot = candidate
                    break
            connection.commit()
            if slot is None:
                raise YouTubeError("Сейчас проверяется несколько каналов. Попробуйте через минуту.")
            with Session(engine) as db:
                cached = db.get(YouTubeCache, cache_key)
                if cached and cached.expires_at > datetime.now(UTC):
                    return ChannelSnapshot.from_payload(cached.payload), True
            if not metered and not unlimited:
                reserve(engine, f"yt:user:{bot_id}:{owner_id}", config.youtube_daily_scans)
            with Session(engine) as db:
                quota_limit = min(
                    config.youtube_daily_units, int(setting(db, "youtube_quota_limit"))
                )
            client = YouTubeClient(api_key, lambda: reserve(engine, "yt:units", quota_limit))
            try:
                snapshot = client.scan(reference, config.youtube_scan_limit)
            except YouTubeError as error:
                if error.quota:
                    with Session(engine) as db, db.begin():
                        row = db.get(RateBucket, "yt:units", with_for_update=True)
                        if row:
                            row.count = config.youtube_daily_units
                raise
            with Session(engine) as db, db.begin():
                db.add(
                    YouTubeCache(
                        key=cache_key,
                        payload=snapshot.payload(),
                        expires_at=datetime.now(UTC) + timedelta(hours=6),
                    )
                )
            return snapshot, False
        finally:
            if slot is not None:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": slot})
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})
            connection.commit()
