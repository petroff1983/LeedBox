# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_admin_postgres import admin_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.owner_overview import expenses, metrics, overview, payments, window
from leedbox.infrastructure.models import AIOperation, PaymentOrder, ProductEvent, User


def operation(owner, bot, report, **changes):
    values = dict(
        owner_user_id=owner,
        bot_id=bot,
        test_mode=report.test,
        action_key=uuid4().hex,
        kind="generation",
        status="completed",
        model="synthetic-model",
        reserve_tokens=100,
        billing_mode="balance",
        input_tokens=10,
        output_tokens=20,
        cost_usd=Decimal("0.01"),
        cost_rub=Decimal("1.25"),
        snapshot={"purpose": "image_prompts", "inputs": "PRIVATE_BRIEF"},
        created_at=report.start + timedelta(hours=1),
    )
    values.update(changes)
    return AIOperation(**values)


@pytest.mark.integration
def test_expenses_isolate_bot_mode_dates_and_include_free_failed_owner(service_fixture):
    engine, config, actors, owners, *_ = service_fixture
    bot = actors[0]
    report = window("7", "test", datetime.now(UTC))
    with Session(engine) as db:
        before = metrics(db, bot, report)
        for changes in (
            {},
            {"billing_mode": "trial", "status": "failed"},
            {"billing_mode": "unlimited", "snapshot": {"purpose": "lead_magnet_revision"}},
            {"kind": "search", "model": "apify", "snapshot": {"purpose": "content_search"}},
            {"kind": "grant", "cost_rub": Decimal("100")},
            {"kind": "purchase", "cost_rub": Decimal("100")},
            {"test_mode": False, "cost_rub": Decimal("100")},
            {"bot_id": bot + 1, "cost_rub": Decimal("100")},
            {"created_at": report.end, "cost_rub": Decimal("100")},
            {"created_at": report.start - timedelta(microseconds=1), "cost_rub": Decimal("100")},
        ):
            db.add(operation(owners[0], bot, report, **changes))
        recipient = User(telegram_id=actors[0] + 9000000000)
        db.add(recipient)
        db.flush()
        db.add_all(
            [
                ProductEvent(
                    bot_id=bot,
                    telegram_user_id=recipient.telegram_id,
                    name="user_started",
                    details={"kind": "recipient"},
                    created_at=report.start,
                ),
                ProductEvent(
                    bot_id=bot,
                    telegram_user_id=recipient.telegram_id,
                    name="delivery_succeeded",
                    details={},
                    created_at=report.start,
                ),
                ProductEvent(
                    bot_id=bot,
                    telegram_user_id=actors[1],
                    name="tool_started",
                    details={"tool": "utm"},
                    created_at=report.start,
                ),
            ]
        )
        db.flush()
        result = metrics(db, bot, report)
        assert result["cost"] == Decimal("5.00")
        assert result["completed"] == 3 and result["failed"] == 1
        assert result["new"] == before["new"]  # Recipient-only is not a new expert.
        assert result["active"] == 2  # Owner + tool user; recipient is not active expert.
        assert result["deliveries"] == 1
        rows = {r["code"]: r for r in expenses(db, bot, report)}
        assert rows["image_prompts"]["calls"] == 2
        assert rows["image_prompts"]["failed"] == 1
        assert rows["lead_magnet"]["cost"] == "1,2500"
        assert rows["content_search"]["cost"] == "1,2500"
        assert "PRIVATE_BRIEF" not in str(rows)
        # The reporting function cannot persist changes, grants or settlements.
        config = config.model_copy(update={"bot_token": SecretStr(f"{bot}:synthetic")})
        with db.no_autoflush:
            result = overview(db, config, report)
        assert not db.new and not db.dirty
        assert result["cost"] == "5,0000"
        # Session closes without commit; all synthetic reporting rows are rolled back.


