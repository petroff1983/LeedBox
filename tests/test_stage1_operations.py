import asyncio
import os
from datetime import UTC, datetime, timedelta
from secrets import randbelow
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application.limits import take
from leedbox.application.materials import publish
from leedbox.application.notifications import consent_for, queue_update
from leedbox.application.profile import delete_my_data, update_profile
from leedbox.application.projects import create_project, register_user
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import Content
from leedbox.domain.wizard import Step, advance, back
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import Consent, DeliveryEvent, Material, OutboxMessage, Recipient
from leedbox.telegram.delivery import claim, find_material, finish
from leedbox.telegram.worker import next_message, send_one


@pytest.fixture
def material_fixture():
    if not os.getenv("LEEDBOX_DATABASE_URL"):
        pytest.skip("PostgreSQL required")
    config = Settings()
    assert config.environment == "test"
    assert (make_url(config.database_url.get_secret_value()).database or "").endswith("_test")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_database_engine(config)
    actor = 800000000 + randbelow(10000000)
    target = actor + 20000000
    with Session(engine) as session, session.begin():
        owner = register_user(session, actor)
        project = create_project(session, owner.id, "Уведомления")
        value = publish(
            session,
            owner.id,
            project.id,
            654321,
            Content("Материал", "", "text", "Текст"),
            "everyone",
            None,
        )
        owner_id, project_id, material_id, slug = owner.id, project.id, value.id, value.slug
    snapshot = find_material(engine, 654321, slug)
    assert snapshot is not None
    assert claim(engine, snapshot, target)
    finish(engine, snapshot, target, "sent", 1)
    try:
        yield engine, owner_id, project_id, material_id, snapshot, actor, target
    finally:
        with Session(engine) as session, session.begin():
            delete_my_data(session, actor)
            delete_my_data(session, target)
        engine.dispose()


def test_shared_wizard_advances_validates_and_goes_back():
    steps = (Step("name", "Название", max_length=3), Step("note", "Описание", optional=True))
    draft = {"step": 0, "revision": 1}
    with pytest.raises(ValueError):
        advance(draft, steps, None)
    with pytest.raises(ValueError):
        advance(draft, steps, "long")
    result = advance(draft, steps, "abc")
    assert result["step"] == 1 and draft["step"] == 0
    assert back(result)["step"] == 0
    assert advance(result, steps, None)["step"] == 2


@pytest.mark.integration
def test_repeat_delivery_and_profile(material_fixture):
    engine, owner, project, id, snapshot, _, target = material_fixture
    assert claim(engine, snapshot, target, "012345abcdef")
    assert not claim(engine, snapshot, target, "012345abcdef")
    finish(engine, snapshot, target, "sent", 2, "012345abcdef")
    with Session(engine) as session, session.begin():
        assert (
            session.scalar(
                select(func.count()).select_from(Recipient).where(Recipient.material_id == id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(DeliveryEvent)
                .where(DeliveryEvent.material_id == id, DeliveryEvent.status == "sent")
            )
            == 2
        )
        update_profile(session, owner, project, "contact", "@test_author")
        update_profile(session, owner, project, "timezone", "Europe/Moscow")
        with pytest.raises(ValueError):
            update_profile(session, owner, project, "timezone", "Not/AZone")
        with pytest.raises(ObjectUnavailable):
            update_profile(session, owner + 999999, project, "contact", "other_author")


@pytest.mark.integration
def test_consent_scopes_cooldown_and_revoke_cancel_outbox(material_fixture):
    engine, owner, _, id, _, _, target = material_fixture
    with Session(engine) as session, session.begin():
        with pytest.raises(ObjectUnavailable):
            consent_for(session, 654321, id, target + 1, True)
        consent_for(session, 654321, id, target, True)
        assert queue_update(session, owner, 654321, id, 1, "test_bot") == 1
        with pytest.raises(ValueError):
            queue_update(session, owner, 654321, id, 1, "test_bot")
        consent_for(session, 654321, id, target, False)
    assert next_message(engine, 654321) is None
    with Session(engine) as session:
        assert (
            session.scalar(select(OutboxMessage.status).where(OutboxMessage.material_id == id))
            == "cancelled"
        )
        assert (
            session.scalar(select(Consent.purpose).where(Consent.material_id == id))
            == "author_updates"
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "failure,status", [("network", "uncertain"), ("retry", "pending"), (None, "sent")]
)
def test_outbox_api_outcomes(material_fixture, failure, status):
    engine, owner, _, id, _, _, target = material_fixture
    with Session(engine) as session, session.begin():
        consent_for(session, 654321, id, target, True)
        queue_update(session, owner, 654321, id, 1, "test_bot")

    async def scenario():
        bot = Bot("654321:TEST-TOKEN-ONLY")
        error = None
        if failure == "network":
            error = TelegramNetworkError(
                method=SendMessage(chat_id=target, text=""), message="network"
            )
        elif failure == "retry":
            error = TelegramRetryAfter(
                method=SendMessage(chat_id=target, text=""), message="rate", retry_after=3
            )
        bot.send_message = AsyncMock(side_effect=error)
        try:
            assert await send_one(bot, engine)
            assert not await send_one(bot, engine)
        finally:
            await bot.session.close()

    asyncio.run(scenario())
    with Session(engine) as session:
        assert (
            session.scalar(select(OutboxMessage.status).where(OutboxMessage.material_id == id))
            == status
        )


@pytest.mark.integration
def test_database_rate_limits_release_without_ban(material_fixture):
    engine, _, _, id, _, _, _ = material_fixture
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        assert take(session, f"test:{id}", 1, 5, now)
        assert not take(session, f"test:{id}", 1, 5, now)
        assert take(session, f"test:{id}", 1, 5, now + timedelta(seconds=5))
        assert session.get(Material, id).archived is False
