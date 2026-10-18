"""One lifetime trial per expert; subscription enforcement is separate from AI trials."""

# ruff: noqa: F811 -- pytest fixture imports.
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_content_agent_postgres import content_fixture, response, tick  # noqa: F401
from test_dzen_agent import ai_fixture  # noqa: F401
from test_lead_magnet_agent import magnets
from test_lead_magnet_agent_postgres import ready
from test_services_postgres import service_fixture  # noqa: F401
from test_youtube_postgres import snapshot

from leedbox.application import agents, ai_tokens, youtube_billing
from leedbox.application import content_agent as content
from leedbox.application import lead_magnet_agent as lm
from leedbox.application.entitlements import configure_policy, extend_access
from leedbox.application.materials import connect_channel, publish
from leedbox.application.versions import fingerprint
from leedbox.domain.materials import Content
from leedbox.infrastructure.models import AccessGrant, AccessPolicy, AIOperation, ContentTask
from leedbox.infrastructure.youtube import YouTubeError
from leedbox.telegram.channels import Subscription
from leedbox.telegram.delivery import deliver, find_material
from leedbox.telegram.lead_magnet_agent import screen


@pytest.fixture
def trials(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    config = config.model_copy(update={"youtube_api_key": SecretStr("dummy-youtube")})
    with Session(engine) as db, db.begin():
        for owner in owners[1:]:
            account = ai_tokens.account(db, owner, bot, True)
            account.balance = 0
            account.free_limits = {"lead_magnet": 1, "content_search": 1, "youtube_search": 1}
    yield engine, config, actors, owners, bot


def test_lead_magnet_one_initial_generation_then_paid_critic(trials):
    engine, config, _, owners, bot = trials
    token = ready(trials)
    with patch.object(lm, "deepseek_generate", return_value=response(magnets())) as api:
        tick(trials)
        assert api.call_count == 1
    with Session(engine) as db, db.begin():
        row = db.get(ContentTask, token)
        op = db.get(AIOperation, row.operation_id)
        assert op.billing_mode == "trial" and op.status == "completed"
        assert ai_tokens.available(db, owners[1], bot, True) == 0
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "lead_magnet") == 0
        assert ai_tokens.free_remaining(db, owners[2], bot, True, "lead_magnet") == 1
        assert ai_tokens.free_remaining(db, owners[1], bot + 1, True, "lead_magnet") == 0
        assert ai_tokens.free_remaining(db, owners[1], bot, False, "lead_magnet") == 0
        _, digest, _, enough = lm.confirmation(db, config, row, True)
        assert not enough
        text, rows, _ = screen(db, config, row)
        assert "Купить токены" in str(rows) and "не хватает" in text
        with pytest.raises(ValueError, match="Недостаточно"):
            lm.launch(db, config, row, digest, True)
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "lead_magnet_revision") == 0


def test_failed_lm_restores_free_attempt(trials):
    engine, _, _, owners, bot = trials
    ready(trials)
    with patch.object(lm, "deepseek_generate", return_value=response({})):
        tick(trials)
    with Session(engine) as db:
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "lead_magnet") == 1


def search_row(db, fixture):
    _, config, actors, owners, bot = fixture
    row = content.create(db, config, owners[1], bot, actors[1], "link")
    content.change(
        row, source_platform="telegram", url="https://t.me/example", period_selected=True
    )
    return row


