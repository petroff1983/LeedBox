"""Publication gates, privacy-preserving redirects and session-limited recommendations."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import access_for
from leedbox.application.partners import PartnerInput
from leedbox.application.services import owner_required
from leedbox.application.settings import product_event, setting
from leedbox.application.versions import fingerprint, lock_content
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import http_url
from leedbox.infrastructure.models import (
    Article,
    ArticleCategory,
    AuditLog,
    Partner,
    Recommendation,
    RecommendationEvent,
    RecommendationPreference,
    ServiceRedirect,
    User,
)


def partner_public(row: Partner | None) -> bool:
    if row is None or not row.enabled:
        return False
    try:
        PartnerInput(
            **{key: getattr(row, key) for key in PartnerInput.__dataclass_fields__}
        ).validated()
    except (ValueError, TypeError):
        return False
    return True


def partner_text(row: Partner) -> str:
    body = "Партнёр: " + row.name + "\n" + row.description
    if all((row.advertiser, row.inn, row.erid, row.marking_text)):
        body += (
            f"\n\nРеклама. {row.marking_text}\nРекламодатель: {row.advertiser}"
            f"\nИНН {row.inn}\nerid: {row.erid}"
        )
    else:
        body += "\n\nОснование отсутствия маркировки: " + row.exception_reason
    return body


def preference(
    session: Session, user_id: int, bot_id: int, now: datetime | None = None
) -> RecommendationPreference:
    now = now or datetime.now(UTC)
    if session.get(User, user_id) is None:
        raise ObjectUnavailable
    lock_content(session, "recommendation_session", f"{user_id}:{bot_id}")
    row = session.scalar(
        select(RecommendationPreference)
        .where(
            RecommendationPreference.owner_user_id == user_id,
            RecommendationPreference.bot_id == bot_id,
        )
        .with_for_update()
    )
    if row is None:
        row = RecommendationPreference(
            owner_user_id=user_id,
            bot_id=bot_id,
            session_key=uuid4().hex,
            last_activity=now,
            opted_out=False,
            claimed=False,
        )
        session.add(row)
    elif now - row.last_activity >= timedelta(minutes=30):
        row.session_key, row.claimed = uuid4().hex, False
    row.last_activity = now
    session.flush()
    return row


def redirect_code(session: Session, bot_id: int, kind: str, entity_id: int) -> str:
    if kind not in {"article", "partner"}:
        raise ValueError("Unknown redirect")
    lock_content(session, "service_redirect", f"{bot_id}:{kind}:{entity_id}")
    row = session.scalar(
        select(ServiceRedirect).where(
            ServiceRedirect.bot_id == bot_id,
            ServiceRedirect.kind == kind,
            ServiceRedirect.entity_id == entity_id,
        )
    )
    if row is None:
        row = ServiceRedirect(
            code=("a" if kind == "article" else "p") + uuid4().hex[:12],
            bot_id=bot_id,
            kind=kind,
            entity_id=entity_id,
        )
        session.add(row)
        session.flush()
    return row.code


def claim_recommendation(
    session: Session, user_id: int, bot_id: int, tool: str, attached_ids: list[int] | None = None
) -> dict[str, Any] | None:
    pref = preference(session, user_id, bot_id)
    if pref.claimed or pref.opted_out or not setting(session, "recommendations_enabled"):
        return None
    if access_for(session, user_id).hide_recommendations:
        return None
    ids = attached_ids
    if ids is None:
        ids = []
        for row in session.scalars(
            select(Recommendation)
            .where(Recommendation.enabled.is_(True))
            .order_by(Recommendation.id)
        ):
            if tool in row.payload["tools"]:
                ids += row.payload["partner_ids"]
    for id in ids:
        partner = session.get(Partner, id)
        if not partner_public(partner):
            continue
        assert partner is not None
        pref.claimed = True
        return {
            "partner_id": id,
            "body": partner_text(partner),
            "code": redirect_code(session, bot_id, "partner", id),
            "session_key": pref.session_key,
        }
    return None


def shown(session: Session, user_id: int, bot_id: int, item: dict[str, Any]) -> None:
    key = f"show:{bot_id}:{user_id}:{item['session_key']}"
    lock_content(session, "recommendation_shown", key)
    if session.scalar(select(RecommendationEvent.id).where(RecommendationEvent.event_key == key)):
        return
    session.add(
        RecommendationEvent(
            bot_id=bot_id,
            owner_user_id=user_id,
            partner_id=item["partner_id"],
            kind="shown",
            event_key=key,
        )
    )
    user = session.get(User, user_id)
    product_event(
        session,
        bot_id,
        user.telegram_id if user else None,
        "recommendation_shown",
        partner_id=item["partner_id"],
    )


def article_public(session: Session, row: Article | None) -> bool:
    return bool(
        row
        and row.enabled
        and session.scalar(
            select(ArticleCategory.id).where(
                ArticleCategory.slug == row.payload["category"], ArticleCategory.enabled.is_(True)
            )
        )
    )


def resolve_redirect(session: Session, code: str, clicked: bool) -> str:
    row = session.get(ServiceRedirect, code)
    if row is None:
        raise ObjectUnavailable
    if row.kind == "partner":
        partner = session.get(Partner, row.entity_id)
        if not setting(session, "recommendations_enabled") or not partner_public(partner):
            raise ObjectUnavailable
        assert partner is not None
        target = partner.url
        if clicked:
            session.add(
                RecommendationEvent(bot_id=row.bot_id, partner_id=partner.id, kind="clicked")
            )
            product_event(
                session, row.bot_id, None, "recommendation_clicked", partner_id=partner.id
            )
    elif row.kind == "article":
        article = session.get(Article, row.entity_id)
        if not article_public(session, article):
            raise ObjectUnavailable
        assert article is not None
        target = article.payload["url"]
        if clicked:
            product_event(session, row.bot_id, None, "article_opened", article_id=article.id)
    else:
        raise ObjectUnavailable
    return http_url(target)


def manual_conversion(
    session: Session, config: Any, actor: int, bot_id: int, partner_id: int, action_key: str
) -> None:
    owner_required(config, actor)
    if len(action_key) != 32 or any(c not in "0123456789abcdef" for c in action_key):
        raise ValueError("Откройте новую форму")
    if not 0 < partner_id <= 2**63 - 1 or session.get(Partner, partner_id) is None:
        raise ObjectUnavailable
    key = fingerprint([actor, bot_id, partner_id, action_key])
    lock_content(session, "manual_conversion", key)
    if session.scalar(select(RecommendationEvent.id).where(RecommendationEvent.event_key == key)):
        return
    session.add(
        RecommendationEvent(bot_id=bot_id, partner_id=partner_id, kind="conversion", event_key=key)
    )
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="partner_conversion_added",
            details={"partner_id": partner_id, "bot_id": bot_id},
        )
    )
