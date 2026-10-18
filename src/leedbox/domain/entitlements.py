"""Pure access rules. Payments never belong inside individual tools."""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Grant:
    kind: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class AccessDecision:
    state: str
    allowed: bool
    ends_at: datetime | None
    expired_delivery: str
    hide_recommendations: bool = False


def decide_access(
    now: datetime,
    mode: str,
    grants: tuple[Grant, ...],
    grace_days: int = 0,
    expired_delivery: str = "bypass",
    activation_deadline: datetime | None = None,
    hide_for_paid: bool = False,
) -> AccessDecision:
    if mode == "free":
        return AccessDecision("free", True, None, "check")
    if mode != "paid" or grace_days < 0 or expired_delivery not in {"bypass", "check", "deny"}:
        raise ValueError("Invalid access policy")
    active = [grant for grant in grants if grant.starts_at <= now < grant.ends_at]
    if active:
        # A gift/paid grant takes precedence over trial, even when trial ends later.
        selected = max(active, key=lambda grant: (grant.kind != "trial", grant.ends_at))
        state = "trial" if selected.kind == "trial" else "active"
        return AccessDecision(
            state,
            True,
            max(grant.ends_at for grant in active),
            "check",
            hide_for_paid and selected.kind == "paid",
        )
    past = [grant.ends_at for grant in grants if grant.ends_at <= now]
    deadline = max(past) + timedelta(days=grace_days) if past else None
    deadlines = [value for value in (deadline, activation_deadline) if value is not None]
    if deadlines and now < max(deadlines):
        return AccessDecision("grace", True, max(deadlines), "check")
    return AccessDecision("expired", False, max(past) if past else None, expired_delivery)
