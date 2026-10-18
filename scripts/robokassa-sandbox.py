"""Isolated, test-only merchant acceptance. Never enables the development store."""

import argparse
import hashlib
import json
import re
import secrets
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values
from flask import abort, jsonify, redirect, render_template_string, request
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from waitress import serve

from leedbox.application.payments import create_order
from leedbox.application.wallet import balance
from leedbox.domain.payments import checkout_url
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import Base, create_database_engine
from leedbox.infrastructure.logging import configure_logging
from leedbox.infrastructure.models import PaymentOrder, PaymentReceipt, User, WalletEntry
from leedbox.infrastructure.payment_tunnel import create_gateway
from leedbox.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime" / "robokassa-sandbox" / "state.json"
PREFIX = "leedbox_robo_sandbox_"


def config_and_state(prepare: bool) -> tuple[Settings, dict]:
    original = Settings()
    if original.environment != "development" or not original.robokassa_test:
        raise ValueError("Development and Robokassa test mode are required")
    if not original.robokassa_merchant or not all(
        p.get_secret_value() for p in [original.robokassa_password1, original.robokassa_password2]
    ):
        raise ValueError("Merchant and both test passwords are required")
    raw = dotenv_values(ROOT / ".env.test").get("LEEDBOX_DATABASE_URL")
    url = make_url(raw or "")
    if (
        url.drivername != "postgresql+psycopg"
        or url.host not in {"localhost", "127.0.0.1"}
        or url.port != 5434
        or not (url.database or "").endswith("_test")
    ):
        raise ValueError("Only the dedicated local test PostgreSQL is allowed")
    bot_id = int(original.bot_token.get_secret_value().split(":")[0])
    merchant_hash = hashlib.sha256(original.robokassa_merchant.encode()).hexdigest()
    if STATE.exists():
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if state["bot_id"] != bot_id or state["merchant_hash"] != merchant_hash:
            raise ValueError("Existing sandbox belongs to another bot or merchant")
    elif prepare:
        name = PREFIX + secrets.token_hex(5) + "_test"
        admin = create_engine(
            url.set(database="postgres"), isolation_level="AUTOCOMMIT", hide_parameters=True
        )
        try:
            with admin.connect() as connection:
                # Name is generated locally, not supplied by a request or model.
                connection.execute(text(f'CREATE DATABASE "{name}"'))
        finally:
            admin.dispose()
        state = {"database": name, "bot_id": bot_id, "merchant_hash": merchant_hash}
    else:
        raise ValueError("Sandbox has not been prepared")
    if not re.fullmatch(PREFIX + r"[0-9a-f]{10}_test", state["database"]):
        raise ValueError("Invalid sandbox database name")
    config = original.model_copy(
        update={
            "database_url": SecretStr(
                url.set(database=state["database"]).render_as_string(hide_password=False)
            ),
            "web_port": 5012,
            "web_host": "127.0.0.1",
            "admin_enabled": False,
            "robokassa_enabled": True,
            "robokassa_test": True,
            "stars_enabled": False,
            "payment_operations_enabled": False,
            "public_base_url": None,
        }
    )
    config = Settings.model_validate(config.model_dump())
    if "order_id" not in state:
        if not prepare:
            raise ValueError("Sandbox order is missing")
        engine = create_database_engine(config)
        try:
            # Ephemeral acceptance database only; production uses Alembic migrations.
            Base.metadata.create_all(engine)
            with Session(engine) as db, db.begin():
                user = User(
                    telegram_id=8_000_000_000_000 + secrets.randbelow(1_000_000),
                    username="robokassa_sandbox_dummy",
                    expert_started_at=datetime.now(UTC),
                )
                db.add(user)
                db.flush()
                receipt = json.dumps(
                    {
                        "items": [
                            {
                                "name": "TEST ONLY - fictitious topup, no sale",
                                "quantity": 1,
                                "sum": 100,
                                "payment_object": "service",
                                "payment_method": "full_prepayment",
                                "tax": "none",
                            }
                        ]
                    },
                    separators=(",", ":"),
                )
                order = create_order(
                    db,
                    user.id,
                    bot_id,
                    kind="topup",
                    provider="robokassa",
                    account=config.robokassa_merchant,
                    test_mode=True,
                    amount=Decimal("100.00"),
                    period_days=0,
                    snapshot={"terms_version": "isolated-test-20261016", "receipt": receipt},
                    action_key="isolated-merchant-acceptance",
                )
                # Avoid the common low invoice IDs of previous merchant tests.
                order.id = 1_000_000_000 + secrets.randbelow(1_000_000_000)
                db.flush()
                state.update({"order_id": order.id, "owner_id": user.id})
        finally:
            engine.dispose()
        STATE.parent.mkdir(parents=True, exist_ok=True)
        with STATE.open("x", encoding="utf-8") as output:
            json.dump(state, output)
    return config, state


