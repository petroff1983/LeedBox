"""Owner control panel acceptance on synthetic accounts only."""
# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

from datetime import UTC, datetime, timedelta
from secrets import randbelow
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_admin_postgres import admin_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import ai_tokens
from leedbox.application.profile import delete_my_data
from leedbox.infrastructure.models import AIAccount, AIOperation, AuditLog, Setting, User
from leedbox.web.app import create_app


@pytest.mark.integration
def test_owner_ai_pages_grant_and_release_all_funding_modes(admin_fixture):
    client, csrf, config, fixture, _ = admin_fixture
    engine, _, actors, owners, *_ = fixture
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    public = create_app(config, engine).test_client()
    paths = [
        "/owner/agents/",
        "/owner/agents/access",
        "/owner/agents/grant",
        "/owner/agents/operations",
        "/owner/agents/uncertain",
    ]
    for path in paths:
        assert public.get(path).status_code == 302
        assert client.get(path).status_code == 200
    status_key = f"provider_status:{bot}"
    numbers = []
    try:
        with Session(engine) as db, db.begin():
            db.add(
                Setting(
                    key=status_key,
                    value={
                        "checked_at": datetime.now(UTC).isoformat(),
                        "balances": {"USD": "10.00"},
                        "available": True,
                    },
                )
            )
            ai_tokens.grant(db, owners[1], bot, True, "qa-admin-initial", 3000)
            for mode in ("balance", "trial", "unlimited"):
                row = ai_tokens.reserve(
                    db,
                    owners[1],
                    bot,
                    True,
                    "qa-uncertain-" + mode,
                    100,
                    {"model": "qa-model", "purpose": "image_prompts"},
                    mode,
                )
                row.status = "uncertain"
                row.created_at = datetime.now(UTC) - timedelta(minutes=6)
                numbers.append(row.id)
        assert "10.00 USD" in client.get(paths[0]).text
        assert (
            client.post(paths[-1], data={"number": str(numbers[0]), "confirmed": "1"}).status_code
            == 403
        )
        for number in numbers:
            data = {"csrf_token": csrf, "confirmed": "1", "number": str(number)}
            assert client.post(paths[-1], data=data).status_code == 302
            assert client.post(paths[-1], data=data).status_code == 409
        with Session(engine) as db:
            funds = db.get(AIAccount, (owners[1], bot, True))
            assert funds.balance == 3000 and funds.reserved == 0
            assert all(db.get(AIOperation, number).status == "failed" for number in numbers)
            assert db.scalar(
                select(AuditLog.id).where(
                    AuditLog.action == "ai_reservation_released_at_service_expense"
                )
            )
        data = {
            "csrf_token": csrf,
            "confirmed": "1",
            "telegram": str(actors[1]),
            "tokens": "500",
            "reason": "Приёмка ручного начисления",
            "key": uuid4().hex,
        }
        for _ in range(2):
            assert client.post("/owner/agents/grant", data=data).status_code == 302
        with Session(engine) as db:
            assert ai_tokens.available(db, owners[1], bot, True) == 3500
    finally:
        with Session(engine) as db, db.begin():
            db.execute(
                delete(AIOperation).where(
                    AIOperation.owner_user_id.in_(owners), AIOperation.bot_id == bot
                )
            )
            db.execute(
                delete(AIAccount).where(
                    AIAccount.owner_user_id.in_(owners), AIAccount.bot_id == bot
                )
            )
            db.execute(delete(Setting).where(Setting.key == status_key))


@pytest.mark.integration
def test_owner_adds_expert_and_controls_free_and_unlimited(admin_fixture):
    client, csrf, config, fixture, _ = admin_fixture
    engine = fixture[0]
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    telegram = 2_100_000_000 + randbelow(10_000_000)
    path = "/owner/agents/access"
    data = {
        "csrf_token": csrf,
        "confirmed": "1",
        "telegram": str(telegram),
        "unlimited": "1",
        "dzen": "2",
        "image_prompts": "3",
        "reason": "Приёмка добавления эксперта",
        "expected": "",
    }
    try:
        assert client.post(path, data={**data, "image_prompts": "-1"}).status_code == 200
        with Session(engine) as db:
            assert db.scalar(select(User.id).where(User.telegram_id == telegram)) is None
        assert client.post(path, data=data).status_code == 302
        with Session(engine) as db:
            user = db.scalar(select(User).where(User.telegram_id == telegram))
            assert user is not None and user.expert_started_at is not None
            funds = db.get(AIAccount, (user.id, bot, True))
            assert funds.unlimited and funds.free_limits == {"dzen": 2, "image_prompts": 3}
            assert db.get(AIAccount, (user.id, bot, False)) is None
            assert ai_tokens.funding(db, config, user.id, bot, True, {}) == "unlimited"
            assert ai_tokens.free_remaining(db, user.id, bot, True, "dzen") == 2
            assert ai_tokens.free_remaining(db, user.id, bot, True, "image_prompts") == 3
        assert client.get(path + f"?telegram={telegram}").status_code == 200
    finally:
        with Session(engine) as db, db.begin():
            delete_my_data(db, telegram)