@pytest.mark.integration
def test_payment_totals_use_confirmation_and_refund_dates(service_fixture):
    engine, _, actors, owners, *_ = service_fixture
    bot = actors[0]
    report = window("7", "test")
    with Session(engine) as db:
        for changes in (
            {},
            {"status": "refunded", "refunded_at": report.start + timedelta(hours=2)},
            {
                "paid_at": report.start - timedelta(days=2),
                "status": "refunded",
                "refunded_at": report.start + timedelta(hours=2),
            },
            {"currency": "XTR", "provider": "stars", "kind": "access", "period_days": 30},
            {"status": "pending", "paid_at": None},
            {"status": "review"},
            {"test_mode": False},
            {"bot_id": bot + 1},
        ):
            values = dict(
                owner_user_id=owners[0],
                bot_id=bot,
                kind="topup",
                provider="robokassa",
                account="synthetic",
                test_mode=True,
                currency="RUB",
                amount=Decimal("100"),
                period_days=0,
                snapshot={},
                action_key=uuid4().hex,
                request_hash="a" * 64,
                status="paid",
                created_at=report.start - timedelta(days=30),
                paid_at=report.start + timedelta(hours=1),
            )
            values.update(changes)
            db.add(PaymentOrder(**values))
        db.flush()
        rub, stars = payments(db, bot, report)
        assert rub == dict(currency="RUB", count=2, received="200,00", refunded="200,00")
        assert stars == dict(currency="XTR", count=1, received="100,00", refunded="0,00")


@pytest.mark.integration
def test_overview_routes_filters_auth_and_no_secrets(admin_fixture):
    client, _, _, fixture, _ = admin_fixture
    engine = fixture[0]
    with Session(engine) as db:
        before = list(db.execute(select(AIOperation.id, AIOperation.status, AIOperation.cost_rub)))
    for path in (
        "/owner/",
        "/owner/?period=today&mode=live",
        "/owner/agents/operations?period=30&mode=test&feature=content&billing=trial",
        "/owner/agents/operations?telegram=99999999999&status=uncertain",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "PRIVATE_BRIEF" not in response.text
        assert "no-store" in response.headers["Cache-Control"]
    for suffix in ("?period=999", "?mode=wrong", "?mode=test&mode=live"):
        assert client.get("/owner/" + suffix).status_code == 400
        assert client.get("/owner/agents/operations" + suffix).status_code == 400
    for suffix in ("?feature=secret", "?status=oops", "?billing=oops", "?telegram=-1", "?page=-1"):
        assert client.get("/owner/agents/operations" + suffix).status_code == 400
    with Session(engine) as db:
        assert (
            list(db.execute(select(AIOperation.id, AIOperation.status, AIOperation.cost_rub)))
            == before
        )
    client.get("/owner/login")
    # A separate, unauthenticated browser cannot read owner reports.
    anonymous = client.application.test_client()
    assert anonymous.get("/owner/").status_code == 302
    assert anonymous.get("/owner/agents/operations").status_code == 302


@pytest.mark.integration
def test_owner_overview_render_snapshots(admin_fixture):
    """Synthetic pages for the isolated preview; never adds a production bypass."""
    import re

    from sqlalchemy import delete
    from test_admin_postgres import RENDER_ASSETS, RENDER_SNAPSHOTS

    client, _, config, fixture, _ = admin_fixture
    engine, _, _, owners, *_ = fixture
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    report = window("7", "test")
    ids = []
    try:
        with Session(engine) as db, db.begin():
            for changes in (
                {},
                {
                    "billing_mode": "unlimited",
                    "status": "failed",
                    "snapshot": {"purpose": "content"},
                },
                {
                    "kind": "search",
                    "model": "zenstat",
                    "snapshot": {"purpose": "dzen_search"},
                    "status": "uncertain",
                    "cost_rub": Decimal("0"),
                },
            ):
                row = operation(owners[0], bot, report, **changes)
                db.add(row)
                db.flush()
                ids.append(row.id)
        for name, path in (
            ("home", "/owner/"),
            ("ai-operations", "/owner/agents/operations?period=7&mode=test"),
            ("ai-empty", "/owner/agents/operations?telegram=99999999999"),
        ):
            response = client.get(path)
            assert response.status_code == 200
            body = response.get_data()
            assert b"PRIVATE_BRIEF" not in body
            assert b">None<" not in body
            RENDER_SNAPSHOTS[name] = body
            for encoded in re.findall(rb'(?:src|href)="(/(?:owner/)?static/[^"?]+)', body):
                url = encoded.decode()
                asset = client.get(url)
                assert asset.status_code == 200
                RENDER_ASSETS[url] = asset.get_data(), asset.content_type
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(AIOperation).where(AIOperation.id.in_(ids)))
