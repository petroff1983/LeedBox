from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.versions import fingerprint, record_version
from leedbox.domain.entitlements import AccessDecision, Grant, decide_access
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AccessGrant, AccessPolicy, AuditLog, User


def policy(session: Session) -> AccessPolicy:
    value = session.get(AccessPolicy, 1)
    # Absent configuration always means free, never accidental charging.
    return value or AccessPolicy(
        id=1,
        mode="free",
        price=Decimal("0"),
        currency="RUB",
        period_days=30,
        trial_days=90,
        grace_days=0,
        activation_grace_days=0,
        expired_delivery="bypass",
        hide_recommendations_for_paid=False,
    )


def start_expert(session: Session, user: User, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    locked = session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked is None:
        raise ValueError("Unknown user")
    if locked.expert_started_at is not None:
        return
    locked.expert_started_at = now
    session.add(
        AccessGrant(
            user_id=user.id,
            kind="trial",
            starts_at=now,
            ends_at=now + timedelta(days=policy(session).trial_days),
            source="first_expert_menu",
            action_key=f"trial:{user.id}",
        )
    )
    session.flush()


def access_for(session: Session, user_id: int, now: datetime | None = None) -> AccessDecision:
    now = now or datetime.now(UTC)
    config = policy(session)
    grants = tuple(
        Grant(row.kind, row.starts_at, row.ends_at)
        for row in session.scalars(select(AccessGrant).where(AccessGrant.user_id == user_id))
    )
    user = session.get(User, user_id)
    activation_deadline = None
    if (
        user
        and user.expert_started_at
        and config.paid_enabled_at
        and user.expert_started_at < config.paid_enabled_at
    ):
        activation_deadline = config.paid_enabled_at + timedelta(days=config.activation_grace_days)
    return decide_access(
        now,
        config.mode,
        grants,
        config.grace_days,
        config.expired_delivery,
        activation_deadline,
        config.hide_recommendations_for_paid,
    )


def require_access(session: Session, user_id: int) -> None:
    if not access_for(session, user_id).allowed:
        raise ValueError("Доступ закончился. Откройте «Мой доступ» в профиле для продления")


def can_check_subscriptions(session: Session, user_id: int, now: datetime | None = None) -> bool:
    """Trial tools/AI tokens do not purchase the core subscription check service."""
    if policy(session).mode == "free":
        return True  # Explicit owner switch: the whole service is currently free.
    now = now or datetime.now(UTC)
    return (
        session.scalar(
            select(AccessGrant.id)
            .where(
                AccessGrant.user_id == user_id,
                AccessGrant.kind.in_(["paid", "gift"]),
                AccessGrant.starts_at <= now,
                AccessGrant.ends_at > now,
            )
            .limit(1)
        )
        is not None
    )


def configure_policy(
    session: Session,
    settings: Settings,
    actor_id: int,
    *,
    mode: str,
    price: Decimal,
    currency: str,
    period_days: int,
    trial_days: int,
    grace_days: int,
    activation_grace_days: int,
    expired_delivery: str,
    hide_for_paid: bool = False,
    now: datetime | None = None,
    expected: str | None = None,
) -> None:
    if not settings.is_owner(actor_id):
        raise PermissionError("Owner access required")
    if (
        mode not in {"free", "paid"}
        or not price.is_finite()
        or price < 0
        or not isinstance(price.as_tuple().exponent, int)
        or int(price.as_tuple().exponent) < -4
        or price >= Decimal("1e16")
        or not currency.isascii()
        or not currency.isalpha()
        or len(currency) > 8
        or period_days <= 0
        or min(trial_days, grace_days, activation_grace_days) < 0
        or max(period_days, trial_days, grace_days, activation_grace_days) > 365000
        or expired_delivery not in {"bypass", "check", "deny"}
    ):
        raise ValueError("Invalid access policy")
    now = now or datetime.now(UTC)
    session.execute(insert(AccessPolicy).values(id=1).on_conflict_do_nothing())
    config = session.scalar(select(AccessPolicy).where(AccessPolicy.id == 1).with_for_update())
    assert config is not None
    before = policy_data(config)
    if expected is not None and fingerprint(before) != expected:
        raise ValueError("Политика доступа изменилась. Откройте форму заново")
    previous = config.mode
    if mode == "paid" and config.mode != "paid":
        config.paid_enabled_at = now
    config.mode, config.price, config.currency = mode, price, currency.upper()
    config.period_days, config.trial_days, config.grace_days = period_days, trial_days, grace_days
    config.activation_grace_days, config.expired_delivery = activation_grace_days, expired_delivery
    config.hide_recommendations_for_paid = hide_for_paid
    session.add(
        AuditLog(
            actor_telegram_id=actor_id,
            action="access_policy_changed",
            details={
                "previous_mode": previous,
                "mode": mode,
                "price": str(price),
                "currency": currency.upper(),
                "period_days": period_days,
                "trial_days": trial_days,
                "grace_days": grace_days,
                "activation_grace_days": activation_grace_days,
                "expired_delivery": expired_delivery,
                "hide_for_paid": hide_for_paid,
            },
        )
    )
    session.flush()
    record_version(session, "policy", "1", actor_id, policy_data(config), before)


def extend_access(
    session: Session,
    settings: Settings,
    actor_id: int,
    user_id: int,
    days: int,
    action_key: str,
    now: datetime | None = None,
) -> AccessGrant:
    if not settings.is_owner(actor_id):
        raise PermissionError("Owner access required")
    if not 0 < days <= 365000 or not action_key or len(action_key) > 100:
        raise ValueError("Invalid grant")
    now = now or datetime.now(UTC)
    user = session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise ValueError("Unknown user")
    existing = session.scalar(select(AccessGrant).where(AccessGrant.action_key == action_key))
    if existing:
        if existing.user_id != user_id or existing.kind != "gift":
            raise ValueError("Action key already used")
        return existing
    grants = session.scalars(select(AccessGrant).where(AccessGrant.user_id == user_id)).all()
    # Extend the continuous active interval; an unrelated future grant must not bridge a gap.
    base = now
    for row in sorted(grants, key=lambda row: row.starts_at):
        if row.starts_at <= base and row.ends_at > base:
            base = row.ends_at
    value = AccessGrant(
        user_id=user_id,
        kind="gift",
        starts_at=now,
        ends_at=base + timedelta(days=days),
        source="owner_extension",
        action_key=action_key,
    )
    session.add(value)
    session.add(
        AuditLog(
            actor_telegram_id=actor_id,
            action="access_extended",
            subject_user_id=user_id,
            details={"days": days, "ends_at": value.ends_at.isoformat()},
        )
    )
    session.flush()
    return value


def policy_data(value: AccessPolicy) -> dict[str, str | int | bool]:
    return {
        "mode": value.mode,
        "price": format(value.price.normalize(), "f"),
        "currency": value.currency,
        "period_days": value.period_days,
        "trial_days": value.trial_days,
        "grace_days": value.grace_days,
        "activation_grace_days": value.activation_grace_days,
        "expired_delivery": value.expired_delivery,
        "hide_for_paid": value.hide_recommendations_for_paid,
    }


def adjust_grant_end(
    session: Session,
    settings: Settings,
    actor: int,
    grant_id: int,
    expected_end: datetime,
    end: datetime,
) -> None:
    if not settings.is_owner(actor):
        raise PermissionError("Owner access required")
    row = session.scalar(select(AccessGrant).where(AccessGrant.id == grant_id).with_for_update())
    if row is None:
        raise ValueError("Доступ не найден")
    if end.tzinfo is None or expected_end.tzinfo is None or end < row.starts_at:
        raise ValueError("Окончание не может быть раньше начала; время задаётся в UTC")
    if row.ends_at == end:
        return
    if row.ends_at != expected_end:
        raise ValueError("Срок изменился. Откройте карточку заново")
    before = row.ends_at
    row.ends_at = end
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="access_end_changed",
            subject_user_id=row.user_id,
            details={"grant_id": row.id, "before": before.isoformat(), "after": end.isoformat()},
        )
    )
