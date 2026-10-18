"""Signed Robokassa ResultURL only. Browser returns never change payment state."""

from flask import Flask, Response, abort, render_template, request
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from leedbox.application.payments import confirm_payment
from leedbox.domain.payments import InvalidPayment, verify_result
from leedbox.infrastructure.config import Settings


def install_payments(app: Flask, engine: Engine, config: Settings) -> None:
    @app.get("/payments/robokassa/checkout/<token>")
    def checkout(token: str) -> Response:
        from urllib.parse import parse_qsl, urlsplit

        from sqlalchemy import select

        from leedbox.application.payments import service_checkout
        from leedbox.application.purchasing import owner_request_for_checkout
        from leedbox.domain.payment_handoff import verify
        from leedbox.infrastructure.models import AuditLog, PaymentOrder

        if not config.robokassa_enabled or len(token) > 110:
            abort(404)
        try:
            number = verify(token, config.bot_token.get_secret_value())
            with Session(engine) as db:
                order = db.get(PaymentOrder, number)
                if order is None or order.owner_user_id is None:
                    raise InvalidPayment("Заказ недоступен")
                accepted = db.scalar(
                    select(AuditLog.id)
                    .where(
                        AuditLog.action == "purchase_terms_accepted",
                        AuditLog.details["order_id"].as_integer() == number,
                    )
                    .limit(1)
                )
                row = owner_request_for_checkout(db, order)
                if (
                    accepted is None
                    or row.consent_revoked_at is not None
                    or row.status == "closed"
                    or order.snapshot.get("category") != "physical_service"
                ):
                    raise InvalidPayment("Заказ недоступен")
                url = service_checkout(
                    db,
                    config,
                    order.owner_user_id,
                    int(config.bot_token.get_secret_value().split(":")[0]),
                    number,
                )
                values = parse_qsl(urlsplit(url).query, strict_parsing=True)
            response = Response(
                render_template("payment_handoff.html", number=number, fields=values),
                mimetype="text/html",
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'self'; form-action https://auth.robokassa.ru; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except InvalidPayment:
            abort(404)

    @app.post("/payments/robokassa/result")
    def robokassa_result() -> Response:
        if not config.robokassa_enabled:
            abort(404)
        if request.content_length is None or request.content_length > 16384:
            abort(413)
        if request.mimetype != "application/x-www-form-urlencoded" or request.args:
            abort(400)
        if any(len(request.form.getlist(key)) != 1 for key in request.form):
            abort(400)
        values = request.form.to_dict()
        bot_id = int(config.bot_token.get_secret_value().split(":")[0])
        custom = {"Shp_bot": str(bot_id), "Shp_mode": "test" if config.robokassa_test else "live"}
        try:
            number, amount = verify_result(
                values, config.robokassa_password2.get_secret_value(), config.robokassa_hash, custom
            )
            with Session(engine) as db, db.begin():
                confirm_payment(
                    db,
                    order_id=number,
                    bot_id=bot_id,
                    provider="robokassa",
                    account=config.robokassa_merchant,
                    test_mode=config.robokassa_test,
                    currency="RUB",
                    amount=amount,
                    charge_id=str(number),
                )
        except InvalidPayment:
            abort(400)
        except SQLAlchemyError:
            # No OK before commit; provider may retry, protected by the receipt journal.
            abort(503)
        response = Response(f"OK{number}", mimetype="text/plain")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/payments/robokassa/success", methods=["GET", "POST"])
    @app.route("/payments/robokassa/fail", methods=["GET", "POST"])
    def browser_return() -> Response:
        if not config.robokassa_enabled:
            abort(404)
        # Do not authenticate or settle an order based on the buyer's browser request.
        response = Response(
            "Возврат со страницы оплаты не подтверждает платёж. "
            "Статус изменяется только после серверного подтверждения Robokassa.",
            mimetype="text/plain",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
