"""Personal lifetime trial and confirmed fixed-token price for public YouTube analysis."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application import ai_tokens, credits
from leedbox.application.agents import operation, test_mode
from leedbox.application.sections import visible_sections
from leedbox.application.settings import setting
from leedbox.application.youtube import snapshot_key
from leedbox.domain.youtube import ChannelSnapshot, channel_reference
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIOperation, WalletAccount
from leedbox.infrastructure.youtube import YouTubeError


@credits.quoted
def quote(db: Session, config: Settings, owner: int, bot: int, raw: str) -> dict[str, Any]:
    parsed = channel_reference(raw)
    reference = (
        f"https://www.youtube.com/user/{parsed.value}"
        if parsed.parameter == "forUsername"
        else parsed.value
    )
    key = snapshot_key(config, raw)
    saved = db.scalar(
        select(AIOperation)
        .where(
            AIOperation.owner_user_id == owner,
            AIOperation.bot_id == bot,
            AIOperation.test_mode == test_mode(config),
            AIOperation.status == "completed",
            AIOperation.snapshot["purpose"].astext == "youtube_search",
            AIOperation.snapshot["cache_key"].astext == key,
            AIOperation.created_at > datetime.now(UTC) - timedelta(hours=6),
        )
        .order_by(AIOperation.id.desc())
        .limit(1)
    )
    return {
        "purpose": "youtube_search",
        "model": "youtube",
        "source": reference,
        "cache_key": key,
        "maximum": int(setting(db, "ai_youtube_search_tokens")),
        "reuse_id": saved.id if saved else None,
    }


def execute(
    engine: Engine,
    config: Settings,
    owner: int,
    bot: int,
    key: str,
    offer: dict[str, Any],
    fetch: Callable[[Engine, Settings, int, int, str], tuple[ChannelSnapshot, bool]],
) -> int:
    test = test_mode(config)
    with Session(engine) as db, db.begin():
        from leedbox.application.legal import require_expert

        require_expert(db, config, owner, bot)
        ai_tokens.account(db, owner, bot, test)
        if "youtube" not in visible_sections(db) or "agents" not in visible_sections(db):
            raise ValueError("Анализ YouTube выключен владельцем")
        wallet = db.get(WalletAccount, (owner, bot, test))
        if wallet and wallet.balance < 0:
            raise ValueError("После возврата платежа есть задолженность. Обратитесь к поддержке")
        current = quote(db, config, owner, bot, offer["source"])
        if current["reuse_id"]:
            return int(current["reuse_id"])
        old = ai_tokens.previous(db, owner, bot, test, key)
        if old:
            if old.snapshot != offer:
                raise ValueError("Подтверждение относится к другому анализу")
            if old.status == "completed":
                return old.id
            raise ValueError(
                "Анализ уже запускался. Откройте новую проверку без повторного списания."
            )
        if {k: v for k, v in offer.items() if k != "confirmed_billing"} != current:
            raise ValueError("Условия анализа изменились. Выберите период заново.")
        mode = ai_tokens.funding(db, config, owner, bot, test, current)
        if offer.get("confirmed_billing") != mode:
            raise ValueError(
                "Способ оплаты изменился. Выберите период и подтвердите условия заново."
            )
        op = ai_tokens.reserve(db, owner, bot, test, key, current["maximum"], offer, mode, "search")
        op.status = "sent"
        number = op.id
    try:
        snapshot, cached = fetch(engine, config, owner, bot, offer["source"])
        if not snapshot.videos:
            raise YouTubeError(
                "В канале нет доступных видео. Бесплатная попытка и баланс сохранены."
            )
    except Exception as error:
        # This integration only reads public statistics: no financial side effect to reconcile.
        with Session(engine) as db, db.begin():
            funds = ai_tokens.account(db, owner, bot, test)
            op = operation(db, owner, bot, test, number)
            if op.status == "sent":
                ai_tokens.release_reservation(funds, op)
                op.status, op.balance_after = "failed", ai_tokens.balance_after(funds, op)
        if isinstance(error, (YouTubeError, ValueError)):
            raise
        raise YouTubeError(
            "Не удалось получить анализ YouTube. Баланс и попытка сохранены."
        ) from None
    with Session(engine) as db, db.begin():
        funds = ai_tokens.account(db, owner, bot, test)
        op = operation(db, owner, bot, test, number)
        if op.status == "sent":
            ai_tokens.release_reservation(funds, op)
            ai_tokens.charge(funds, op, op.reserve_tokens)
            op.status, op.result = "completed", {"youtube": snapshot.payload(), "cached": cached}
            op.balance_after = ai_tokens.balance_after(funds, op)
    return number


def result(
    db: Session, owner: int, bot: int, test: bool, number: int
) -> tuple[ChannelSnapshot, bool]:
    op = operation(db, owner, bot, test, number)
    if op.status != "completed" or op.snapshot.get("purpose") != "youtube_search" or not op.result:
        raise ValueError("Сохранённый анализ недоступен")
    return ChannelSnapshot.from_payload(op.result["youtube"]), bool(op.result.get("cached"))


def recover_stale(engine: Engine, config: Settings, bot: int) -> None:
    """A crashed read-only scan must not hold a trial/reserve forever."""
    cutoff = datetime.now(UTC) - timedelta(minutes=3)
    with Session(engine) as db:
        candidates = list(
            db.execute(
                select(AIOperation.id, AIOperation.owner_user_id)
                .where(
                    AIOperation.bot_id == bot,
                    AIOperation.test_mode == test_mode(config),
                    AIOperation.status == "sent",
                    AIOperation.snapshot["purpose"].astext == "youtube_search",
                    AIOperation.owner_user_id.is_not(None),
                    AIOperation.created_at < cutoff,
                )
                .limit(20)
            )
        )
    for number, owner in candidates:
        with Session(engine) as db, db.begin():
            funds = ai_tokens.account(db, owner, bot, test_mode(config))
            op = operation(db, owner, bot, test_mode(config), number)
            if op.status == "sent" and op.created_at < cutoff:
                ai_tokens.release_reservation(funds, op)
                op.status, op.balance_after = "failed", ai_tokens.balance_after(funds, op)
                op.result = {"failure": "interrupted_read"}


def receipt(db: Session, owner: int, bot: int, test: bool, number: int) -> str:
    op = operation(db, owner, bot, test, number)
    return ai_tokens.receipt(op)
    if op.billing_mode == "unlimited":
        return "♾ Безлимитный доступ. Баланс не списывается."
    if op.billing_mode == "trial":
        return "🎁 Бесплатная попытка анализа YouTube. Баланс не списывается."
    if credits.is_credit(op.snapshot):
        return f"Списано: {op.charged_credits} кредитов. Остаток: {op.balance_after} кредитов."
    return (
        f"Стоимость анализа: {op.reserve_tokens} токенов (не расход ИИ). "
        f"Осталось: {op.balance_after}."
    )
