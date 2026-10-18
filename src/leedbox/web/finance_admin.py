"""Extend the established owner console: prices, operating result and API balances."""

from datetime import UTC, datetime

from flask import abort, flash, g, redirect, request
from flask_admin import expose  # type: ignore[import-untyped]
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application import credits, finance, provider_balance, provider_limits
from leedbox.application.owner_overview import FEATURES, PERIODS, money, window
from leedbox.application.settings import set_setting, setting
from leedbox.application.versions import fingerprint
from leedbox.infrastructure.models import Setting
from leedbox.web.admin import OwnerView, confirmed


class FinanceView(OwnerView):
    def bot_id(self) -> int:
        return int(self.config.bot_token.get_secret_value().split(":")[0])

    @expose("/")
    def index(self) -> str:
        try:
            report = window(
                request.args.get("period", "30"),
                request.args.get(
                    "mode", "live" if self.config.environment == "production" else "test"
                ),
            )
        except ValueError:
            abort(400)
        with Session(self.engine) as db:
            values = finance.summary(db, self.bot_id(), report)
            rows = [
                [
                    FEATURES.get(r.feature, r.feature),
                    r.calls,
                    r.credits or 0,
                    money(r.revenue),
                    f"Известно {money(r.cost)}; расход не подтверждён полностью"
                    if r.unknown
                    else money(r.cost),
                    "Не определён" if r.unknown else money((r.revenue or 0) - (r.cost or 0)),
                ]
                for r in finance.by_agent(db, self.bot_id(), report)
            ]
        return self.page(
            "finance.html",
            title="Финансы и кредиты",
            report=report,
            periods=PERIODS,
            values={k: money(v) if k != "unknown" else v for k, v in values.items()},
            rows=rows,
        )

    @expose("/prices", methods=["GET", "POST"])
    def prices(self) -> str | Response:
        error = ""
        if request.method == "POST":
            confirmed()
            try:
                with Session(self.engine) as db, db.begin():
                    set_setting(
                        db,
                        self.config,
                        g.owner_id,
                        "ai_credits_enabled",
                        True,
                        request.form.get("expected", ""),
                    )
                flash(
                    "Кредиты включены. Старые остатки переведены по сохранённой стоимости "
                    "с округлением в пользу клиента.",
                    "success",
                )
                return redirect(self.url + "/prices")
            except ValueError as problem:
                error = str(problem)
        with Session(self.engine) as db:
            active = credits.enabled(db)
            price = credits.unit_price(db)
            rows = [
                [
                    label,
                    credits.price(db, task),
                    money(credits.price(db, task) * price),
                    {
                        "label": "Изменить",
                        "url": self.config.admin_path + "/settings/edit?key=ai_credit_cost_" + task,
                    },
                ]
                for task, (label, _) in credits.TARIFFS.items()
            ]
        return self.page(
            "credit-prices.html",
            title="Цены AI-кредитов",
            rows=rows,
            active=active,
            price=money(price),
            expected=fingerprint(False),
            error=error,
        )

    @expose("/providers", methods=["GET", "POST"])
    def providers(self) -> str | Response:
        if request.method == "POST":
            confirmed()
            provider_limits.refresh(self.engine, self.config, self.bot_id(), manual=True)
            provider_balance.check(self.engine, self.config, self.bot_id())
            flash(
                "Проверка выполнена. Повторные проверки ограничены: "
                "Zenstat/Apify — 5 минут, DeepSeek — 1 час.",
                "success",
            )
            return redirect(self.url + "/providers")
        with Session(self.engine) as db:
            statuses = {}
            for name in ("zenstat", "apify"):
                row = db.get(Setting, f"provider_limits:{self.bot_id()}:{name}")
                statuses[name] = dict(row.value) if row else {}
            row = db.get(Setting, f"provider_status:{self.bot_id()}")
            statuses["deepseek"] = dict(row.value) if row else {}
            for name, status in statuses.items():
                try:
                    stamp = datetime.fromisoformat(status.get("checked_at", ""))
                    hours = (
                        int(setting(db, "zenstat_balance_check_hours")) if name == "zenstat" else 1
                    )
                    status["stale"] = (
                        datetime.now(UTC) - stamp
                    ).total_seconds() > hours * 3600 + 300
                except (ValueError, TypeError):
                    status["stale"] = True
            youtube = provider_limits.youtube_usage(db, self.config)
            package_price = setting(db, "zenstat_package_price_rub")
            package_size = setting(db, "zenstat_package_requests")
            cost = setting(db, "zenstat_request_cost_rub")
        return self.page(
            "provider-limits.html",
            title="Остатки и лимиты API",
            statuses=statuses,
            youtube=youtube,
            now=datetime.now(UTC),
            package_price=package_price,
            package_size=package_size,
            request_cost=cost,
        )
