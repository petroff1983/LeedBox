# ruff: noqa: F811
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_admin_postgres import admin_fixture  # noqa: F401
from test_dzen_agent import ai_fixture  # noqa: F401
from test_image_agent import article
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import agents, ai_tokens, provider_balance
from leedbox.application.versions import fingerprint
from leedbox.infrastructure.agents import AgentError
from leedbox.infrastructure.models import AIAccount, AuditLog, OutboxMessage, RateBucket, Setting


def test_provider_low_balance_deduplicated_owner_only(ai_fixture):
    engine, config, actors, _, bot = ai_fixture
    key = f"provider_status:{bot}"
    with Session(engine) as db:
        old = db.get(Setting, key)
        saved = old.value if old else None
    try:
        with patch.object(
            provider_balance,
            "request_json",
            return_value={
                "is_available": True,
                "balance_infos": [{"currency": "USD", "total_balance": "0.50"}],
            },
        ) as api:
            provider_balance.check(engine, config, bot)
            provider_balance.check(engine, config, bot)
            api.assert_called_once()
        with Session(engine) as db:
            alerts = list(
                db.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.bot_id == bot, OutboxMessage.purpose == "provider_alert"
                    )
                )
            )
            assert len(alerts) == 1 and alerts[0].telegram_user_id == actors[0]
            assert "0.50 USD" in alerts[0].text
            assert db.get(Setting, key).value["balances"] == {"USD": "0.50"}
    finally:
        with Session(engine) as db, db.begin():
            db.execute(
                delete(OutboxMessage).where(
                    OutboxMessage.bot_id == bot, OutboxMessage.purpose == "provider_alert"
                )
            )
            db.execute(delete(RateBucket).where(RateBucket.key.startswith("provider:")))
            db.execute(delete(Setting).where(Setting.key == key))
            if saved is not None:
                db.add(Setting(key=key, value=saved))


def test_provider_empty_balance_refunds_free_attempt(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    with Session(engine) as db, db.begin():
        funds = ai_tokens.account(db, owners[1], bot, True)
        funds.free_limits = {"image_prompts": 1}
        offer = agents.quote_images(db, article())
    try:
        with (
            patch.object(agents, "deepseek_generate", side_effect=AgentError("provider", 402)),
            pytest.raises(ValueError, match="Владелец уведомлён"),
        ):
            agents.generate(engine, config, owners[1], bot, "empty-provider", offer)
        with Session(engine) as db:
            assert ai_tokens.free_remaining(db, owners[1], bot, True, "image_prompts") == 1
            assert ai_tokens.available(db, owners[1], bot, True) == 50000
    finally:
        with Session(engine) as db, db.begin():
            db.execute(
                delete(OutboxMessage).where(
                    OutboxMessage.bot_id == bot, OutboxMessage.purpose == "provider_alert"
                )
            )
            db.execute(delete(RateBucket).where(RateBucket.key.startswith("provider:")))


def test_search_one_free_then_fixed_price_cached_no_charge(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    with Session(engine) as db, db.begin():
        ai_tokens.account(db, owners[1], bot, True).free_limits = {"dzen": 1}
    posts = [{"title": "Растения", "url": "https://dzen.ru/a/test"}]
    with patch.object(agents, "dzen_posts", return_value=posts) as api:
        first = agents.stats(
            engine, config, owners[1], bot, "https://dzen.ru/example", "1", "search1", 1000
        )
        assert (
            agents.stats(
                engine, config, owners[1], bot, "https://dzen.ru/example", "1", "search1", 1000
            )
            == first
        )
        with Session(engine) as db:
            assert ai_tokens.available(db, owners[1], bot, True) == 50000
        agents.stats(
            engine, config, owners[1], bot, "https://dzen.ru/example", "1", "search2", 1000
        )
        with Session(engine) as db:
            assert ai_tokens.available(db, owners[1], bot, True) == 49000
            assert ai_tokens.free_remaining(db, owners[1], bot, True, "dzen") == 1
        assert api.call_count == 2


def test_admin_access_csrf_audit_and_stale_form(admin_fixture):
    client, csrf, config, fixture, _ = admin_fixture
    engine, _, actors, owners, *_ = fixture
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    url = "/owner/agents/access"
    assert client.get(url).status_code == 200
    page = client.get(url + f"?telegram={actors[1]}")
    assert "Бесплат" in page.text or "бесплат" in page.text
    data = {
        "telegram": str(actors[1]),
        "unlimited": "1",
        "dzen": "3",
        "image_prompts": "2",
        "reason": "Тестовый доступ",
        "confirmed": "1",
        "expected": fingerprint({"unlimited": False, "free_limits": {}}),
    }
    assert client.post(url, data=data).status_code == 403
    data["csrf_token"] = csrf
    try:
        assert client.post(url, data=data).status_code == 302
        with Session(engine) as db:
            funds = db.get(AIAccount, (owners[1], bot, True))
            assert funds.unlimited and funds.free_limits == {"dzen": 3, "image_prompts": 2}
            assert db.scalar(
                select(AuditLog.id).where(
                    AuditLog.action == "ai_access_changed", AuditLog.subject_user_id == owners[1]
                )
            )
        response = client.post(url, data={**data, "dzen": "5"})
        assert response.status_code == 200 and "Доступ изменился" in response.text
    finally:
        with Session(engine) as db, db.begin():
            db.execute(
                delete(AIAccount).where(
                    AIAccount.owner_user_id == owners[1], AIAccount.bot_id == bot
                )
            )
