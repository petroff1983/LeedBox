"""Read-only balance monitoring; owner-only, deduplicated outbox alerts."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.limits import take
from leedbox.application.settings import setting
from leedbox.infrastructure.agents import AgentError, request_json
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import OutboxMessage, Setting


def notify(db: Session, config: Settings, bot: int, category: str, text: str) -> None:
    if not take(db, f"provider:alert:{bot}:{category}", 1, 21600):
        return
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    for telegram in config.owner_ids:
        db.add(
            OutboxMessage(
                bot_id=bot,
                telegram_user_id=telegram,
                purpose="provider_alert",
                text=text,
                dedupe_key=f"provider:{bot}:{category}:{stamp}:{telegram}",
            )
        )


def check(engine: Engine, config: Settings, bot: int) -> None:
    key = config.deepseek_api_key.get_secret_value()
    if not key:
        return
    with Session(engine) as db, db.begin():
        if not take(db, f"provider:check:{bot}", 1, 3600):
            return
        thresholds = {
            currency: Decimal(str(setting(db, "ai_provider_low_" + currency.lower())))
            for currency in ("USD", "CNY")
        }
    status: dict[str, Any] = {"checked_at": datetime.now(UTC).isoformat(), "balances": {}}
    try:
        data = request_json(
            "https://api.deepseek.com/user/balance", {"Authorization": "Bearer " + key}
        )
        if type(data.get("is_available")) is not bool or not isinstance(
            data.get("balance_infos"), list
        ):
            raise ValueError
        balances = {}
        for item in data["balance_infos"]:
            currency = item["currency"]
            amount = Decimal(item["total_balance"])
            if currency not in thresholds or not amount.is_finite():
                raise ValueError
            balances[currency] = str(amount)
        if not balances:
            raise ValueError
        status.update(available=data["is_available"], balances=balances)
        low = not data["is_available"] or all(
            Decimal(amount) <= thresholds[currency] for currency, amount in balances.items()
        )
        message = "DeepSeek: пора пополнить баланс API. Остаток: " + ", ".join(
            f"{amount} {currency}" for currency, amount in balances.items()
        )
    except (AgentError, ValueError, KeyError, TypeError, InvalidOperation):
        status.update(error="balance_unavailable")
        low, message = (
            True,
            "Не удалось проверить баланс DeepSeek. Проверьте API-ключ и кабинет провайдера.",
        )
    with Session(engine) as db, db.begin():
        db.execute(
            insert(Setting)
            .values(key=f"provider_status:{bot}", value=status)
            .on_conflict_do_update(index_elements=[Setting.key], set_={"value": status})
        )
        if low:
            notify(db, config, bot, "balance", message)
