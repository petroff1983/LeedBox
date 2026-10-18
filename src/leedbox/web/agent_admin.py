"""Owner-only token grants and explicit settlement of uncertain paid calls."""

import json
from typing import Any
from uuid import uuid4

from flask import abort, g, redirect, request
from flask_admin import expose  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application import ai_tokens, credits
from leedbox.application.agents import test_mode
from leedbox.application.entitlements import start_expert
from leedbox.application.owner_overview import (
    BILLING,
    FEATURES,
    PERIODS,
    STATUSES,
    ai_scope,
    feature_expression,
    money,
    window,
)
from leedbox.application.settings import DEFAULTS, setting
from leedbox.application.versions import fingerprint
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import AIOperation, AuditLog, Setting, User
from leedbox.web.admin import SETTING_LABELS, OwnerView, confirmed, input_field, numeric


class AgentsView(OwnerView):
    @expose("/")
    def index(self) -> str:
        with Session(self.engine) as db:
            bot = int(self.config.bot_token.get_secret_value().split(":")[0])
            provider = db.get(Setting, f"provider_status:{bot}")
            rows = [
                [
                    SETTING_LABELS.get(key, key),
                    str(setting(db, key))[:160] + ("…" if len(str(setting(db, key))) > 160 else ""),
                    {
                        "label": "Изменить",
                        "url": self.config.admin_path + "/settings/edit?key=" + key,
                    },
                ]
                for key in DEFAULTS
                if key.startswith(("ai_", "zenstat_"))
                and not (
                    credits.enabled(db)
                    and key
                    in {
                        "ai_token_packages",
                        "ai_token_price_rub",
                        "ai_content_search_tokens",
                        "ai_dzen_search_tokens",
                        "ai_youtube_search_tokens",
                    }
                )
            ]
            rows.insert(
                0,
                [
                    "DeepSeek: последняя проверка баланса API",
                    (
                        "Ошибка проверки; остаток неизвестен"
                        if provider and provider.value.get("error")
                        else ", ".join(
                            f"{amount} {currency}"
                            for currency, amount in provider.value.get("balances", {}).items()
                        )
                        if provider
                        else "Проверка ещё не выполнялась"
                    )
                    + (" · " + provider.value.get("checked_at", "") if provider else ""),
                    "",
                ],
            )
        return self.page(
            "table.html",
            title="Агенты и AI-токены",
            columns=["Настройка", "Значение", "Действие"],
            rows=rows,
            links=[
                ("Цены кредитов", self.config.admin_path + "/finance/prices"),
                ("Финансы и остатки API", self.config.admin_path + "/finance/"),
                ("Промпты и агенты", self.config.admin_path + "/ai/"),
                ("Доступ пользователей: бесплатные попытки и безлимит", self.url + "/access"),
                ("Начислить токены эксперту", self.url + "/grant"),
                ("История расхода токенов", self.url + "/operations"),
                ("Проверить зависшие операции", self.url + "/uncertain"),
            ],
        )

    @expose("/access", methods=["GET", "POST"])
    def access(self) -> str | Response:
        bot = int(self.config.bot_token.get_secret_value().split(":")[0])
        test = test_mode(self.config)
        error = ""
        telegram = request.values.get("telegram", "")
        values = {
            "unlimited": request.form.get("unlimited") == "1",
            **{task: request.form.get(task, "") for task in ai_tokens.TRIAL_LABELS},
        }
        expected = request.form.get("expected", "")
        preview = "Введите Telegram ID и откройте пользователя."
        with Session(self.engine) as db, db.begin():
            try:
                if request.method == "POST" and not telegram:
                    raise ValueError("Укажите числовой Telegram ID")
                if telegram:
                    number = numeric(telegram)
                    user = db.scalar(select(User).where(User.telegram_id == number))
                    if request.method == "POST":
                        confirmed()
                        reason = request.form.get("reason", "").strip()
                        if not 1 <= len(reason) <= 200:
                            raise ValueError("Укажите причину изменения: до 200 символов")
                        if user is None:
                            user = User(telegram_id=number)
                            db.add(user)
                            db.flush()
                        start_expert(db, user)
                        funds = ai_tokens.account(db, user.id, bot, test)
                        before: dict[str, Any] = {
                            "unlimited": funds.unlimited,
                            "free_limits": funds.free_limits,
                        }
                        if request.form.get("expected", "") != fingerprint(before) and (
                            request.form.get("expected", "") != ""
                            or before
                            != {
                                "unlimited": False,
                                "free_limits": {},
                            }
                        ):
                            raise ValueError("Доступ изменился. Откройте пользователя заново")
                        limits = {}
                        for task in ai_tokens.TRIAL_LABELS:
                            raw = request.form.get(task, "").strip()
                            if raw:
                                if (
                                    not raw.isascii()
                                    or not raw.isdecimal()
                                    or not 0 <= int(raw) <= 100000
                                ):
                                    raise ValueError(
                                        "Количество попыток: целое от 0 до 100000 или пустое поле"
                                    )
                                limits[task] = int(raw)
                        funds.unlimited = request.form.get("unlimited") == "1"
                        funds.free_limits = limits
                        db.add(
                            AuditLog(
                                actor_telegram_id=g.owner_id,
                                subject_user_id=user.id,
                                action="ai_access_changed",
                                details={
                                    "bot_id": bot,
                                    "test_mode": test,
                                    "before": before,
                                    "after": {"unlimited": funds.unlimited, "free_limits": limits},
                                    "reason": reason,
                                },
                            )
                        )
                        return redirect(self.url + "/access?telegram=" + telegram)
                    if user and user.expert_started_at:
                        from leedbox.infrastructure.models import AIAccount

                        existing = db.get(AIAccount, (user.id, bot, test))
                        before = {
                            "unlimited": existing.unlimited if existing else False,
                            "free_limits": existing.free_limits if existing else {},
                        }
                        expected = fingerprint(before)
                        values = {
                            "unlimited": before["unlimited"],
                            **{
                                task: before["free_limits"].get(task, "")
                                for task in ai_tokens.TRIAL_LABELS
                            },
                        }
                        preview = f"Баланс: {ai_tokens.available(db, user.id, bot, test)} " + (
                            "кредитов.\n" if credits.enabled(db) else "токенов.\n"
                        )
                        preview += "\n".join(
                            f"{label}: доступно "
                            f"{ai_tokens.free_remaining(db, user.id, bot, test, task)} попыток"
                            for task, label in (
                                *ai_tokens.TRIAL_LABELS.items(),
                                ("dzen_search", "Дзен: поиск"),
                            )
                        )
                        if self.config.is_owner(number):
                            preview += (
                                "\nВладелец сервиса: безлимит всегда, независимо от переключателя."
                            )
                    else:
                        preview = (
                            "При сохранении будет добавлен эксперт. Приглашение не отправляется."
                        )
            except (ValueError, ObjectUnavailable) as problem:
                db.rollback()
                error = str(problem) or "Пользователь недоступен"
        return self.editor(
            "AI-доступ пользователя",
            [
                input_field("telegram", "Telegram ID", telegram),
                input_field(
                    "unlimited",
                    "Безлимит: все AI-операции без списания баланса",
                    values["unlimited"],
                    "checkbox",
                ),
                *[
                    input_field(
                        task, f"{label}: всего бесплатных попыток за всё время", values[task]
                    )
                    for task, label in ai_tokens.TRIAL_LABELS.items()
                ],
                input_field("reason", "Причина изменения", request.form.get("reason", "")),
            ],
            {"expected": expected},
            error=error,
            preview=preview,
            lookup=True,
            help_text=(
                "Пустое количество — общий норматив. Использованные попытки вычитаются; "
                "0 отключает бесплатные генерации. Платный баланс не меняется. "
                "Локальные и production-права разделены."
            ),
        )

    @expose("/operations")
    def operations(self) -> str:
        bot = int(self.config.bot_token.get_secret_value().split(":")[0])
        try:
            keys = ("period", "mode", "feature", "status", "billing", "telegram", "page")
            if any(len(request.args.getlist(key)) > 1 for key in keys):
                abort(400)
            report = window(
                request.args.get("period", "all"),
                request.args.get(
                    "mode", "live" if self.config.environment == "production" else "test"
                ),
            )
            feature = request.args.get("feature", "")
            status = request.args.get("status", "")
            billing = request.args.get("billing", "")
            telegram = request.args.get("telegram", "")
            page = numeric(request.args.get("page", "0"), 10000)
            if (
                (feature and feature not in FEATURES)
                or (status and status not in STATUSES)
                or (billing and billing not in BILLING)
            ):
                abort(400)
            query = (
                select(AIOperation, User.telegram_id, feature_expression())
                .outerjoin(User, User.id == AIOperation.owner_user_id)
                .where(*ai_scope(bot, report))
            )
            if feature:
                query = query.where(
                    feature_expression() == feature, AIOperation.kind.in_(["generation", "search"])
                )
            if status:
                query = query.where(AIOperation.status == status)
            if billing:
                query = query.where(AIOperation.billing_mode == billing)
            if telegram:
                query = query.where(User.telegram_id == numeric(telegram))
        except ValueError:
            abort(400)
        with Session(self.engine) as db:
            entries = list(
                db.execute(query.order_by(AIOperation.id.desc()).offset(page * 50).limit(51))
            )
            rows = [
                [
                    row.id,
                    row.created_at.strftime("%d.%m.%Y %H:%M UTC"),
                    actor or "Удалён",
                    FEATURES[code]
                    if row.kind in {"generation", "search"}
                    else {
                        "grant": "Ручное начисление",
                        "purchase": "Покупка токенов",
                    }.get(row.kind, row.kind),
                    STATUSES[row.status],
                    row.model,
                    BILLING[row.billing_mode],
                    row.input_tokens,
                    row.output_tokens,
                    row.reserve_tokens,
                    row.balance_after if row.balance_after is not None else "Неизвестно",
                    money(row.cost_rub, 4),
                    row.charged_credits,
                    money(row.revenue_rub, 4),
                    "Кредиты"
                    if credits.is_credit(row.snapshot) or row.snapshot.get("unit") == "credits"
                    else "Токены",
                ]
                for row, actor, code in entries[:50]
            ]
        return self.page(
            "ai_operations.html",
            title="AI-операции",
            report=report,
            periods=PERIODS,
            features=FEATURES,
            statuses=STATUSES,
            billing_modes=BILLING,
            feature=feature,
            status=status,
            billing=billing,
            telegram=telegram,
            page=page,
            more=len(entries) > 50,
            columns=[
                "ID",
                "Дата UTC",
                "Telegram ID эксперта",
                "Тип",
                "Статус",
                "Модель",
                "Оплата",
                "Вход",
                "Выход",
                "Резерв",
                "Остаток",
                "Оценка API, ₽",
                "Списано кредитов",
                "Выручка кредитов, ₽",
                "Единицы остатка",
            ],
            rows=rows,
        )

    @expose("/grant", methods=["GET", "POST"])
    def grant(self) -> str | Response:
        error = ""
        values = {key: request.form.get(key, "") for key in ("telegram", "tokens", "reason")}
        key = request.form.get("key", uuid4().hex)
        if request.method == "POST":
            try:
                confirmed()
                telegram, tokens = numeric(values["telegram"]), numeric(values["tokens"], 100000000)
                if not 1 <= len(values["reason"]) <= 200 or len(key) != 32:
                    raise ValueError("Нужна причина начисления (до 200 символов)")
                bot = int(self.config.bot_token.get_secret_value().split(":")[0])
                with Session(self.engine) as db, db.begin():
                    owner = db.scalar(select(User.id).where(User.telegram_id == telegram))
                    if owner is None:
                        raise ValueError("Эксперт не найден")
                    ai_tokens.grant(
                        db,
                        owner,
                        bot,
                        test_mode(self.config),
                        "owner:" + key,
                        tokens,
                        snapshot={
                            "actor": g.owner_id,
                            "reason": values["reason"],
                            **({"unit": "credits"} if credits.enabled(db) else {}),
                        },
                    )
                    db.add(
                        AuditLog(
                            action="ai_tokens_granted",
                            actor_telegram_id=g.owner_id,
                            details={"owner_user_id": owner, "tokens": tokens},
                        )
                    )
                return redirect(self.url + "/")
            except (ValueError, ObjectUnavailable) as problem:
                error = str(problem) or "Начисление доступно только зарегистрированному эксперту"
        return self.editor(
            "Начислить AI-баланс (кредиты в новом режиме)",
            [
                input_field(key, label, values[key])
                for key, label in (
                    ("telegram", "Telegram ID эксперта"),
                    ("tokens", "Количество"),
                    ("reason", "Причина"),
                )
            ],
            {"key": key},
            error=error,
            help_text=(
                "Это ручное начисление, не подтверждение оплаты. "
                "Локальные и реальные балансы разделены."
            ),
        )

    @expose("/uncertain", methods=["GET", "POST"])
    def uncertain(self) -> str | Response:
        bot = int(self.config.bot_token.get_secret_value().split(":")[0])
        if request.method == "POST":
            confirmed()
            number = numeric(request.form.get("number", ""))
            with Session(self.engine) as db, db.begin():
                row = db.get(AIOperation, number)
                if (
                    row is None
                    or row.owner_user_id is None
                    or row.bot_id != bot
                    or row.test_mode != test_mode(self.config)
                ):
                    abort(404)
                funds = ai_tokens.account(db, row.owner_user_id, bot, row.test_mode)
                db.refresh(row, with_for_update=True)
                if row.status not in {"sent", "uncertain"}:
                    abort(409)
                from datetime import UTC, datetime, timedelta

                if row.created_at > datetime.now(UTC) - timedelta(minutes=5):
                    abort(409)
                ai_tokens.release_reservation(funds, row)
                row.status, row.balance_after = "failed", ai_tokens.balance_after(funds, row)
                db.add(
                    AuditLog(
                        action="ai_reservation_released_at_service_expense",
                        actor_telegram_id=g.owner_id,
                        details={"operation_id": number},
                    )
                )
            return redirect(self.url + "/uncertain")
        with Session(self.engine) as db:
            rows = [
                [r.id, r.created_at.isoformat(), r.owner_user_id, r.status, r.reserve_tokens]
                for r in db.scalars(
                    select(AIOperation)
                    .where(
                        AIOperation.bot_id == bot,
                        AIOperation.test_mode == test_mode(self.config),
                        AIOperation.status.in_(["sent", "uncertain"]),
                    )
                    .limit(100)
                )
            ]
        return self.editor(
            "Освободить резерв за счёт сервиса",
            [input_field("number", "ID операции", "")],
            {},
            preview=json.dumps(rows, ensure_ascii=False, indent=2),
            help_text=(
                "Список: ID, дата UTC, эксперт, статус, резерв. Освобождение — только "
                "после пяти минут и проверки. Неизвестный результат оплачивает сервис; "
                "повтор этой операции не запускается."
            ),
        )