def report(engine, state: dict) -> dict:
    with Session(engine) as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        order = db.get(PaymentOrder, state["order_id"])
        return {
            "test_only": True,
            "order_id": order.id,
            "status": order.status,
            "amount": str(order.amount),
            "test_balance": str(balance(db, state["owner_id"], state["bot_id"], True)),
            "live_balance": str(balance(db, state["owner_id"], state["bot_id"], False)),
            "receipts": db.scalar(select(func.count()).select_from(PaymentReceipt)),
            "wallet_entries": db.scalar(select(func.count()).select_from(WalletEntry)),
        }


def payment_link(config: Settings, state: dict, engine) -> str:
    with Session(engine) as db:
        order = db.get(PaymentOrder, state["order_id"])
        if not order.test_mode or order.status != "pending":
            raise ValueError("Only the pending sandbox test order can be opened")
        url = checkout_url(
            order.account,
            order.amount,
            order.id,
            config.robokassa_password1.get_secret_value(),
            config.robokassa_hash,
            True,
            state["bot_id"],
            order.snapshot["receipt"],
        )
    parsed = urlsplit(url)
    if parsed.netloc != "auth.robokassa.ru" or parse_qs(parsed.query).get("IsTest") != ["1"]:
        raise ValueError("Refusing a non-test checkout")
    return url


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "serve", "status"])
    args = parser.parse_args()
    config, state = config_and_state(args.mode == "prepare")
    engine = create_database_engine(config)
    if args.mode in {"prepare", "status"}:
        try:
            print(json.dumps(report(engine, state)))
        finally:
            engine.dispose()
        return
    app = create_app(config, engine)
    allowed = {
        "/health",
        "/ready",
        "/sandbox/",
        "/sandbox/status",
        "/sandbox/checkout",
        "/payments/robokassa/result",
        "/payments/robokassa/success",
        "/payments/robokassa/fail",
    }

    @app.before_request
    def limit_routes():
        if request.path not in allowed:
            abort(404)

    @app.get("/sandbox/status")
    def sandbox_status():
        return jsonify(report(engine, state))

    @app.get("/sandbox/")
    def sandbox_page():
        return render_template_string(
            """<!doctype html><html lang="ru"><meta charset="utf-8">
        <meta http-equiv="refresh" content="10"><title>Robokassa — тест</title>
        <body><h1>Только тест. Настоящие деньги не списываются.</h1>
        <p>Вымышленный покупатель, отдельная БД. Сумма: 100 тестовых рублей.</p>
        <p>Не вводите настоящие банковские реквизиты. На странице провайдера
        обязательно должна быть отмечена тестовая оплата.</p>
        {% if data.status == 'pending' %}<p><a href="/sandbox/checkout" target="_blank"
        rel="noopener noreferrer">Открыть тестовую оплату Robokassa</a></p>{% endif %}
        <pre>{{ data | tojson(indent=2) }}</pre>
        <p>Статус обновляется раз в 10 секунд. Возврат браузера не зачисляет средства.</p>
        </body></html>""",
            data=report(engine, state),
        )

    @app.get("/sandbox/checkout")
    def sandbox_checkout():
        return redirect(payment_link(config, state, engine))

    threading.Thread(
        target=serve, args=(app,), kwargs={"host": "127.0.0.1", "port": 5012}, daemon=True
    ).start()
    print(
        json.dumps(
            {"event": "sandbox_start", "test_only": True, "local": "http://127.0.0.1:5012/sandbox/"}
        ),
        flush=True,
    )
    serve(create_gateway(config), host="127.0.0.1", port=5011, threads=4)


if __name__ == "__main__":
    configure_logging()
    try:
        main()
    except Exception:
        # Never expose driver errors, env values or signed checkout URLs in logs.
        print(json.dumps({"event": "sandbox_failed", "secrets_omitted": True}), flush=True)
        raise SystemExit(1) from None
