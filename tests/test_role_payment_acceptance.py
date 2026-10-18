"""Synthetic checkout-to-AI acceptance, no provider or Telegram network requests."""
# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.

from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.orm import Session
from test_dzen_agent import result as headline_result
from test_image_agent import article, payload
from test_image_agent import result as image_result
from test_payments_postgres import payments_fixture  # noqa: F401
from test_purchasing import purchasing_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401
from test_wallet import topup, wallet_fixture  # noqa: F401

from leedbox.application import agents, ai_tokens, wallet
from leedbox.application.payment_operations import confirm_refund
from leedbox.application.settings import set_setting
from leedbox.domain.payments import signature
from leedbox.infrastructure.models import AIAccount, AIOperation, Setting
from leedbox.web.app import create_app


@pytest.mark.integration
def test_topup_package_trials_paid_usage_owner_and_refund_debt(wallet_fixture):
    engine, config, actors, owners, bot = wallet_fixture
    config.deepseek_api_key = SecretStr("synthetic-no-network-key")
    values = {
        "ai_dzen_enabled": True,
        "ai_images_enabled": True,
        "ai_free_dzen": 1,
        "ai_free_image_prompts": 1,
        "ai_token_packages": [
            {"id": "qa", "tokens": 50000, "price_rub": "100.00", "enabled": True}
        ],
    }
    with Session(engine) as db, db.begin():
        saved = {key: row.value for key in values if (row := db.get(Setting, key)) is not None}
        for key, value in values.items():
            set_setting(db, config, actors[0], key, value)
    try:
        number = topup(wallet_fixture)
        client = create_app(config, engine).test_client()
        custom = {"Shp_bot": str(bot), "Shp_mode": "test"}
        form = {"OutSum": "1000.00", "InvId": str(number), **custom}
        form["SignatureValue"] = signature(
            [form["OutSum"], str(number)], "fixture-pass2", custom, "md5"
        )
        assert (
            client.post(
                "/payments/robokassa/result", data={**form, "OutSum": "9000.00"}
            ).status_code
            == 400
        )
        with Session(engine) as db:
            assert wallet.balance(db, owners[1], bot, True) == 0
        for _ in range(2):
            assert client.post("/payments/robokassa/result", data=form).text == f"OK{number}"
        with Session(engine) as db, db.begin():
            package = agents.package_offer(db, config, "qa")
            first = agents.buy_package(db, config, owners[1], bot, "qa-package", package)
            assert (
                agents.buy_package(db, config, owners[1], bot, "qa-package", package).id == first.id
            )
            assert wallet.balance(db, owners[1], bot, True) == 900
            assert wallet.balance(db, owners[2], bot, True) == 0
            assert ai_tokens.available(db, owners[1], bot, True) == 50000
            image_offer = agents.quote_images(db, article())
            title_offer = agents.quote_generation(
                db, {"title": "Домашние растения"}, "Для начинающих", []
            )

        with patch.object(agents, "deepseek_generate", return_value=payload(image_result())) as api:
            free_image = agents.generate(
                engine, config, owners[1], bot, "qa-image-trial", image_offer
            )
            paid_image = agents.generate(
                engine, config, owners[1], bot, "qa-image-paid", image_offer
            )
            assert (
                agents.generate(engine, config, owners[1], bot, "qa-image-paid", image_offer)
                == paid_image
            )
            owner_image = agents.generate(engine, config, owners[0], bot, "qa-owner", image_offer)
            assert api.call_count == 3
        with patch.object(agents, "deepseek_generate", return_value=payload(headline_result())):
            free_title = agents.generate(
                engine, config, owners[1], bot, "qa-title-trial", title_offer
            )
            paid_title = agents.generate(
                engine, config, owners[1], bot, "qa-title-paid", title_offer
            )
        with Session(engine) as db:
            rows = [
                agents.operation(db, owners[1], bot, True, item)
                for item in (free_image, paid_image, free_title, paid_title)
            ]
            assert [row.billing_mode for row in rows] == ["trial", "balance", "trial", "balance"]
            spent = sum(row.input_tokens + row.output_tokens for row in (rows[1], rows[3]))
            assert ai_tokens.available(db, owners[1], bot, True) == 50000 - spent
            assert ai_tokens.available(db, owners[2], bot, True) == 0
            assert ai_tokens.available(db, owners[1], bot, False) == 0
            assert ai_tokens.available(db, owners[0], bot, True) == 0
            assert (
                agents.operation(db, owners[0], bot, True, owner_image).billing_mode == "unlimited"
            )
            assert ai_tokens.free_remaining(db, owners[2], bot, True, "dzen") == 1
            assert ai_tokens.free_remaining(db, owners[2], bot, True, "image_prompts") == 1

        with Session(engine) as db, db.begin():
            for _ in range(2):
                confirm_refund(
                    db,
                    number=number,
                    bot_id=bot,
                    provider="robokassa",
                    account=config.robokassa_merchant,
                    test_mode=True,
                    amount=Decimal("1000"),
                    charge_id=str(number),
                )
            assert wallet.balance(db, owners[1], bot, True) == -100
        with patch.object(agents, "deepseek_generate") as api:
            with pytest.raises(ValueError, match="задолженность"):
                agents.generate(engine, config, owners[1], bot, "qa-after-refund", image_offer)
            api.assert_not_called()
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
            for key in values:
                db.execute(delete(Setting).where(Setting.key == key))
                if key in saved:
                    db.add(Setting(key=key, value=saved[key]))
