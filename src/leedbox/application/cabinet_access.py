"""Expert-only web access. Raw credentials are never persisted or logged."""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import CabinetCredential, User

LOGIN_SECONDS = 300
SESSION_SECONDS = 28800


def available(settings: Settings) -> bool:
    secret = settings.web_secret_key.get_secret_value()
    return (
        len(secret) >= 32
        and len(set(secret)) >= 12
        and "replace" not in secret.lower()
        and secret != settings.bot_token.get_secret_value()
        and bool(
            re.fullmatch(
                r"[1-9][0-9]{0,18}:[A-Za-z0-9_-]{20,}", settings.bot_token.get_secret_value()
            )
        )
        and (
            settings.environment != "production"
            or bool(settings.public_base_url and settings.public_base_url.startswith("https://"))
        )
    )


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bot_id(settings: Settings) -> int:
    return int(settings.bot_token.get_secret_value().split(":")[0])


def issue_ticket(session: Session, settings: Settings, telegram_id: int) -> str:
    if not available(settings):
        raise ValueError("Кабинет пока не настроен владельцем сервиса")
    user = session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
    if not user or not user.expert_started_at:
        raise ObjectUnavailable
    now = datetime.now(UTC)
    session.execute(
        delete(CabinetCredential).where(
            CabinetCredential.owner_user_id == user.id,
            CabinetCredential.bot_id == bot_id(settings),
            (CabinetCredential.kind == "login") | (CabinetCredential.expires_at <= now),
        )
    )
    token = token_urlsafe(32)
    session.add(
        CabinetCredential(
            token_hash=digest(token),
            owner_user_id=user.id,
            bot_id=bot_id(settings),
            kind="login",
            expires_at=now + timedelta(seconds=LOGIN_SECONDS),
        )
    )
    # A fragment is not transmitted to HTTP/access logs or in Referer headers.
    base = settings.public_base_url or f"http://localhost:{settings.web_port}"
    parsed = urlsplit(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/cabinet/login#token={token}"


def consume_ticket(session: Session, settings: Settings, token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ObjectUnavailable
    now = datetime.now(UTC)
    row = session.scalar(
        select(CabinetCredential)
        .join(User, User.id == CabinetCredential.owner_user_id)
        .where(
            CabinetCredential.token_hash == digest(token),
            CabinetCredential.kind == "login",
            CabinetCredential.bot_id == bot_id(settings),
            CabinetCredential.expires_at > now,
            User.expert_started_at.is_not(None),
        )
        .with_for_update(of=CabinetCredential)
    )
    if not row:
        raise ObjectUnavailable
    owner = row.owner_user_id
    session.delete(row)
    raw = token_urlsafe(32)
    session.add(
        CabinetCredential(
            token_hash=digest(raw),
            owner_user_id=owner,
            bot_id=bot_id(settings),
            kind="session",
            expires_at=now + timedelta(seconds=SESSION_SECONDS),
        )
    )
    return raw


def session_owner(session: Session, settings: Settings, token: str) -> int | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    return session.scalar(
        select(CabinetCredential.owner_user_id)
        .join(User, User.id == CabinetCredential.owner_user_id)
        .where(
            CabinetCredential.token_hash == digest(token),
            CabinetCredential.kind == "session",
            CabinetCredential.bot_id == bot_id(settings),
            CabinetCredential.expires_at > datetime.now(UTC),
            User.expert_started_at.is_not(None),
        )
    )
