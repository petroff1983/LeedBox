"""Account locking, exact provider usage, explicit unknown-outcome reservations."""

from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application import credits
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIAccount, AIOperation, User

TRIAL_LABELS = {
    "dzen": "Дзен: заголовки",
    "image_prompts": "Картинки",
    "lead_magnet": "Темы лид-магнитов",
    "content_search": "Анализ конкурентов Telegram",
    "youtube_search": "Анализ YouTube",
}


def purpose(snapshot: dict[str, Any]) -> str:
    return str(snapshot.get("purpose") or "dzen")


def free_remaining(db: Session, owner: int, bot: int, test: bool, task: str) -> int:
    from leedbox.application.settings import setting

    if task not in {*TRIAL_LABELS, "dzen_search"}:
        return 0
    row = db.get(AIAccount, (owner, bot, test))
    quota = "dzen" if task == "dzen_search" else task
    limit = (row.free_limits if row else {}).get(quota, setting(db, "ai_free_" + quota))
    used = (
        db.scalar(
            select(func.count())
            .select_from(AIOperation)
            .where(
                AIOperation.owner_user_id == owner,
                AIOperation.bot_id == bot,
                AIOperation.test_mode == test,
                AIOperation.billing_mode == "trial",
                AIOperation.status != "failed",
                func.coalesce(AIOperation.snapshot["purpose"].astext, "dzen") == task,
            )
        )
        or 0
    )
    return max(0, int(limit) - used)


def funding(
    db: Session, config: Settings, owner: int, bot: int, test: bool, snapshot: dict[str, Any]
) -> str:
    user = db.get(User, owner)
    if user is None or user.expert_started_at is None:
        raise ObjectUnavailable
    row = db.get(AIAccount, (owner, bot, test))
    if config.is_owner(user.telegram_id) or (row and row.unlimited):
        return "unlimited"
    if free_remaining(db, owner, bot, test, purpose(snapshot)):
        return "trial"
    return "balance"


def release_reservation(funds: AIAccount, row: AIOperation) -> None:
    if row.billing_mode == "balance":
        if credits.is_credit(row.snapshot):
            funds.credit_reserved -= credits.amount(row.snapshot)
        else:
            funds.reserved -= row.reserve_tokens


def receipt(row: AIOperation, session_total: int | None = None) -> str:
    used = row.input_tokens + row.output_tokens
    total = used if session_total is None else session_total
    usage = f"Потрачено: {used} токенов за этот шаг.\nВсего за сессию: {total} токенов.\n"
    usage += f"Вход: {row.input_tokens}, выход: {row.output_tokens}.\n"
    if row.billing_mode == "unlimited":
        return usage + "♾ Безлимитный доступ. Баланс не списывается."
    if row.billing_mode == "trial":
        return usage + "🎁 Бесплатная попытка. Баланс не списывается."
    if row.status == "failed":
        if credits.is_credit(row.snapshot):
            return usage + "За неудачный шаг кредиты не списаны."
        return usage + "За неудачный шаг токены с баланса не списаны."
    if row.status != "completed":
        return usage + "Расход последнего запроса уточняется; резерв на проверке."
    if credits.is_credit(row.snapshot):
        return (
            usage
            + f"Списано: {row.charged_credits} кредитов. Остаток: {row.balance_after} кредитов."
        )
    return usage + (
        f"Осталось: {row.balance_after}."
        + (
            "\nБаланс исчерпан. Пополните его для следующей генерации."
            if row.balance_after == 0
            else ""
        )
    )


def account(db: Session, owner: int, bot: int, test: bool) -> AIAccount:
    # Every operation locks User first, then account, then operation.
    # The shared barrier prevents a one-time unit conversion racing with reservations.
    db.execute(text("SELECT pg_advisory_xact_lock_shared(731910000000000020)"))
    user = db.scalar(select(User).where(User.id == owner).with_for_update())
    if user is None or user.expert_started_at is None:
        raise ObjectUnavailable
    db.execute(
        insert(AIAccount)
        .values(owner_user_id=owner, bot_id=bot, test_mode=test, balance=0, reserved=0)
        .on_conflict_do_nothing()
    )
    row = db.get(AIAccount, (owner, bot, test), with_for_update=True)
    assert row is not None
    return row


def available(db: Session, owner: int, bot: int, test: bool) -> int:
    row = db.get(AIAccount, (owner, bot, test))
    return credits.available(row, credits.enabled(db)) if row else 0


def balance_after(funds: AIAccount, row: AIOperation) -> int:
    return credits.available(funds, credits.is_credit(row.snapshot))


def charge(funds: AIAccount, row: AIOperation, legacy_amount: int) -> None:
    credits.charge(funds, row, legacy_amount)


def previous(db: Session, owner: int, bot: int, test: bool, key: str) -> AIOperation | None:
    row = db.scalar(
        select(AIOperation).where(AIOperation.bot_id == bot, AIOperation.action_key == key)
    )
    if row and (row.owner_user_id != owner or row.test_mode != test):
        raise ObjectUnavailable
    return row


