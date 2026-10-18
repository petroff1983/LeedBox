import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import test_admin_postgres
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import broadcasts
from leedbox.application.projects import register_user
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import OutboxMessage, OwnerBroadcast, User, WizardDraft
from leedbox.telegram.worker import send_one

admin_fixture = test_admin_postgres.admin_fixture


@pytest.fixture
def broadcast_fixture(request):
    engine, config, actors, owners, *_ = request.getfixturevalue("service_fixture")
    ids = []
    bot_id = broadcasts.configured_bot(config)
    with Session(engine) as db, db.begin():
        for owner in owners:
            broadcasts.enroll(db, db.get(User, owner), bot_id)
    try:
        yield engine, config, actors, owners, ids
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(OwnerBroadcast).where(OwnerBroadcast.id.in_(ids)))


@pytest.mark.integration
def test_expert_scope_idempotency_optout_and_cancel(broadcast_fixture):
    engine, config, actors, owners, ids = broadcast_fixture
    bot_id = broadcasts.configured_bot(config)
    with Session(engine) as db, db.begin():
        recipient = register_user(db, actors[2] + 100000000)
        with pytest.raises(ObjectUnavailable):
            broadcasts.enroll(db, recipient, bot_id)
        with pytest.raises(PermissionError):
            broadcasts.save_campaign(db, config, actors[1], "Тест", "notice", {"text": "Тест"})
        row = broadcasts.save_campaign(
            db, config, actors[0], "Новость", "notice", {"text": "Новый промпт"}
        )
        ids.append(row.id)
        revision = row.revision
        broadcasts.change_preference(db, actors[2], bot_id, "notice", False)
        assert broadcasts.enqueue(db, config, actors[0], row.id, revision) == 2
        assert broadcasts.enqueue(db, config, actors[0], row.id, revision) == 2
        messages = list(
            db.scalars(select(OutboxMessage).where(OutboxMessage.broadcast_id == row.id))
        )
        assert len(messages) == 2
        broadcasts.change_preference(db, actors[1], bot_id, "notice", False)
        assert not broadcasts.can_deliver(
            db, next(m for m in messages if m.telegram_user_id == actors[1])
        )
        broadcasts.cancel(db, config, actors[0], row.id)
        assert all(m.status == "cancelled" for m in messages)
        db.delete(recipient)


@pytest.mark.integration
def test_advertising_requires_individual_consent_and_recipient_mode_skips(broadcast_fixture):
    engine, config, actors, owners, ids = broadcast_fixture
    bot_id = broadcasts.configured_bot(config)
    with Session(engine) as db, db.begin():
        payload = {
            "text": "Тест",
            "advertiser": "Тест",
            "inn": "0000000000",
            "erid": "test",
            "marking_text": "Реклама",
        }
        row = broadcasts.save_campaign(db, config, actors[0], "Реклама", "advertising", payload)
        ids.append(row.id)
        assert broadcasts.audience_count(db, bot_id, "advertising") == 0
        broadcasts.change_preference(db, actors[1], bot_id, "advertising", True)
        assert broadcasts.enqueue(db, config, actors[0], row.id, 1) == 1
        message = db.scalar(select(OutboxMessage).where(OutboxMessage.broadcast_id == row.id))
        assert broadcasts.can_deliver(db, message)
        key = f"{bot_id}:{actors[1]}:{actors[1]}:None:None:default"
        db.add(WizardDraft(key=key, data={"mode": "recipient"}))
        db.flush()
        assert not broadcasts.can_deliver(db, message)
        broadcasts.change_preference(db, actors[1], bot_id, "advertising", False)
        preference = broadcasts.preferences(db, actors[1], bot_id)
        assert preference.advertising_granted_at and preference.advertising_revoked_at


