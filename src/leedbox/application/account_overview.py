"""Read-only customer view: money, AI credits and core subscription are independent."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application import agents, ai_tokens, credits, wallet
from leedbox.application.entitlements import policy
from leedbox.application.settings import setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AccessGrant, AIAccount, AIOperation, User, WalletEntry


def number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def overview(db: Session, config: Settings, actor: int, bot: int) -> dict[str, Any]:
    user = db.scalar(select(User).where(User.telegram_id == actor))
    if user is None or user.expert_started_at is None:
        raise ObjectUnavailable
    test = agents.test_mode(config)
    funds = db.get(AIAccount, (user.id, bot, test))
    available = ai_tokens.available(db, user.id, bot, test)
    rubles = wallet.balance(db, user.id, bot, config.robokassa_test)
    unlimited = config.is_owner(actor) or bool(funds and funds.unlimited)
    current = policy(db)
    grants = db.scalars(
        select(AccessGrant).where(
            AccessGrant.user_id == user.id, AccessGrant.kind.in_(["paid", "gift"])
        )
    ).all()
    now = datetime.now(UTC)
    active = [g for g in grants if g.starts_at <= now < g.ends_at]
    if current.mode == "free":
        subscription = "Основные функции доступны бесплатно"
    elif active:
        end = max(g.ends_at for g in active).astimezone(ZoneInfo("Europe/Moscow"))
        subscription = f"Подписка активна до {end:%d.%m.%Y %H:%M} МСК"
    else:
        subscription = "Подписка не активирована"
        past = [g.ends_at for g in grants if g.ends_at <= now]
        if past:
            end = max(past).astimezone(ZoneInfo("Europe/Moscow"))
            subscription = f"Подписка истекла {end:%d.%m.%Y}"
    visible = bool(setting(db, "wallet_visible"))
    enabled = visible and bool(setting(db, "wallet_enabled"))
    trials = {
        task: ai_tokens.free_remaining(db, user.id, bot, test, task)
        for task in ai_tokens.TRIAL_LABELS
    }
    tokens = number(available)
    if unlimited:
        tokens += " · безлимитное использование"
    credit = credits.enabled(db)
    label = "AI-кредиты" if credit else "AI-токены"
    summary = f"💳 Рублёвый баланс: {rubles:.2f} ₽\n✨ {label}: {tokens}\n🔑 {subscription}"
    reserved = (funds.credit_reserved if credit else funds.reserved) if funds else 0
    if reserved:
        summary += f"\nВ работе: {number(reserved)} {'кредитов' if credit else 'токенов'} в резерве"
    return {
        "owner": user.id,
        "rubles": rubles,
        "tokens": available,
        "credits": credit,
        "summary": summary,
        "subscription": subscription,
        "trials": trials,
        "unlimited": unlimited,
        "test": config.robokassa_test or test,
        "wallet_enabled": enabled,
        "topup": enabled and bool(setting(db, "wallet_topup_enabled")) and config.robokassa_enabled,
        "packages": enabled and config.robokassa_test == test,
        "subscribe": current.mode == "paid" and current.price > 0 and enabled,
        "stars": current.mode == "paid"
        and config.stars_enabled
        and setting(db, "stars_access_price") > 0,
    }


def history(db: Session, config: Settings, actor: int, bot: int, page: int = 0) -> tuple[str, bool]:
    if type(page) is not int or not 0 <= page <= 100000:
        raise ValueError("Неверная страница истории")
    value = overview(db, config, actor, bot)
    money = db.scalars(
        select(WalletEntry)
        .where(
            WalletEntry.owner_user_id == value["owner"],
            WalletEntry.bot_id == bot,
            WalletEntry.test_mode == config.robokassa_test,
        )
        .order_by(WalletEntry.id.desc())
        .offset(page * 5)
        .limit(6)
    ).all()
    ai = db.scalars(
        select(AIOperation)
        .where(
            AIOperation.owner_user_id == value["owner"],
            AIOperation.bot_id == bot,
            AIOperation.test_mode == agents.test_mode(config),
        )
        .order_by(AIOperation.id.desc())
        .offset(page * 5)
        .limit(6)
    ).all()
    lines = ["📒 Покупки и расход", "", "Рубли"]
    for entry in money[:5]:
        label = (
            "Покупка AI-токенов"
            if entry.snapshot.get("purpose") == "ai_tokens"
            else {"topup": "Пополнение", "purchase": "Оплата доступа", "refund": "Возврат"}.get(
                entry.kind, "Операция"
            )
        )
        when = entry.created_at.astimezone(ZoneInfo("Europe/Moscow"))
        lines.append(f"{when:%d.%m %H:%M} · {label}: {entry.amount:+.2f} ₽")
    if not money:
        lines.append("Операций на этой странице нет.")
    lines += ["", "AI-кредиты" if value.get("credits") else "AI-токены"]
    for row in ai[:5]:
        when = row.created_at.astimezone(ZoneInfo("Europe/Moscow"))
        if row.kind in {"purchase", "grant"}:
            label = "Покупка" if row.kind == "purchase" else "Начисление"
            unit = "кредитов" if row.snapshot.get("unit") == "credits" else "токенов"
            detail = f"+{number(int(row.snapshot.get('tokens', 0)))} {unit}"
        else:
            label = "Генерация" if row.kind == "generation" else "Анализ источника"
            used = row.input_tokens + row.output_tokens
            if row.status == "failed":
                detail = "ошибка, списания нет"
            elif row.status != "completed":
                detail = "выполняется; итог уточняется"
            elif row.billing_mode in {"trial", "unlimited"}:
                detail = f"расход ИИ {number(used)}; с баланса не списано"
            else:
                spent = (
                    row.charged_credits
                    if credits.is_credit(row.snapshot)
                    else used
                    if row.kind == "generation"
                    else row.reserve_tokens
                )
                unit = "кредитов" if credits.is_credit(row.snapshot) else "токенов"
                detail = f"−{number(spent)} {unit}"
        lines.append(f"{when:%d.%m %H:%M} · {label}: {detail}")
    if not ai:
        lines.append("Операций на этой странице нет.")
    if value["test"]:
        lines += ["", "🧪 Тестовые операции. Настоящие деньги не затронуты."]
    return "\n".join(lines), len(money) > 5 or len(ai) > 5