def reserve(
    db: Session,
    owner: int,
    bot: int,
    test: bool,
    key: str,
    maximum: int,
    snapshot: dict[str, Any],
    billing_mode: str = "balance",
    kind: str = "generation",
) -> AIOperation:
    # Variable-size content batches have a separately bounded two-call quote.
    ceiling = (
        1000000
        if kind == "search"
        else 250000
        if snapshot.get("purpose") == "content" and snapshot.get("version") == 2
        else 100000
    )
    if not 1 <= maximum <= ceiling or not 1 <= len(key) <= 100:
        raise ValueError("Некорректный резерв")
    funds = account(db, owner, bot, test)
    old = previous(db, owner, bot, test, key)
    if old:
        if old.snapshot != snapshot or old.kind != kind:
            raise ValueError("Подтверждение относится к другой операции")
        return old
    if billing_mode not in {"balance", "trial", "unlimited"}:
        raise ValueError("Некорректный способ списания")
    if kind not in {"generation", "search"}:
        raise ValueError("Некорректный тип операции")
    credit = credits.is_credit(snapshot)
    if credit != credits.enabled(db):
        raise ValueError("Единицы оплаты изменились. Подтвердите действие заново")
    required = credits.amount(snapshot) if credit else maximum
    if not 1 <= required <= 1000000:
        raise ValueError("Некорректная стоимость действия")
    if billing_mode == "balance" and credit:
        credits.ensure_margin(db, snapshot)
    if billing_mode == "balance" and credits.available(funds, credit) < required:
        if credit:
            raise ValueError(f"Недостаточно кредитов: нужно {required}. Пополните баланс.")
        raise ValueError(f"Недостаточно токенов для резерва {maximum}. Пополните баланс.")
    if billing_mode == "balance":
        if credit:
            funds.credit_reserved += required
        else:
            funds.reserved += maximum
    row = AIOperation(
        owner_user_id=owner,
        bot_id=bot,
        test_mode=test,
        action_key=key,
        kind=kind,
        status="reserved",
        model=snapshot["model"],
        reserve_tokens=maximum,
        billing_mode=billing_mode,
        snapshot=snapshot,
    )
    db.add(row)
    db.flush()
    return row


def usage_counts(usage: Any, maximum: int) -> tuple[int, int]:
    if not isinstance(usage, dict):
        raise ValueError("Нет подтверждённого расхода API")
    counts = [usage.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if any(type(count) is not int or count < 0 for count in counts):
        raise ValueError("Некорректный расход API")
    incoming, outgoing, total = cast(tuple[int, int, int], tuple(counts))
    if total != incoming + outgoing or total > maximum or total <= 0:
        raise ValueError("Расход не соответствует резерву")
    return incoming, outgoing


def settle(
    db: Session,
    owner: int,
    bot: int,
    test: bool,
    number: int,
    usage: dict[str, Any],
    result: dict[str, Any] | None,
    valid: bool,
) -> AIOperation:
    funds = account(db, owner, bot, test)
    row = db.get(AIOperation, number, with_for_update=True)
    if row is None or (row.owner_user_id, row.bot_id, row.test_mode) != (owner, bot, test):
        raise ObjectUnavailable
    if row.status in {"completed", "failed"}:
        return row
    incoming, outgoing = usage_counts(usage, row.reserve_tokens)
    release_reservation(funds, row)
    # A malformed result is paid by the service, not the customer; actual usage is retained.
    if valid and row.billing_mode == "balance":
        charge(funds, row, incoming + outgoing)
    row.input_tokens, row.output_tokens = incoming, outgoing
    rates = row.snapshot["rates"]
    row.cost_usd = (
        Decimal(incoming) * Decimal(rates["input"]) + Decimal(outgoing) * Decimal(rates["output"])
    ) / Decimal(1000000)
    row.cost_rub = row.cost_usd * Decimal(rates["usd_rub"])
    row.status, row.result = ("completed", result) if valid else ("failed", None)
    row.balance_after = balance_after(funds, row)
    return row


def grant(
    db: Session,
    owner: int,
    bot: int,
    test: bool,
    key: str,
    tokens: int,
    kind: str = "grant",
    snapshot: dict[str, Any] | None = None,
) -> AIOperation:
    if type(tokens) is not int or not 1 <= tokens <= 100000000 or kind not in {"grant", "purchase"}:
        raise ValueError("Некорректное пополнение токенов")
    funds = account(db, owner, bot, test)
    old = previous(db, owner, bot, test, key)
    if old:
        if old.kind != kind or old.snapshot != {**(snapshot or {}), "tokens": tokens}:
            raise ValueError("Пополнение изменилось")
        return old
    credit = (snapshot or {}).get("unit") == "credits"
    if credit:
        funds.credit_balance += tokens
        if kind == "purchase":
            funds.credit_value_rub += Decimal(str((snapshot or {})["offer"]["price_rub"]))
    else:
        funds.balance += tokens
    row = AIOperation(
        owner_user_id=owner,
        bot_id=bot,
        test_mode=test,
        action_key=key,
        kind=kind,
        status="completed",
        model="",
        snapshot={**(snapshot or {}), "tokens": tokens},
        balance_after=credits.available(funds, credit),
    )
    db.add(row)
    db.flush()
    return row
