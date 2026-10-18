"""Consistent provider failure transitions; never retry an ambiguous paid request."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application import ai_tokens
from leedbox.application.provider_balance import notify
from leedbox.infrastructure.agents import AgentError, rejected_request
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIOperation, ContentTask


def alert(db: Session, config: Settings, bot: int, number: int, error: BaseException) -> None:
    status = error.status if isinstance(error, AgentError) else None
    category, message = {
        401: ("auth", "Проверьте API-ключ DeepSeek: сервис отклонил авторизацию."),
        403: ("auth", "DeepSeek отклонил доступ. Проверьте права API-ключа."),
        402: ("exhausted", "Баланс API DeepSeek исчерпан. Пополните его в кабинете провайдера."),
        429: ("rate", "DeepSeek временно ограничил запросы. Проверьте лимиты провайдера."),
    }.get(status or 0, ("request", "Не удалось завершить запрос ИИ. Проверьте операции в админке."))
    notify(db, config, bot, category, f"{message}\nОперация №{number}. Повтор не выполнялся.")


def record(db: Session, config: Settings, row: AIOperation, error: BaseException) -> bool:
    if row.status in {"completed", "failed"} or row.owner_user_id is None:
        return row.status == "failed"
    funds = ai_tokens.account(db, row.owner_user_id, row.bot_id, row.test_mode)
    rejected = rejected_request(error)
    if rejected:
        ai_tokens.release_reservation(funds, row)
        row.status, row.balance_after = "failed", ai_tokens.balance_after(funds, row)
    else:
        row.status = "uncertain"
    row.result = {"failure": "provider_rejected" if rejected else "provider_unconfirmed"}
    alert(db, config, row.bot_id, row.id, error)
    return rejected


def recover_stale(engine: Engine, config: Settings, bot: int) -> None:
    """Watchdog for interrupted foreground generations, not tasks waiting in a queue."""
    cutoff = datetime.now(UTC) - timedelta(minutes=3)
    with Session(engine) as db:
        candidates = list(
            db.execute(
                select(AIOperation.id, AIOperation.owner_user_id)
                .where(
                    AIOperation.bot_id == bot,
                    AIOperation.test_mode == (config.environment != "production"),
                    AIOperation.kind == "generation",
                    AIOperation.status == "sent",
                    AIOperation.created_at < cutoff,
                    AIOperation.owner_user_id.is_not(None),
                    ~select(ContentTask.token)
                    .where(ContentTask.operation_id == AIOperation.id)
                    .exists(),
                )
                .limit(20)
            )
        )
    for number, owner in candidates:
        with Session(engine) as db, db.begin():
            ai_tokens.account(db, owner, bot, config.environment != "production")
            row = db.get(AIOperation, number, with_for_update=True)
            if row and row.status == "sent" and row.created_at < cutoff:
                record(db, config, row, AgentError())