def test_competitor_trial_zero_balance_then_next_search_is_paid(trials):
    engine, config, _, owners, bot = trials
    with Session(engine) as db, db.begin():
        row = search_row(db, trials)
        offer = content.quote(db, row)
        mode = ai_tokens.funding(db, config, owners[1], bot, True, offer)
        assert mode == "trial"
        digest = fingerprint({**offer, "billing": mode})[:12]
        content.launch(db, config, owners[1], bot, row.token, digest)
        content.launch(db, config, owners[1], bot, row.token, digest)
        number = row.operation_id
        row.payload = {
            **row.payload,
            "parts": [{"id": "1", "url": "https://t.me/example/1", "metrics": {"views": 10}}],
        }
        content._search_done(db, row)
        row.status = "completed"
        assert db.get(AIOperation, number).billing_mode == "trial"
        assert ai_tokens.available(db, owners[1], bot, True) == 0
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "content_search") == 0
        assert ai_tokens.free_remaining(db, owners[2], bot, True, "content_search") == 1
        second = search_row(db, trials)
        current = content.quote(db, second)
        mode = ai_tokens.funding(db, config, owners[1], bot, True, current)
        assert mode == "balance"
        with pytest.raises(ValueError, match="Недостаточно"):
            content.launch(
                db,
                config,
                owners[1],
                bot,
                second.token,
                fingerprint({**current, "billing": mode})[:12],
            )


def test_empty_competitor_search_restores_trial(trials):
    engine, config, _, owners, bot = trials
    with Session(engine) as db, db.begin():
        row = search_row(db, trials)
        offer = content.quote(db, row)
        content.launch(
            db, config, owners[1], bot, row.token, fingerprint({**offer, "billing": "trial"})[:12]
        )
        content.finish(db, row, False)
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "content_search") == 1


def youtube_offer(db, fixture, source="@example", index=1):
    _, config, _, owners, bot = fixture
    offer = youtube_billing.quote(db, config, owners[index], bot, source)
    _, text = agents.payment_preview(db, config, owners[index], bot, offer)
    return offer, text


def test_youtube_trial_paid_repeat_and_foreign_user(trials):
    engine, config, _, owners, bot = trials
    with Session(engine) as db:
        offer, text = youtube_offer(db, trials)
        assert "Бесплатная попытка" in text
    with patch("leedbox.application.youtube.analyze", return_value=(snapshot(), True)) as fetch:
        number = youtube_billing.execute(engine, config, owners[1], bot, "yt-trial", offer, fetch)
        assert (
            youtube_billing.execute(engine, config, owners[1], bot, "yt-trial", offer, fetch)
            == number
        )
        assert (
            youtube_billing.execute(engine, config, owners[1], bot, "yt-reopen", offer, fetch)
            == number
        )
        fetch.assert_called_once()
    with Session(engine) as db:
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "youtube_search") == 0
        assert ai_tokens.free_remaining(db, owners[2], bot, True, "youtube_search") == 1
        next_offer, text = youtube_offer(db, trials, "@another")
        assert "Цена поиска" in text and "Пополните" in text
    with patch("leedbox.application.youtube.analyze") as fetch:
        with pytest.raises(ValueError, match="Недостаточно"):
            youtube_billing.execute(engine, config, owners[1], bot, "yt-paid", next_offer, fetch)
        fetch.assert_not_called()
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owners[1], bot, True, "yt-funds", 5000)
    with patch("leedbox.application.youtube.analyze", return_value=(snapshot(), False)) as fetch:
        number = youtube_billing.execute(
            engine, config, owners[1], bot, "yt-paid", next_offer, fetch
        )
    with Session(engine) as db:
        op = db.get(AIOperation, number)
        assert op.billing_mode == "balance" and op.status == "completed"
        assert ai_tokens.available(db, owners[1], bot, True) == 4000
        assert "не расход ИИ" in youtube_billing.receipt(db, owners[1], bot, True, number)


def test_youtube_error_restores_trial_and_paid_reserve(trials):
    engine, config, _, owners, bot = trials
    with Session(engine) as db:
        offer, _ = youtube_offer(db, trials)
    with (
        patch("leedbox.application.youtube.analyze", side_effect=YouTubeError("dummy")) as fetch,
        pytest.raises(YouTubeError),
    ):
        youtube_billing.execute(engine, config, owners[1], bot, "yt-failure", offer, fetch)
    with Session(engine) as db:
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "youtube_search") == 1
        assert ai_tokens.account(db, owners[1], bot, True).reserved == 0


