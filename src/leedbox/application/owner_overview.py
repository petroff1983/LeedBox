"""Read-only owner reporting over existing journals. No provider calls or settlements."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import policy
from leedbox.application.settings import setting
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    AIOperation,
    AuditLog,
    PaymentOperation,
    PaymentOrder,
    ProductEvent,
    ServiceRequest,
    Setting,
    User,
)

PERIODS = {"today": "Сегодня (UTC)", "7": "7 дней", "30": "30 дней", "all": "Вся история"}
FEATURES = {
    "dzen": "Дзен · заголовки",
    "writing_profile": "Дзен · промпт статьи",
    "dzen_search": "Дзен · поиск Zenstat",
    "image_prompts": "Промпты для картинок",
    "content": "Контент · генерация",
    "content_search": "Контент · сбор Apify",
    "youtube_search": "YouTube · анализ канала",
    "lead_magnet": "Темы лид-магнитов",
    "other": "Другие AI-операции",
}
STATUSES = {
    "reserved": "Зарезервировано",
    "sent": "Отправлено",
    "completed": "Завершено",
    "uncertain": "Нужна проверка",
    "failed": "Ошибка",
}
BILLING = {"balance": "Баланс", "trial": "Бесплатная попытка", "unlimited": "Безлимит"}


@dataclass(frozen=True)
class ReportWindow:
    period: str
    mode: str
    start: datetime
    end: datetime

    @property
    def test(self) -> bool:
        return self.mode == "test"

    @property
    def previous(self) -> "ReportWindow":
        return ReportWindow(
            self.period, self.mode, self.start - (self.end - self.start), self.start
        )


def window(period: str, mode: str, now: datetime | None = None) -> ReportWindow:
    if period not in PERIODS or mode not in {"test", "live"}:
        raise ValueError("Неизвестный период или режим отчёта")
    end = (now or datetime.now(UTC)).astimezone(UTC)
    start = (
        datetime(1970, 1, 1, tzinfo=UTC)
        if period == "all"
        else (
            end.replace(hour=0, minute=0, second=0, microsecond=0)
            if period == "today"
            else (end - timedelta(days=int(period)))
        )
    )
    return ReportWindow(period, mode, start, end)


def feature_expression() -> Any:
    purpose = func.coalesce(AIOperation.snapshot["purpose"].astext, "dzen")
    return case(
        (purpose == "lead_magnet_revision", "lead_magnet"),
        (purpose.in_(list(FEATURES)), purpose),
        else_="other",
    )


def ai_scope(bot: int, report: ReportWindow) -> tuple[Any, ...]:
    return (
        AIOperation.bot_id == bot,
        AIOperation.test_mode == report.test,
        AIOperation.created_at >= report.start,
        AIOperation.created_at < report.end,
    )


def money(value: Any, places: int = 2) -> str:
    return f"{Decimal(value or 0):,.{places}f}".replace(",", "\u00a0").replace(".", ",")


def metrics(db: Session, bot: int, report: ReportWindow) -> dict[str, Any]:
    scope = ai_scope(bot, report)
    ai = db.execute(
        select(
            func.count().filter(AIOperation.status == "completed"),
            func.count().filter(AIOperation.status == "failed"),
            func.coalesce(func.sum(AIOperation.cost_rub), 0),
        ).where(*scope, AIOperation.kind.in_(["generation", "search"]))
    ).one()
    # Users have no bot_id: new registrations are explicitly labelled service-wide.
    new = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.expert_started_at >= report.start,
                User.expert_started_at < report.end,
            )
        )
        or 0
    )
    event_actors = select(ProductEvent.telegram_user_id.label("telegram_id")).where(
        ProductEvent.bot_id == bot,
        ProductEvent.created_at >= report.start,
        ProductEvent.created_at < report.end,
        ProductEvent.name.in_(
            ["tool_started", "tool_completed", "service_requested", "link_created"]
        ),
    )
    ai_actors = (
        select(User.telegram_id.label("telegram_id"))
        .join(AIOperation, AIOperation.owner_user_id == User.id)
        .where(
            AIOperation.bot_id == bot,
            AIOperation.created_at >= report.start,
            AIOperation.created_at < report.end,
            AIOperation.kind.in_(["generation", "search"]),
        )
    )
    actors = event_actors.union(ai_actors).subquery()
    active = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.expert_started_at.is_not(None),
                User.telegram_id.in_(select(actors.c.telegram_id)),
            )
        )
        or 0
    )
    events = {
        name: count
        for name, count in db.execute(
            select(ProductEvent.name, func.count())
            .where(
                ProductEvent.bot_id == bot,
                ProductEvent.created_at >= report.start,
                ProductEvent.created_at < report.end,
                ProductEvent.name.in_(["delivery_succeeded", "error_occurred"]),
            )
            .group_by(ProductEvent.name)
        ).all()
    }
    return {
        "new": new,
        "active": active,
        "completed": ai[0],
        "failed": ai[1],
        "cost": ai[2],
        "deliveries": events.get("delivery_succeeded", 0),
        "errors": events.get("error_occurred", 0),
    }


def expenses(db: Session, bot: int, report: ReportWindow) -> list[dict[str, Any]]:
    feature = feature_expression()
    rows = db.execute(
        select(
            feature,
            func.count(),
            func.count().filter(AIOperation.status == "completed"),
            func.count().filter(AIOperation.status == "failed"),
            func.count().filter(AIOperation.status.in_(["reserved", "sent", "uncertain"])),
            func.sum(AIOperation.input_tokens),
            func.sum(AIOperation.output_tokens),
            func.sum(AIOperation.cost_rub),
        )
        .where(*ai_scope(bot, report), AIOperation.kind.in_(["generation", "search"]))
        .group_by(feature)
        .order_by(func.sum(AIOperation.cost_rub).desc(), feature)
    ).all()
    return [
        dict(
            code=r[0],
            name=FEATURES[r[0]],
            calls=r[1],
            completed=r[2],
            failed=r[3],
            pending=r[4],
            incoming=r[5],
            outgoing=r[6],
            cost=money(r[7], 4),
        )
        for r in rows
    ]


def payments(db: Session, bot: int, report: ReportWindow) -> list[dict[str, Any]]:
    result = []
    for currency in ("RUB", "XTR"):
        scope = (
            PaymentOrder.bot_id == bot,
            PaymentOrder.test_mode == report.test,
            PaymentOrder.currency == currency,
        )
        received = db.execute(
            select(func.count(), func.coalesce(func.sum(PaymentOrder.amount), 0)).where(
                *scope,
                PaymentOrder.status.in_(["paid", "refunded"]),
                PaymentOrder.paid_at >= report.start,
                PaymentOrder.paid_at < report.end,
            )
        ).one()
        refunded = db.scalar(
            select(func.coalesce(func.sum(PaymentOrder.amount), 0)).where(
                *scope,
                PaymentOrder.status == "refunded",
                PaymentOrder.refunded_at >= report.start,
                PaymentOrder.refunded_at < report.end,
            )
        )
        result.append(
            dict(
                currency=currency,
                count=received[0],
                received=money(received[1]),
                refunded=money(refunded),
            )
        )
    return result


def integrations(db: Session, config: Settings, bot: int, now: datetime) -> list[dict[str, Any]]:
    root = config.admin_path
    result: list[dict[str, Any]] = []
    configured = bool(config.deepseek_api_key.get_secret_value())
    row = db.get(Setting, f"provider_status:{bot}")
    data = row.value if row and isinstance(row.value, dict) else {}
    checked: datetime | None = None
    try:
        checked = datetime.fromisoformat(data["checked_at"])
        if checked.tzinfo is None or checked > now:
            checked = None
    except (KeyError, ValueError, TypeError):
        pass
    status, detail, attention = "Не настроен", "API-ключ отсутствует.", configured
    if configured:
        status, detail = "Нет свежей проверки", "Баланс проверяет фоновый процесс бота."
        if checked and now - checked <= timedelta(hours=2):
            try:
                balances = data["balances"]
                if (
                    not balances
                    or not isinstance(balances, dict)
                    or type(data["available"]) is not bool
                ):
                    raise ValueError
                amounts = {c: Decimal(v) for c, v in balances.items()}
                if any(c not in {"USD", "CNY"} or not a.is_finite() for c, a in amounts.items()):
                    raise ValueError
                low = not data["available"] or all(
                    a <= Decimal(str(setting(db, "ai_provider_low_" + c.lower())))
                    for c, a in amounts.items()
                )
                attention = low
                status = "Пора пополнить" if low else "Баланс проверен"
                detail = "; ".join(f"{money(a)} {c}" for c, a in amounts.items())
            except (KeyError, TypeError, ValueError, InvalidOperation):
                status, detail = "Проверка не удалась", "Проверьте ключ и кабинет провайдера."
    result.append(
        dict(
            name="DeepSeek",
            status=status,
            detail=detail,
            checked=checked.strftime("%d.%m.%Y %H:%M UTC") if checked else "Нет данных",
            attention=attention,
            url=root + "/agents/",
        )
    )
    for name, key in (
        ("Zenstat", config.zenstat_api_key),
        ("Apify", config.apify_api_token),
        ("YouTube", config.youtube_api_key),
    ):
        result.append(
            dict(
                name=name,
                status="Ключ задан" if key.get_secret_value() else "Не настроен",
                detail="Доступность и баланс не проверялись.",
                checked="Нет проверки",
                attention=False,
                url=root + "/ai/",
            )
        )
    result.extend(
        [
            dict(
                name="Telegram",
                status="Нет проверки polling",
                detail="Открытие админки не проверяет соединение и не запускает бота.",
                checked="Нет проверки",
                attention=False,
                url=root + "/operations/stats",
            ),
            dict(
                name="Robokassa",
                status=("Настроена · тест" if config.robokassa_test else "Настроена · боевой режим")
                if config.robokassa_enabled
                else "Отключена",
                detail="Доступность и прохождение платежа не проверялись.",
                checked="Нет проверки",
                attention=False,
                url=root + "/payments/",
            ),
        ]
    )
    return result


def overview(db: Session, config: Settings, report: ReportWindow) -> dict[str, Any]:
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    root = config.admin_path
    alerts: list[dict[str, str]] = []
    ready = bool(setting(db, "owner_details") and setting(db, "owner_contact"))
    if not ready:
        alerts.append(
            dict(
                title="Подготовьте приём заявок",
                detail="Не заполнены контакт или реквизиты владельца.",
                url=root + "/settings/",
                action="Открыть настройки",
            )
        )
    pending = (
        db.scalar(
            select(func.count())
            .select_from(ServiceRequest)
            .where(
                ServiceRequest.bot_id == bot,
                ServiceRequest.status == "new",
                ServiceRequest.consent_revoked_at.is_(None),
            )
        )
        or 0
    )
    if pending:
        alerts.append(
            dict(
                title=f"Новые заявки: {pending}",
                detail="Ожидают обработки.",
                url=root + "/requests/?status=new",
                action="Открыть заявки",
            )
        )
    uncertain = (
        db.scalar(
            select(func.count())
            .select_from(AIOperation)
            .where(
                AIOperation.bot_id == bot,
                AIOperation.test_mode == report.test,
                (AIOperation.status == "uncertain")
                | (
                    (AIOperation.status == "sent")
                    & (AIOperation.created_at < report.end - timedelta(minutes=5))
                ),
            )
        )
        or 0
    )
    if uncertain:
        alerts.append(
            dict(
                title=f"AI-операции требуют проверки: {uncertain}",
                detail="Исход запроса неизвестен. Не повторяйте его автоматически.",
                url=root + "/agents/uncertain"
                if report.test == (config.environment != "production")
                else root + "/agents/operations?period=all&status=uncertain&mode=" + report.mode,
                action="Разобрать операции",
            )
        )
    problem = select(PaymentOperation.order_id).where(
        PaymentOperation.status.in_(["failed", "uncertain"])
    )
    orders = list(
        db.scalars(
            select(PaymentOrder.id)
            .where(
                PaymentOrder.bot_id == bot,
                PaymentOrder.test_mode == report.test,
                (PaymentOrder.status == "review") | PaymentOrder.id.in_(problem),
            )
            .order_by(PaymentOrder.id.desc())
            .limit(6)
        )
    )
    for number in orders[:5]:
        alerts.append(
            dict(
                title=f"Проверить платёж №{number}",
                detail="Требует сверки или проверки операции возврата.",
                url=f"{root}/payments/detail?id={number}",
                action="Открыть платёж",
            )
        )
    if len(orders) > 5:
        alerts.append(
            dict(
                title="Есть другие проблемные платежи",
                detail="Откройте журнал платежей.",
                url=root + "/payments/",
                action="Все платежи",
            )
        )
    providers = integrations(db, config, bot, report.end)
    for provider in providers:
        if provider["attention"]:
            alerts.append(
                dict(
                    title=provider["name"] + ": " + provider["status"],
                    detail=provider["detail"],
                    url=provider["url"],
                    action="Проверить интеграцию",
                )
            )
    # AuditLog has no bot_id: show only safe metadata and explicitly label its scope.
    audit = list(
        db.execute(
            select(AuditLog.action, AuditLog.created_at)
            .where(AuditLog.actor_telegram_id.in_(config.owner_ids))
            .order_by(AuditLog.id.desc())
            .limit(5)
        )
    )
    current = metrics(db, bot, report)
    return dict(
        report=report,
        periods=PERIODS,
        metrics=current,
        previous=metrics(db, bot, report.previous) if report.period != "all" else {},
        expenses=expenses(db, bot, report),
        payments=payments(db, bot, report),
        alerts=alerts,
        integrations=providers,
        audit=audit,
        state=policy(db).mode,
        cost=money(current["cost"], 4),
    )