@pytest.mark.integration
@pytest.mark.parametrize(
    "failure,status",
    [(None, "sent"), ("network", "uncertain"), ("forbidden", "failed"), ("retry", "pending")],
)
def test_worker_campaign_outcomes_no_real_messages(broadcast_fixture, failure, status):
    engine, config, actors, owners, ids = broadcast_fixture
    bot = Bot(config.bot_token.get_secret_value())
    with Session(engine) as db, db.begin():
        for actor in (actors[0], actors[2]):
            broadcasts.change_preference(db, actor, bot.id, "notice", False)
        row = broadcasts.save_campaign(
            db,
            config,
            actors[0],
            "Новость",
            "notice",
            {
                "text": "Синтетический тест <b>не HTML</b>",
                "button_label": "Подробнее",
                "button_url": "https://example.com",
            },
        )
        ids.append(row.id)
        broadcasts.enqueue(db, config, actors[0], row.id, 1)
        id = row.id
    calls = []

    async def api(_bot, method, timeout=None):
        assert isinstance(method, SendMessage)
        calls.append(method)
        if failure == "network":
            raise TelegramNetworkError(method, "synthetic")
        if failure == "forbidden":
            raise TelegramForbiddenError(method, "synthetic")
        if failure == "retry":
            raise TelegramRetryAfter(method, "synthetic", 30)
        return True

    async def run():
        try:
            with patch.object(bot.session, "make_request", api):
                await send_one(bot, engine, config)
        finally:
            await bot.session.close()

    asyncio.run(run())
    assert len(calls) == 1 and calls[0].parse_mode is None
    assert calls[0].reply_markup.inline_keyboard[-1][0].callback_data == "notify:set:notice:0"
    with Session(engine) as db:
        message = db.scalar(select(OutboxMessage).where(OutboxMessage.broadcast_id == id))
        assert message.status == status
        if failure == "retry":
            assert message.ready_at > datetime.now(UTC)


@pytest.mark.integration
def test_admin_draft_preview_release_csrf_and_readonly_after_launch(admin_fixture):
    client, csrf, config, context, _ = admin_fixture
    engine, _, actors, owners, *_ = context
    with Session(engine) as db, db.begin():
        broadcasts.enroll(db, db.get(User, owners[1]), broadcasts.configured_bot(config))
    payload = {
        "csrf_token": csrf,
        "confirmed": "1",
        "action": "save",
        "expected": "0",
        "title": "Тестовая рассылка",
        "kind": "notice",
        "text": "Синтетическое объявление",
    }
    assert client.get("/owner/broadcasts/").status_code == 200
    overview = client.get("/owner/").text
    assert "Сообщения бота" in overview and "Промпты и агенты" in overview
    assert "Темы воронки" not in overview and "Категории знаний" not in overview
    assert "Статьи" not in overview and ">Тексты<" not in overview
    settings = client.get("/owner/settings/").text
    assert "offer_ad_limit" not in settings and "offer_button_limit" not in settings
    oversized = client.post("/owner/broadcasts/edit", data={**payload, "text": "🎁" * 2500})
    assert oversized.status_code == 200 and "слишком длинное" in oversized.text
    assert (
        client.post("/owner/broadcasts/edit", data={**payload, "csrf_token": "bad"}).status_code
        == 403
    )
    preview = client.post("/owner/broadcasts/edit", data={**payload, "action": "preview"})
    assert "Синтетическое объявление" in preview.text
    saved = client.post("/owner/broadcasts/edit", data=payload)
    assert saved.status_code == 302
    url = saved.location
    id = int(url.split("id=")[1])
    try:
        page = client.get(url)
        assert "Отправить экспертам" in page.text
        test_admin_postgres.RENDER_SNAPSHOTS["broadcast-ready"] = page.get_data()
        test_admin_postgres.RENDER_SNAPSHOTS["broadcast-edit"] = client.get(
            "/owner/broadcasts/edit"
        ).get_data()
        import re

        for body in (page.get_data(), test_admin_postgres.RENDER_SNAPSHOTS["broadcast-edit"]):
            for path in re.findall(rb'(?:src|href)="(/(?:owner/)?static/[^"?]+)', body):
                asset = client.get(path.decode())
                test_admin_postgres.RENDER_ASSETS[path.decode()] = (
                    asset.get_data(),
                    asset.content_type,
                )
        assert (
            client.post(
                url, data={"csrf_token": csrf, "action": "send", "expected": "1"}
            ).status_code
            == 200
        )
        with Session(engine) as db:
            assert broadcasts.counts(db, id) == {}
        send = {"csrf_token": csrf, "confirmed": "1", "action": "send", "expected": "1"}
        assert client.post(url, data=send).status_code == 302
        assert client.post(url, data=send).status_code == 302
        assert "Отправить экспертам" not in client.get(url).text
        assert (
            client.post(
                url, data={"csrf_token": csrf, "confirmed": "1", "action": "cancel"}
            ).status_code
            == 302
        )
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(OwnerBroadcast).where(OwnerBroadcast.id == id))
