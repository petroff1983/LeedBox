# ruff: noqa: F811
"""Money paths use only isolated PostgreSQL and synthetic provider responses."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_admin_postgres import (
    RENDER_ASSETS,
    RENDER_SNAPSHOTS,
    admin_fixture,  # noqa: F401
)
from test_content_agent_postgres import response, tick
from test_dzen_agent import ai_fixture  # noqa: F401
from test_image_agent import article
from test_image_agent import payload as image_payload
from test_image_agent import result as image_result
from test_lead_magnet_agent import critique, magnets
from test_lead_magnet_agent_postgres import ready
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import (
    agents,
    ai_tokens,
    credits,
    finance,
    provider_limits,
    youtube_billing,
)
from leedbox.application import content_agent as content_tasks
from leedbox.application import lead_magnet_agent as lm
from leedbox.application.owner_overview import window
from leedbox.application.profile import delete_my_data
from leedbox.application.settings import set_setting
from leedbox.infrastructure import apify
from leedbox.infrastructure.agents import AgentError
from leedbox.infrastructure.models import (
    AIAccount,
    AIOperation,
    ContentTask,
    Setting,
    WalletAccount,
)
from leedbox.infrastructure.youtube import YouTubeError


@pytest.fixture
def credit_fixture(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    keys = [
        "ai_credits_enabled",
        "ai_credit_price_rub",
        "ai_content_enabled",
        "ai_input_usd_per_million",
        "ai_output_usd_per_million",
        "ai_usd_rub",
        "finance_fee_percent",
        "finance_tax_percent",
        "finance_server_monthly_rub",
    ]
    keys.extend("ai_credit_cost_" + key for key in credits.TARIFFS)
    with Session(engine) as db, db.begin():
        saved = {k: row.value for k in keys if (row := db.get(Setting, k))}
        db.execute(delete(Setting).where(Setting.key.in_(keys)))
        # Tests of new operations do not implicitly perform a global legacy conversion.
        db.add(Setting(key="ai_credits_enabled", value=True))
        db.add(Setting(key="ai_content_enabled", value=True))
        ai_tokens.grant(
            db, owners[1], bot, True, "credits-initial", 200, snapshot={"unit": "credits"}
        )
    try:
        yield ai_fixture
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(ContentTask).where(ContentTask.owner_user_id.in_(owners)))
            for funds in db.scalars(select(AIAccount).where(AIAccount.owner_user_id.in_(owners))):
                funds.credit_balance = funds.credit_reserved = 0
                funds.credit_value_rub = Decimal(0)
            db.execute(delete(Setting).where(Setting.key.in_(keys)))
            for key, value in saved.items():
                db.add(Setting(key=key, value=value))


def offer(db, task="image_prompts"):
    return credits.quoted_value(
        db,
        {
            "purpose": task,
            "model": "synthetic",
            "maximum": 6000,
            "rates": {"input": "0.30", "output": "1.32", "usd_rub": "100"},
        },
    )


USAGE = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


@pytest.mark.parametrize(
    "task,expected", [(key, price) for key, (_, price) in credits.TARIFFS.items()]
)
def test_fixed_prices(credit_fixture, task, expected):
    engine, _, _, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        quoted = offer(db, task)
        assert credits.amount(quoted) == expected
        row = ai_tokens.reserve(db, owners[1], bot, True, "fixed-price-" + task, 10000, quoted)
        operation_id = row.id
    with Session(engine) as db, db.begin():
        row = ai_tokens.settle(db, owners[1], bot, True, operation_id, USAGE, {}, True)
        assert row.charged_credits == expected
        assert ai_tokens.available(db, owners[1], bot, True) == 200 - expected


@pytest.mark.parametrize("valid,charged", [(True, 1), (False, 0)])
def test_actual_usage_not_charged_as_credits_and_idempotency(credit_fixture, valid, charged):
    engine, _, _, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        quoted = offer(db)
        row = ai_tokens.reserve(db, owners[1], bot, True, "credit-step", 10000, quoted)
        number = row.id
        assert ai_tokens.available(db, owners[1], bot, True) == 199
    with Session(engine) as db, db.begin():
        row = ai_tokens.settle(db, owners[1], bot, True, number, USAGE, {"ok": True}, valid)
        ai_tokens.settle(db, owners[1], bot, True, number, USAGE, {"ok": True}, valid)
        assert row.charged_credits == charged
        assert row.input_tokens + row.output_tokens == 150
        assert row.cost_rub == Decimal("0.009600")
        assert ai_tokens.available(db, owners[1], bot, True) == 200 - charged
        assert ai_tokens.previous(db, owners[1], bot, True, "credit-step").id == number


def test_free_and_owner_unlimited_do_not_consume_credit_balance(credit_fixture):
    engine, config, _, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        for index, mode in ((0, "unlimited"), (1, "trial")):
            quoted = offer(db)
            row = ai_tokens.reserve(
                db, owners[index], bot, True, f"free-credit-{index}", 10000, quoted, mode
            )
            ai_tokens.settle(db, owners[index], bot, True, row.id, USAGE, {"ok": True}, True)
            assert row.charged_credits == 0 and row.revenue_rub == 0
        assert ai_tokens.funding(db, config, owners[0], bot, True, offer(db)) == "unlimited"
        assert ai_tokens.available(db, owners[1], bot, True) == 200
        assert ai_tokens.available(db, owners[2], bot, True) == 0


def test_concurrent_reservations_do_not_overdraw(credit_fixture):
    engine, config, actors, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        set_setting(db, config, actors[0], "ai_credit_cost_image_prompts", 150)

    def reserve(key):
        try:
            with Session(engine) as db, db.begin():
                ai_tokens.reserve(db, owners[1], bot, True, key, 10000, offer(db))
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ["race-a", "race-b"])) == [False, True]
    with Session(engine) as db, db.begin():
        row = db.scalar(
            select(AIOperation).where(
                AIOperation.owner_user_id == owners[1], AIOperation.status == "reserved"
            )
        )
        ai_tokens.release_reservation(ai_tokens.account(db, owners[1], bot, True), row)
        row.status = "failed"


def test_frozen_task_price_survives_setting_change(credit_fixture):
    engine, config, actors, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        row = ai_tokens.reserve(db, owners[1], bot, True, "frozen-credit-price", 10000, offer(db))
        number = row.id
    with Session(engine) as db, db.begin():
        set_setting(db, config, actors[0], "ai_credit_cost_image_prompts", 99)
        set_setting(db, config, actors[0], "ai_credit_price_rub", "2.00")
    with Session(engine) as db, db.begin():
        assert credits.amount(offer(db)) == 99
        row = ai_tokens.settle(db, owners[1], bot, True, number, USAGE, {}, True)
        assert row.charged_credits == 1


def test_wallet_purchase_rounding_and_frozen_value(credit_fixture):
    engine, config, actors, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        db.add(
            WalletAccount(
                owner_user_id=owners[1], bot_id=bot, test_mode=True, balance=Decimal("500")
            )
        )
    with Session(engine) as db, db.begin():
        quoted = agents.token_offer(db, config, Decimal("200.50"))
        assert quoted["tokens"] == 200 and Decimal(quoted["price_rub"]) == 200
        op = agents.buy_tokens(db, config, owners[1], bot, "credits-purchase", quoted)
        assert agents.buy_tokens(db, config, owners[1], bot, "credits-purchase", quoted).id == op.id
        assert db.get(WalletAccount, (owners[1], bot, True)).balance == 300
        # Initial bonus: 200 credits with zero value; purchase: 200 credits worth 200 RUB.
        assert ai_tokens.available(db, owners[1], bot, True) == 400
    with Session(engine) as db, db.begin():
        set_setting(db, config, actors[0], "ai_credit_price_rub", "5.00")
    with Session(engine) as db, db.begin():
        row = ai_tokens.reserve(db, owners[1], bot, True, "spend-purchased", 10000, offer(db))
        ai_tokens.settle(db, owners[1], bot, True, row.id, USAGE, {}, True)
        assert row.revenue_rub == Decimal("0.500000")  # Carrying value of one spent credit.
        assert db.get(AIAccount, (owners[1], bot, True)).credit_value_rub == Decimal("199.500000")
        with pytest.raises(ValueError, match="Цена"):
            agents.buy_tokens(db, config, owners[1], bot, "stale-credit-price", quoted)


def test_credit_balance_protects_delete_and_units_are_separate(credit_fixture):
    engine, _, actors, owners, bot = credit_fixture
    with Session(engine) as db:
        funds = db.get(AIAccount, (owners[1], bot, True))
        assert funds.balance == 50000 and funds.credit_balance == 200
        assert ai_tokens.available(db, owners[1], bot, False) == 0
        funds.balance = 0
        with pytest.raises(ValueError, match="остаток"):
            delete_my_data(db, actors[1])
        db.rollback()


def test_content_batch_price_is_per_post(credit_fixture):
    engine, *_ = credit_fixture
    with Session(engine) as db:
        raw = {
            "purpose": "content",
            "maximum": 120000,
            "content_phase": "generation",
            "requests": [{"topic": "Маркетинг", "count": 3}, {"topic": "Бизнес", "count": 2}],
        }
        assert credits.amount(credits.quoted_value(db, raw)) == 5


def test_legacy_quote_cannot_debit_new_credit_balance(credit_fixture):
    engine, _, _, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        legacy = {"purpose": "dzen", "model": "synthetic", "maximum": 10000}
        with pytest.raises(ValueError, match="Единицы"):
            ai_tokens.reserve(db, owners[1], bot, True, "old-quote", 10000, legacy)


def test_api_costs_include_free_failed_and_unlimited(credit_fixture):
    engine, _, _, owners, bot = credit_fixture
    with Session(engine) as db:
        before = finance.summary(db, bot, window("all", "test"))
    with Session(engine) as db, db.begin():
        for index, mode in enumerate(("unlimited", "trial", "balance")):
            row = ai_tokens.reserve(
                db, owners[1], bot, True, f"profit-{index}", 10000, offer(db), mode
            )
            ai_tokens.settle(db, owners[1], bot, True, row.id, USAGE, {}, index != 2)
    with Session(engine) as db:
        after = finance.summary(db, bot, window("all", "test"))
        assert after["api"] - before["api"] == Decimal("0.028800")
        assert after["ai_revenue"] == before["ai_revenue"]


@pytest.mark.parametrize("platform", ["telegram"])
def test_apify_20_is_provider_input_not_just_output_filter(platform):
    url = "https://t.me/example" if platform == "telegram" else "https://www.threads.com/@example"
    with patch.object(
        apify, "request", return_value={"data": {"id": "syntheticRun123"}}
    ) as request:
        apify.start("synthetic", platform, url, 30, "0.12", competitor=True)
    path, body = request.call_args.args[1:]
    assert parse_qs(urlsplit(path).query)["maxItems"] == ["20"]
    if platform == "telegram":
        assert "automation-lab~telegram-scraper" in path
        assert body["maxMessages"] == 20 and body["includeChannelInfo"] is False


def test_apify_event_charge_arrives_before_final_usage_total():
    data = {
        "usageTotalUsd": 0,
        "chargedEventCounts": {"start": 1, "message": 20},
        "pricingInfo": {
            "pricingModel": "PAY_PER_EVENT",
            "pricingPerEvent": {
                "actorChargeEvents": {
                    "start": {"eventPriceUsd": 0.005},
                    "message": {"eventPriceUsd": 0.001},
                }
            },
        },
    }
    assert apify.run_cost(data) == Decimal("0.025")
    data["usageTotalUsd"] = 0.025
    assert apify.run_cost(data) == Decimal("0.025")  # Not .05: no double-count.


def test_apify_ranks_ten_of_twenty():
    rows = [
        {"date": datetime.now(UTC).isoformat(), "url": f"https://t.me/example/{i}", "views": i}
        for i in range(20)
    ]
    posts = apify.normalized(rows, "telegram", "https://t.me/example", 30, links_only=True)
    assert len(posts) == 10 and [p["rank"] for p in posts] == list(range(19, 9, -1))


def test_zenstat_actual_balance_zero_and_expiry_not_invented():
    expiry = (datetime.now(UTC) + timedelta(days=4)).isoformat()
    data = {
        "status": True,
        "response": {"packages": [{"balance": 0, "type": "common", "date_end": expiry}]},
    }
    assert provider_limits.zenstat_packages(data) == data["response"]["packages"]
    for bad in (None, -1, "100", True):
        data["response"]["packages"][0]["balance"] = bad
        with pytest.raises(ValueError):
            provider_limits.zenstat_packages(data)


def test_apify_limit_is_not_wallet_balance():
    result = provider_limits.apify_limits(
        {
            "data": {
                "limits": {"maxMonthlyUsageUsd": 10},
                "current": {"monthlyUsageUsd": 3},
                "monthlyUsageCycle": {"endAt": "2026-11-01"},
            }
        }
    )
    assert result["remaining_usd"] == "7" and "balance" not in result


def test_finance_admin_get_no_provider_calls_and_csrf(admin_fixture):
    client, csrf, _, fixture, _ = admin_fixture
    engine = fixture[0]
    with (
        patch.object(provider_limits, "refresh") as refresh,
        patch("leedbox.application.provider_balance.check") as check,
    ):
        for path in ("/owner/finance/", "/owner/finance/prices", "/owner/finance/providers"):
            response = client.get(path)
            assert response.status_code == 200 and "API" in response.text
        price_page = client.get("/owner/finance/prices")
        assert "/owner/settings/edit?key=ai_credit_cost_lead_magnet" in price_page.text
        edit_path = "/owner/settings/edit?key=ai_credit_cost_lead_magnet"
        editor = client.get(edit_path)
        assert editor.status_code == 200 and 'name="value"' in editor.text
        expected = re.search(rb'name="expected" value="([^"]+)"', editor.data).group(1).decode()
        assert (
            client.post(
                edit_path,
                data={"csrf_token": csrf, "expected": expected, "value": "2", "confirmed": "1"},
            ).status_code
            == 302
        )
        with Session(engine) as db:
            assert db.get(Setting, "ai_credit_cost_lead_magnet").value == 2
        editor = client.get(edit_path)
        expected = re.search(rb'name="expected" value="([^"]+)"', editor.data).group(1).decode()
        assert (
            client.post(
                edit_path,
                data={"csrf_token": csrf, "expected": expected, "value": "1", "confirmed": "1"},
            ).status_code
            == 302
        )
        refresh.assert_not_called()
        check.assert_not_called()
        assert client.post("/owner/finance/providers", data={"confirmed": "1"}).status_code == 403
        response = client.post(
            "/owner/finance/providers", data={"csrf_token": csrf, "confirmed": "1"}
        )
        assert response.status_code == 302
        refresh.assert_called_once()
        check.assert_called_once()


def test_actual_image_agent_uses_fixed_credit_price(credit_fixture):
    engine, config, _, owners, bot = credit_fixture
    with Session(engine) as db:
        quoted = agents.quote_images(db, article())
        enough, preview = agents.payment_preview(db, config, owners[1], bot, quoted)
        assert enough and "1 кредит" in preview
    with patch.object(
        agents, "deepseek_generate", return_value=image_payload(image_result())
    ) as api:
        number = agents.generate(engine, config, owners[1], bot, "actual-credit-images", quoted)
        assert (
            agents.generate(engine, config, owners[1], bot, "actual-credit-images", quoted)
            == number
        )
        api.assert_called_once()
    with Session(engine) as db:
        row = db.get(AIOperation, number)
        assert row.charged_credits == 1 and row.input_tokens + row.output_tokens == 1200
        assert ai_tokens.available(db, owners[1], bot, True) == 199


def test_actual_lm_then_critic_is_two_fixed_actions(credit_fixture):
    engine, config, _, owners, bot = credit_fixture
    token = ready(credit_fixture)
    with patch.object(
        lm,
        "deepseek_generate",
        side_effect=[response(magnets()), response(critique()), response(magnets("Улучшенный"))],
    ):
        tick(credit_fixture)
        with Session(engine) as db, db.begin():
            row = content_tasks.owned(db, config, owners[1], bot, token)
            assert db.get(AIOperation, row.operation_id).charged_credits == 1
            quoted, digest, text, enough = lm.confirmation(db, config, row, True)
            assert enough and credits.amount(quoted) == 1 and "кредитов" in text
            lm.launch(db, config, row, digest, True)
        tick(credit_fixture)
        tick(credit_fixture)
    with Session(engine) as db:
        row = content_tasks.owned(db, config, owners[1], bot, token)
        assert row.status == "completed"
        assert db.get(AIOperation, row.operation_id).charged_credits == 1
        assert ai_tokens.available(db, owners[1], bot, True) == 198


def test_one_way_conversion_and_stale_confirmation(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    # Conversion is global. Keep it in a rolled-back transaction so this test never
    # migrates other fixtures' surviving accounts in the shared test database.
    with Session(engine) as db:
        set_setting(db, config, actors[0], "ai_token_price_rub", "0.0003")
        with pytest.raises(ValueError, match="изменилась"):
            set_setting(db, config, actors[0], "ai_credits_enabled", True, "stale")
        assert db.get(AIAccount, (owners[1], bot, True)).balance == 50000
        set_setting(db, config, actors[0], "ai_credits_enabled", True)
        assert ai_tokens.available(db, owners[1], bot, True) == 15
        funds = db.get(AIAccount, (owners[1], bot, True))
        assert funds.balance == 0 and funds.credit_value_rub == 0
        set_setting(db, config, actors[0], "ai_credits_enabled", True)
        with pytest.raises(ValueError, match="Обратное"):
            set_setting(db, config, actors[0], "ai_credits_enabled", False)
        assert funds.credit_balance == 15


def test_below_cost_credit_price_blocks_before_api(credit_fixture):
    engine, config, actors, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        set_setting(db, config, actors[0], "ai_credit_price_rub", "0.01")
    with Session(engine) as db:
        quoted = agents.quote_images(db, article())
    with (
        patch.object(agents, "deepseek_generate") as api,
        pytest.raises(ValueError, match="Цена"),
    ):
        agents.generate(engine, config, owners[1], bot, "below-cost", quoted)
    api.assert_not_called()


def test_package_settings_update_unit_cost(credit_fixture):
    engine, config, actors, *_ = credit_fixture
    with Session(engine) as db:
        saved = {
            k: row.value
            for k in (
                "zenstat_package_price_rub",
                "zenstat_package_requests",
                "zenstat_request_cost_rub",
            )
            if (row := db.get(Setting, k))
        }
    try:
        with Session(engine) as db, db.begin():
            set_setting(db, config, actors[0], "zenstat_package_price_rub", "1500.00")
            set_setting(db, config, actors[0], "zenstat_package_requests", 5000)
        with Session(engine) as db:
            assert Decimal(db.get(Setting, "zenstat_request_cost_rub").value) == Decimal("0.30")
    finally:
        with Session(engine) as db, db.begin():
            for key in (
                "zenstat_package_price_rub",
                "zenstat_package_requests",
                "zenstat_request_cost_rub",
            ):
                db.execute(delete(Setting).where(Setting.key == key))
                if key in saved:
                    db.add(Setting(key=key, value=saved[key]))


def test_finance_render_snapshots(admin_fixture):
    client, _, _, fixture, _ = admin_fixture
    engine = fixture[0]
    bot = 123456
    with Session(engine) as db, db.begin():
        for provider, data in {
            "zenstat": {
                "checked_at": "2026-10-16T10:50:57+00:00",
                "packages": [
                    {"type": "common", "balance": 993, "date_end": "2026-11-12T09:41:09+03:00"}
                ],
            },
            "apify": {
                "checked_at": "2026-10-16T10:50:57+00:00",
                "used_usd": "2.59",
                "limit_usd": "5.00",
                "remaining_usd": "2.41",
                "cycle_end": "2026-10-23T23:59:59.999Z",
                "error": "unavailable",
            },
        }.items():
            key = f"provider_limits:{bot}:{provider}"
            db.execute(delete(Setting).where(Setting.key == key))
            db.add(Setting(key=key, value=data))
    with patch.object(provider_limits, "refresh") as external:
        for name, url in {
            "finance": "/owner/finance/",
            "credit-prices": "/owner/finance/prices",
            "provider-limits": "/owner/finance/providers",
        }.items():
            page = client.get(url)
            assert page.status_code == 200
            RENDER_SNAPSHOTS[name] = page.get_data()
            for path in re.findall(rb'(?:src|href)="(/(?:owner/)?static/[^"?]+)', page.data):
                asset = client.get(path.decode())
                assert asset.status_code == 200
                RENDER_ASSETS[path.decode()] = (asset.get_data(), asset.content_type)
        external.assert_not_called()


@pytest.mark.parametrize("provider", ["zenstat", "youtube", "apify"])
def test_provider_quota_rejection_never_charges_customer(credit_fixture, provider):
    engine, config, actors, owners, bot = credit_fixture
    config = config.model_copy(update={"apify_api_token": SecretStr("synthetic")})
    if provider == "zenstat":
        with (
            patch.object(agents, "dzen_posts", side_effect=AgentError("Quota", 402)),
            pytest.raises(AgentError),
        ):
            agents.stats(
                engine, config, owners[1], bot, "https://dzen.ru/example", "1", "quota-zen"
            )
    elif provider == "youtube":
        with Session(engine) as db:
            quoted = youtube_billing.quote(db, config, owners[1], bot, "@synthetic")
            quoted["confirmed_billing"] = "balance"

        def rejected(*_):
            raise YouTubeError("Quota exhausted", quota=True)

        with pytest.raises(YouTubeError):
            youtube_billing.execute(
                engine, config, owners[1], bot, "quota-youtube", quoted, rejected
            )
    else:
        with Session(engine) as db, db.begin():
            row = content_tasks.create(db, config, owners[1], bot, actors[1], "link")
            content_tasks.change(
                row, source_platform="telegram", url="https://t.me/example", period_selected=True
            )
            from leedbox.application.versions import fingerprint

            quoted = content_tasks.quote(db, row)
            digest = fingerprint({**quoted, "billing": "balance"})[:12]
            content_tasks.launch(db, config, owners[1], bot, row.token, digest)
        with patch.object(apify, "start", side_effect=AgentError("Quota", 402)):
            tick((engine, config, actors, owners, bot))
    with Session(engine) as db:
        funds = db.get(AIAccount, (owners[1], bot, True))
        assert funds.credit_balance == 200 and funds.credit_reserved == 0
        row = db.scalar(
            select(AIOperation)
            .where(
                AIOperation.owner_user_id == owners[1],
                AIOperation.kind == "search",
            )
            .order_by(AIOperation.id.desc())
        )
        assert row.status == "failed" and row.charged_credits == 0


def test_uncertain_api_expense_is_not_presented_as_zero_profit(credit_fixture):
    engine, _, _, owners, bot = credit_fixture
    with Session(engine) as db, db.begin():
        row = ai_tokens.reserve(db, owners[1], bot, True, "unknown-expense", 10000, offer(db))
        row.status = "uncertain"
    with Session(engine) as db:
        report = window("30", "test")
        assert finance.summary(db, bot, report)["unknown"] >= 1
        rows = finance.by_agent(db, bot, report)
        assert any(row.unknown >= 1 for row in rows)