def test_parallel_youtube_trial_cannot_be_used_twice(trials):
    engine, config, _, owners, bot = trials
    with Session(engine) as db:
        offers = [youtube_offer(db, trials, f"@example{i}")[0] for i in range(2)]

    def run(i):
        try:
            return youtube_billing.execute(
                engine,
                config,
                owners[1],
                bot,
                f"yt-race-{i}",
                offers[i],
                lambda *_: (snapshot(), False),
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(r is not None for r in pool.map(run, range(2))) == 1


def test_youtube_interrupted_scan_returns_trial(trials):
    engine, config, _, owners, bot = trials
    with Session(engine) as db, db.begin():
        offer, _ = youtube_offer(db, trials)
        op = ai_tokens.reserve(
            db, owners[1], bot, True, "yt-crash", offer["maximum"], offer, "trial", "search"
        )
        op.status, op.created_at = "sent", datetime.now(UTC) - timedelta(minutes=4)
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "youtube_search") == 0
    youtube_billing.recover_stale(engine, config, bot)
    youtube_billing.recover_stale(engine, config, bot)
    with Session(engine) as db:
        assert ai_tokens.free_remaining(db, owners[1], bot, True, "youtube_search") == 1
        assert ai_tokens.account(db, owners[1], bot, True).reserved == 0


@pytest.mark.parametrize("legacy_policy", ["bypass", "check", "deny"])
@pytest.mark.parametrize("trial_active", [False, True])
def test_expired_expert_gift_stays_public_paid_access_restores_check(
    service_fixture, legacy_policy, trial_active
):
    from decimal import Decimal

    engine, config, actors, owners, projects, *_ = service_fixture
    now = datetime.now(UTC)
    with Session(engine) as db:
        policy = db.get(AccessPolicy, 1)
        before = (
            {c.name: getattr(policy, c.name) for c in AccessPolicy.__table__.columns}
            if policy
            else None
        )
    try:
        with Session(engine) as db, db.begin():
            channel = connect_channel(
                db, owners[1], -1000000000000 - owners[1], "Test", "https://t.me/example"
            )
            material = publish(
                db,
                owners[1],
                projects[1],
                123456,
                Content("Подарок", "", "text", "Текст"),
                "subscribers",
                channel.id,
            )
            slug = material.slug
            configure_policy(
                db,
                config,
                actors[0],
                mode="paid",
                price=Decimal("100"),
                currency="RUB",
                period_days=30,
                trial_days=0,
                grace_days=0,
                activation_grace_days=0,
                expired_delivery=legacy_policy,
                now=now,
            )
            for grant in db.scalars(select(AccessGrant).where(AccessGrant.user_id == owners[1])):
                grant.starts_at = now - timedelta(days=1 if trial_active else 40)
                grant.ends_at = (
                    now + timedelta(days=30) if trial_active else now - timedelta(days=10)
                )
        public = find_material(engine, 123456, slug)
        assert public.mode == "everyone" and public.bypassed
        fake_bot = Mock(id=123456)
        fake_bot.me = AsyncMock(return_value=Mock(username="trial_test_bot"))
        fake_bot.send_message = AsyncMock(return_value=Mock(message_id=42))
        with patch("leedbox.telegram.delivery.subscription", new_callable=AsyncMock) as check:
            assert asyncio.run(deliver(fake_bot, engine, actors[2], slug)) is None
            check.assert_not_called()
            fake_bot.send_message.assert_awaited_once()
        with Session(engine) as db, db.begin():
            extend_access(db, config, actors[0], owners[1], 30, f"qa-check-paid-{owners[1]}")
        paid = find_material(engine, 123456, slug)
        assert paid.mode == "subscribers" and not paid.bypassed
        fake_bot.send_message.reset_mock()
        with patch(
            "leedbox.telegram.delivery.subscription",
            new_callable=AsyncMock,
            return_value=Subscription.NOT_SUBSCRIBED,
        ) as check:
            assert "Подписка пока не найдена" in asyncio.run(
                deliver(fake_bot, engine, actors[2], slug)
            )
            check.assert_awaited_once()
            fake_bot.send_message.assert_not_awaited()
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(AccessPolicy).where(AccessPolicy.id == 1))
            if before:
                db.add(AccessPolicy(**before))
