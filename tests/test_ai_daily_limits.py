"""Lifetime per-agent trials instead of daily caps."""

# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session
from test_dzen_agent import ai_fixture  # noqa: F401
from test_image_agent import article, payload, result
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import agents, ai_tokens
from leedbox.infrastructure.agents import AgentError


def enable_trial(engine, owner, bot):
    with Session(engine) as db, db.begin():
        funds = ai_tokens.account(db, owner, bot, True)
        funds.balance = 0
        funds.free_limits = {"dzen": 1, "image_prompts": 1}


def test_free_trial_per_agent_owner_bot_and_environment(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    enable_trial(engine, owners[1], bot)
    with Session(engine) as db:
        offer = agents.quote_images(db, article())
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "image_prompts") == 1
        assert ai_tokens.free_remaining(db, owners[2], bot, True, "image_prompts") == 0
        assert ai_tokens.free_remaining(db, owners[1], bot + 1, True, "image_prompts") == 0
        assert ai_tokens.free_remaining(db, owners[1], bot, False, "image_prompts") == 0
    with patch.object(agents, "deepseek_generate", return_value=payload(result())) as api:
        number = agents.generate(engine, config, owners[1], bot, "free", offer)
        assert agents.generate(engine, config, owners[1], bot, "free", offer) == number
        with pytest.raises(ValueError, match="Пополните баланс"):
            agents.generate(engine, config, owners[1], bot, "paid", offer)
        api.assert_called_once()
    with Session(engine) as db:
        row = agents.operation(db, owners[1], bot, True, number)
        assert row.billing_mode == "trial" and row.input_tokens > 0
        assert ai_tokens.available(db, owners[1], bot, True) == 0
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "dzen") == 1
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "writing_profile") == 0


def test_invalid_result_returns_trial_and_unknown_holds_it(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    enable_trial(engine, owners[1], bot)
    with Session(engine) as db:
        offer = agents.quote_images(db, article())
    with (
        patch.object(agents, "deepseek_generate", return_value=payload({"images": []})),
        pytest.raises(ValueError, match="неподходящий"),
    ):
        agents.generate(engine, config, owners[1], bot, "invalid", offer)
    with Session(engine) as db:
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "image_prompts") == 1
    with patch.object(agents, "deepseek_generate", side_effect=AgentError("fixture")) as api:
        with pytest.raises(ValueError, match="Резерв сохранён"):
            agents.generate(engine, config, owners[1], bot, "unknown", offer)
        with pytest.raises(ValueError, match="Пополните баланс"):
            agents.generate(engine, config, owners[1], bot, "another", offer)
        api.assert_called_once()
    with Session(engine) as db:
        assert ai_tokens.account(db, owners[1], bot, True).reserved == 0


@pytest.mark.parametrize("environment", ["development", "production"])
def test_owner_unlimited_no_balance_or_daily_cap(ai_fixture, environment):
    engine, config, actors, owners, bot = ai_fixture
    owner_config = config.model_copy(
        update={"environment": environment, "owner_ids": frozenset({actors[1]})}
    )
    with Session(engine) as db:
        offer = agents.quote_images(db, article())
    with patch.object(agents, "deepseek_generate", return_value=payload(result())):
        for i in range(12):
            number = agents.generate(engine, owner_config, owners[1], bot, f"owner-{i}", offer)
    with Session(engine) as db:
        row = agents.operation(db, owners[1], bot, environment != "production", number)
        assert row.billing_mode == "unlimited" and row.cost_rub > 0
        assert ai_tokens.available(db, owners[1], bot, True) == 50000
        assert ai_tokens.available(db, owners[1], bot, False) == 0


def test_single_trial_concurrent_different_keys(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    enable_trial(engine, owners[1], bot)
    with Session(engine) as db:
        offer = agents.quote_images(db, article())

    def run(i):
        try:
            return agents.generate(engine, config, owners[1], bot, f"race-{i}", offer)
        except ValueError:
            return None

    with patch.object(agents, "deepseek_generate", return_value=payload(result())) as api:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, range(2)))
        assert sum(item is not None for item in results) == 1
        api.assert_called_once()


def test_manual_unlimited_can_be_revoked(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    enable_trial(engine, owners[1], bot)
    with Session(engine) as db, db.begin():
        funds = ai_tokens.account(db, owners[1], bot, True)
        funds.unlimited = True
        funds.free_limits = {"image_prompts": 0}
        offer = agents.quote_images(db, article())
    with patch.object(agents, "deepseek_generate", return_value=payload(result())):
        number = agents.generate(engine, config, owners[1], bot, "manual", offer)
    with Session(engine) as db, db.begin():
        funds = ai_tokens.account(db, owners[1], bot, True)
        funds.unlimited = False
        assert agents.operation(db, owners[1], bot, True, number).billing_mode == "unlimited"
    with patch.object(agents, "deepseek_generate") as api:
        with pytest.raises(ValueError, match="Пополните баланс"):
            agents.generate(engine, config, owners[1], bot, "revoked", offer)
        api.assert_not_called()


def test_free_confirmation_cannot_silently_turn_paid(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    with Session(engine) as db, db.begin():
        funds = ai_tokens.account(db, owners[1], bot, True)
        funds.free_limits = {"image_prompts": 1}
        first = agents.quote_images(db, article())
        second = agents.quote_images(db, article())
        assert agents.payment_preview(db, config, owners[1], bot, first)[0]
        assert agents.payment_preview(db, config, owners[1], bot, second)[0]
    with patch.object(agents, "deepseek_generate", return_value=payload(result())) as api:
        agents.generate(
            engine, config, owners[1], bot, "first-free", first, require_confirmation=True
        )
        with pytest.raises(ValueError, match="Способ оплаты изменился"):
            agents.generate(
                engine, config, owners[1], bot, "stale-free", second, require_confirmation=True
            )
        api.assert_called_once()
    with Session(engine) as db:
        assert ai_tokens.available(db, owners[1], bot, True) == 50000
