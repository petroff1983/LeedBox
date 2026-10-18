"""Owner-only provider snapshots. Never present local estimates as provider balances."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.limits import take
from leedbox.application.provider_balance import notify
from leedbox.application.settings import setting
from leedbox.application.youtube import quota_day
from leedbox.infrastructure import apify
from leedbox.infrastructure.agents import AgentError, request_json
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import RateBucket, Setting


def zenstat_packages(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("status") is not True:
        raise ValueError("No confirmed provider packages")
    rows = data["response"]["packages"]
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Invalid packages")
    result = []
    for row in rows:
        if type(row["balance"]) is not int or row["balance"] < 0:
            raise ValueError("Invalid package balance")
        stamp = datetime.fromisoformat(row["date_end"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or not isinstance(row["type"], str):
            raise ValueError("Invalid expiry")
        result.append(
            {"balance": row["balance"], "type": row["type"][:40], "date_end": stamp.isoformat()}
        )
    return result


def apify_limits(data: dict[str, Any]) -> dict[str, str]:
    data = data["data"]
    limit = Decimal(str(data["limits"]["maxMonthlyUsageUsd"]))
    used = Decimal(str(data["current"]["monthlyUsageUsd"]))
    if not all(v.is_finite() and v >= 0 for v in (limit, used)):
        raise ValueError("Invalid provider limits")
    return {
        "limit_usd": str(limit),
        "used_usd": str(used),
        "remaining_usd": str(max(Decimal(0), limit - used)),
        "cycle_end": str(data.get("monthlyUsageCycle", {}).get("endAt", ""))[:50],
    }


def refresh(engine: Engine, config: Settings, bot: int, *, manual: bool = False) -> None:
    for provider, key in (
        ("zenstat", config.zenstat_api_key.get_secret_value()),
        ("apify", config.apify_api_token.get_secret_value()),
    ):
        if not key:
            continue
        with Session(engine) as db, db.begin():
            seconds = (
                3600 * int(setting(db, "zenstat_balance_check_hours"))
                if provider == "zenstat"
                else 3600
            )
            if not take(db, f"provider:limits:{bot}:{provider}", 1, 300 if manual else seconds):
                continue
        stamp = datetime.now(UTC).isoformat()
        status: dict[str, Any] = {"checked_at": stamp}
        try:
            if provider == "zenstat":
                status["packages"] = zenstat_packages(
                    request_json(
                        "https://partner.zenstat.ru/api/v1/profile/packages/",
                        {"Authorization": key},
                    )
                )
            else:
                status.update(apify_limits(apify.request(key, "users/me/limits")))
        except (AgentError, ValueError, KeyError, TypeError, InvalidOperation, AttributeError):
            status["error"] = "provider_unavailable"
        with Session(engine) as db, db.begin():
            name = f"provider_limits:{bot}:{provider}"
            old = db.get(Setting, name)
            if status.get("error") and old:
                status = {**old.value, "error": "provider_unavailable", "attempted_at": stamp}
            db.execute(
                insert(Setting)
                .values(key=name, value=status)
                .on_conflict_do_update(index_elements=[Setting.key], set_={"value": status})
            )
            if provider == "zenstat" and not status.get("error"):
                common = [
                    p
                    for p in status["packages"]
                    if p["type"] == "common"
                    and datetime.fromisoformat(p["date_end"]) > datetime.now(UTC)
                ]
                remaining = sum(p["balance"] for p in common)
                if remaining <= int(setting(db, "zenstat_low_requests")):
                    notify(
                        db,
                        config,
                        bot,
                        "zenstat_low",
                        f"Zenstat: осталось {remaining} запросов. Проверьте пакет API.",
                    )


def youtube_usage(db: Session, config: Settings) -> dict[str, Any]:
    day = quota_day(datetime.now(UTC))
    row = db.get(RateBucket, "yt:units")
    used = row.count if row and row.started_at == day else 0
    limit = min(config.youtube_daily_units, int(setting(db, "youtube_quota_limit")))
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "day": day.isoformat(),
        "source": "local_estimate",
    }
