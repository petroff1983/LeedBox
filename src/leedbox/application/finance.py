"""Read-only financial management estimates, never cash receipts counted twice."""

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leedbox.application.owner_overview import ReportWindow, ai_scope, feature_expression
from leedbox.application.settings import setting
from leedbox.infrastructure.models import (
    AIAccount,
    AIOperation,
    PaymentOrder,
    WalletAccount,
    WalletEntry,
)


def summary(db: Session, bot: int, report: ReportWindow) -> dict[str, Any]:
    scope = (
        WalletEntry.bot_id == bot,
        WalletEntry.test_mode == report.test,
        WalletEntry.created_at >= report.start,
        WalletEntry.created_at < report.end,
    )
    paid = db.scalar(
        select(func.coalesce(func.sum(PaymentOrder.amount), 0)).where(
            PaymentOrder.bot_id == bot,
            PaymentOrder.test_mode == report.test,
            PaymentOrder.currency == "RUB",
            PaymentOrder.paid_at >= report.start,
            PaymentOrder.paid_at < report.end,
            PaymentOrder.status.in_(["paid", "refunded"]),
        )
    ) or Decimal(0)
    refunded = db.scalar(
        select(func.coalesce(func.sum(PaymentOrder.amount), 0)).where(
            PaymentOrder.bot_id == bot,
            PaymentOrder.test_mode == report.test,
            PaymentOrder.currency == "RUB",
            PaymentOrder.status == "refunded",
            PaymentOrder.refunded_at >= report.start,
            PaymentOrder.refunded_at < report.end,
        )
    ) or Decimal(0)
    core = -(
        db.scalar(
            select(func.coalesce(func.sum(WalletEntry.amount), 0)).where(
                *scope,
                WalletEntry.kind == "purchase",
                func.coalesce(WalletEntry.snapshot["purpose"].astext, "access") != "ai_tokens",
            )
        )
        or Decimal(0)
    )
    ai_revenue = db.scalar(
        select(func.coalesce(func.sum(AIOperation.revenue_rub), 0)).where(
            *ai_scope(bot, report), AIOperation.kind.in_(["generation", "search"])
        )
    ) or Decimal(0)
    legacy_revenue = -(
        db.scalar(
            select(func.coalesce(func.sum(WalletEntry.amount), 0)).where(
                *scope,
                WalletEntry.kind == "purchase",
                WalletEntry.snapshot["purpose"].astext == "ai_tokens",
                func.coalesce(WalletEntry.snapshot["unit"].astext, "tokens") != "credits",
            )
        )
        or Decimal(0)
    )
    ai_cost = db.scalar(
        select(func.coalesce(func.sum(AIOperation.cost_rub), 0)).where(
            *ai_scope(bot, report), AIOperation.kind.in_(["generation", "search"])
        )
    ) or Decimal(0)
    credits_sold = -(
        db.scalar(
            select(func.coalesce(func.sum(WalletEntry.amount), 0)).where(
                *scope,
                WalletEntry.kind == "purchase",
                WalletEntry.snapshot["unit"].astext == "credits",
            )
        )
        or Decimal(0)
    )
    obligations = db.scalar(
        select(func.coalesce(func.sum(WalletAccount.balance), 0)).where(
            WalletAccount.bot_id == bot,
            WalletAccount.test_mode == report.test,
            WalletAccount.balance > 0,
        )
    ) or Decimal(0)
    credit_value = db.scalar(
        select(func.coalesce(func.sum(AIAccount.credit_value_rub), 0)).where(
            AIAccount.bot_id == bot,
            AIAccount.test_mode == report.test,
        )
    ) or Decimal(0)
    unknown = (
        db.scalar(
            select(func.count())
            .select_from(AIOperation)
            .where(
                *ai_scope(bot, report),
                AIOperation.kind.in_(["generation", "search"]),
                (AIOperation.cost_rub.is_(None))
                | AIOperation.status.in_(["sent", "uncertain", "reserved"]),
            )
        )
        or 0
    )
    fees = paid * Decimal(str(setting(db, "finance_fee_percent"))) / 100
    taxes = (paid - refunded) * Decimal(str(setting(db, "finance_tax_percent"))) / 100
    days = Decimal(str((report.end - report.start).total_seconds())) / 86400
    if report.period == "all":
        first = db.scalar(
            select(func.min(AIOperation.created_at)).where(
                AIOperation.bot_id == bot, AIOperation.test_mode == report.test
            )
        )
        days = (
            max(Decimal(0), Decimal(str((report.end - first).total_seconds())) / 86400)
            if first
            else Decimal(0)
        )
    server = Decimal(str(setting(db, "finance_server_monthly_rub"))) * days / 30
    revenue = core + ai_revenue + legacy_revenue
    return {
        "received": paid,
        "refunded": refunded,
        "core": core,
        "ai_revenue": ai_revenue,
        "legacy_revenue": legacy_revenue,
        "credits_sold": credits_sold,
        "revenue": revenue,
        "api": ai_cost,
        "fees": fees,
        "taxes": taxes,
        "server": server,
        "result": revenue - ai_cost - fees - taxes - server,
        "wallet_liability": obligations,
        "credit_liability": credit_value,
        "unknown": unknown,
    }


def by_agent(db: Session, bot: int, report: ReportWindow) -> list[Any]:
    feature = feature_expression()
    return list(
        db.execute(
            select(
                feature.label("feature"),
                func.count().label("calls"),
                func.sum(AIOperation.charged_credits).label("credits"),
                func.sum(AIOperation.revenue_rub).label("revenue"),
                func.sum(AIOperation.cost_rub).label("cost"),
                func.count()
                .filter(
                    (AIOperation.cost_rub.is_(None))
                    | AIOperation.status.in_(["sent", "uncertain", "reserved"])
                )
                .label("unknown"),
            )
            .where(*ai_scope(bot, report), AIOperation.kind.in_(["generation", "search"]))
            .group_by(feature)
            .order_by(feature)
        )
    )
