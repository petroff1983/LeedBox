import asyncio
import os
from datetime import UTC, datetime, timedelta
from secrets import randbelow, token_hex
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    AnswerCallbackQuery,
    CreateChatInviteLink,
    GetMe,
    RevokeChatInviteLink,
    SendDocument,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatInviteLink,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    Update,
    User,
)
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application.invites import (
    join_summary,
    observe_membership,
    recover_operations,
    snapshot,
)
from leedbox.application.links import (
    get_link,
    record_start,
    save_utm,
    search_links,
    set_enabled,
    stats,
    utm_csv,
)
from leedbox.application.materials import publish
from leedbox.application.profile import delete_my_data
from leedbox.application.projects import create_project, register_user
from leedbox.application.results import get_result
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import Content
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import (
    Channel,
    JoinEvent,
    LinkEvent,
    OutboxMessage,
    TrackedLink,
    UtmTemplate,
)
from leedbox.telegram.handlers import build_router
from leedbox.telegram.invites import revoke_invite
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app


@pytest.fixture
def link_fixture():
    if not os.getenv("LEEDBOX_DATABASE_URL"):
        pytest.skip("PostgreSQL required")
    config = Settings()
    assert config.environment == "test"
    assert (make_url(config.database_url.get_secret_value()).database or "").endswith("_test")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_database_engine(config)
    actors = [900000000 + randbelow(10000000) for _ in range(3)]
    owners, projects = [], []
    with Session(engine) as session, session.begin():
        for actor in actors:
            user = register_user(session, actor)
            owners.append(user.id)
            projects.append(create_project(session, user.id, "Ссылки").id)
    try:
        yield engine, config, actors, owners, projects
    finally:
        with Session(engine) as session, session.begin():
            for actor in actors:
                delete_my_data(session, actor)
        engine.dispose()


def saved_link(session, owner_id, project_id, *, kind="expert", **values):
    """Seed historical records directly; retired creation APIs are not part of the app."""
    code = token_hex(6)
    payload = values.pop("payload", "ex_" + code if kind == "expert" else None)
    fields = {
        "name": "Saved link",
        "source": "source",
        "campaign": "campaign",
        "destination": "https://t.me/leedbox_test?start=" + (payload or ""),
        "action_key": "saved:" + code,
    }
    fields.update(values)
    link = TrackedLink(
        owner_user_id=owner_id,
        project_id=project_id,
        bot_id=123456,
        kind=kind,
        code=code,
        payload=payload,
        **fields,
    )
    session.add(link)
    session.flush()
    return link


@pytest.mark.integration
def test_utm_results_idempotence_export_and_owner_isolation(link_fixture):
    engine, _, _, owners, projects = link_fixture
    inputs = {
        "name": "Мой шаблон",
        "platform": "vk",
        "urls": "https://example.com/a\nhttps://example.com/b",
        "source": "=1+1",
        "medium": "paid_social",
        "campaign": "test",
    }
    with Session(engine) as session, session.begin():
        value = save_utm(session, owners[0], projects[0], 123456, inputs, f"test-utm:{owners[0]}")
        assert (
            save_utm(session, owners[0], projects[0], 123456, inputs, f"test-utm:{owners[0]}").id
            == value.id
        )
        assert "'=1+1" in utm_csv(value).decode("utf-8-sig")
        for foreign in owners[1:]:
            with pytest.raises(ObjectUnavailable):
                get_result(session, foreign, 123456, value.id)
        with pytest.raises(ObjectUnavailable):
            get_result(session, owners[0], 888, value.id)


@pytest.mark.integration
def test_tracked_deep_links_clicks_starts_disable_and_canonical_material(link_fixture):
    engine, config, actors, owners, projects = link_fixture
    with Session(engine) as session, session.begin():
        material = publish(
            session,
            owners[0],
            projects[0],
            123456,
            Content("Файл", "", "text", "Текст"),
            "everyone",
            None,
        )
        link = saved_link(
            session,
            owners[0],
            projects[0],
            kind="material",
            material_id=material.id,
            payload=material.slug + "_" + token_hex(6),
        )
        session.add(
            UtmTemplate(
                owner_user_id=owners[0],
                project_id=projects[0],
                bot_id=123456,
                name="Saved template",
                platform="vk",
                tags={"source": "vk"},
                action_key=f"saved-template:{owners[0]}",
            )
        )
        assert len(link.payload) <= 64 and link.payload.startswith(material.slug + "_")
        code, payload, id, slug = link.code, link.payload, link.id, material.slug
        assert record_start(session, 123456, payload, actors[1], f"event:{id}:1")
        assert record_start(session, 123456, payload, actors[1], f"event:{id}:1")
        assert record_start(session, 123456, payload, actors[1], f"event:{id}:2")
        assert record_start(session, 123456, payload, actors[2], f"event:{id}:3")
        assert stats(session, id) == (0, 3, 2)
        assert record_start(session, 777, payload, actors[1], "foreign") is None
        for foreign in owners[1:]:
            with pytest.raises(ObjectUnavailable):
                get_link(session, foreign, 123456, id)
            with pytest.raises(ObjectUnavailable):
                set_enabled(session, foreign, 123456, id, False)
        assert record_start(session, 123456, slug, actors[1], "canonical") is None
    client = create_app(config, engine).test_client()
    assert client.get(f"/go/{code}").status_code == 302
    assert client.get(f"/go/{code}").headers["Location"].endswith("start=" + payload)
    assert client.head(f"/go/{code}").status_code == 302
    assert client.get("/go/missing").status_code == 404
    with Session(engine) as session, session.begin():
        assert stats(session, id) == (2, 3, 2)
        set_enabled(session, owners[0], 123456, id, False)
        assert record_start(session, 123456, payload, actors[1], "after-disable") is False
    assert client.get(f"/go/{code}").status_code == 404
    from leedbox.telegram.delivery import find_material

    assert find_material(engine, 123456, slug) is not None
    with Session(engine) as session, session.begin():
        delete_my_data(session, actors[0])
        assert (
            session.scalar(
                select(func.count()).select_from(TrackedLink).where(TrackedLink.id == id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(LinkEvent).where(LinkEvent.link_id == id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(UtmTemplate)
                .where(UtmTemplate.owner_user_id == owners[0])
            )
            == 0
        )


@pytest.mark.integration
def test_retired_link_wizards_preserve_exports_and_saved_links(link_fixture):
    engine, config, actors, owners, projects = link_fixture
    with Session(engine) as session, session.begin():
        saved = save_utm(
            session,
            owners[0],
            projects[0],
            123456,
            {
                "name": "Saved",
                "platform": "yandex",
                "urls": "https://example.com",
                "source": "yandex",
                "medium": "cpc",
                "campaign": "test",
            },
            f"saved-utm:{owners[0]}",
        )
        result_id = saved.id
        link = saved_link(session, owners[0], projects[0])
        link_id = link.id

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        bot_user = User(id=123456, is_bot=True, first_name="Synthetic", username="leedbox_test")
        calls = []
        sequence = 0

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, AnswerCallbackQuery):
                return True
            if isinstance(method, GetMe):
                return bot_user
            return Message(
                message_id=20,
                date=datetime.now(UTC),
                chat=Chat(id=actors[0], type="private"),
                from_user=bot_user,
            )

        async def callback(value, actor=actors[0]):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        data=value,
                        chat_instance="test",
                        from_user=User(id=actor, first_name="Synthetic", is_bot=False),
                        message=Message(
                            message_id=20,
                            date=datetime.now(UTC),
                            chat=Chat(id=actor, type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        key = StorageKey(bot.id, actors[0], actors[0])
        current = {"mode": "expert", "utm_draft": {"token": "preserve-current-draft"}}
        try:
            await storage.set_data(key, current)
            await storage.set_data(StorageKey(bot.id, actors[1], actors[1]), {"mode": "expert"})
            with patch.object(bot.session, "make_request", api):
                for value in (
                    "ln:new:yandex",
                    "ln:template:1",
                    "ln:channel:1",
                    "lw:1:publish",
                    "li:retry:1",
                    f"ur:save:{result_id}",
                ):
                    await callback(value)
                    after = await storage.get_data(key)
                    assert after["mode"] == current["mode"]
                    assert after["utm_draft"] == current["utm_draft"]
                    assert isinstance(calls[-1], AnswerCallbackQuery)
                    assert "убран" in calls[-1].text
                assert not any(isinstance(c, CreateChatInviteLink) for c in calls)
                await callback(f"ur:csv:{result_id}")
                assert any(isinstance(c, SendDocument) for c in calls)
                before = sum(isinstance(c, SendDocument) for c in calls)
                await callback(f"ur:csv:{result_id}", actors[1])
                assert sum(isinstance(c, SendDocument) for c in calls) == before
                await callback("ln:history:0")
                await callback(f"lr:open:{link_id}")
                await callback(f"lr:disable:{link_id}")
                await callback(f"lr:confirm:{link_id}")
                with Session(engine) as session:
                    assert not get_link(session, owners[0], bot.id, link_id).enabled
                    assert (
                        session.scalar(
                            select(func.count())
                            .select_from(UtmTemplate)
                            .where(UtmTemplate.owner_user_id == owners[0])
                        )
                        == 0
                    )
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_saved_invite_membership_and_rights_events_remain_idempotent(link_fixture):
    engine, config, actors, owners, projects = link_fixture
    destination = "https://t.me/+synthetic_saved_invite"
    with Session(engine) as session, session.begin():
        channel = Channel(
            owner_user_id=owners[0],
            telegram_id=-1000000000000 - actors[0],
            title="Synthetic",
            join_url="https://t.me/synthetic_channel",
            bot_rights="ok",
        )
        session.add(channel)
        session.flush()
        channel_id, channel_tg_id = channel.id, channel.telegram_id
        record = saved_link(
            session,
            owners[0],
            projects[0],
            kind="invite",
            channel_id=channel_id,
            destination=destination,
        )
        link_id = record.id

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router(engine, config))
        own = User(id=123456, is_bot=True, first_name="Synthetic")
        member = User(id=actors[2], is_bot=False, first_name="Synthetic")
        chat = Chat(id=channel_tg_id, type="channel")
        date = datetime.now(UTC)
        joined = Update(
            update_id=10000000 + actors[0],
            chat_member=ChatMemberUpdated(
                chat=chat,
                date=date,
                from_user=member,
                old_chat_member=ChatMemberLeft(user=member),
                new_chat_member=ChatMemberMember(user=member),
                invite_link=ChatInviteLink(
                    invite_link=destination,
                    creator=own,
                    creates_join_request=False,
                    is_primary=False,
                    is_revoked=False,
                ),
            ),
        )
        lost = Update(
            update_id=20000000 + actors[0],
            my_chat_member=ChatMemberUpdated(
                chat=chat,
                date=date,
                from_user=member,
                old_chat_member=ChatMemberMember(user=own),
                new_chat_member=ChatMemberLeft(user=own),
            ),
        )
        try:
            for update in (joined, joined, lost, lost):
                await dispatcher.feed_update(bot, update)
        finally:
            await bot.session.close()
            await dispatcher.storage.close()

    asyncio.run(scenario())
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(JoinEvent).where(JoinEvent.link_id == link_id)
            )
            == 1
        )
        channel = session.get(Channel, channel_id)
        assert channel.bot_rights == "no_rights" and channel.membership_gap
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.dedupe_key == f"my_rights:123456:{20000000 + actors[0]}")
            )
            == 1
        )


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["timeout", "rejected"])
def test_saved_invite_revocation_retry_and_owner_isolation(link_fixture, failure):
    engine, config, actors, owners, projects = link_fixture
    with Session(engine) as session, session.begin():
        channel = Channel(
            owner_user_id=owners[0],
            telegram_id=-1000000000000 - actors[0],
            title="Synthetic",
            join_url="https://t.me/test_channel",
            bot_rights="ok",
        )
        session.add(channel)
        session.flush()
        link = saved_link(
            session,
            owners[0],
            projects[0],
            kind="invite",
            channel_id=channel.id,
            destination="https://t.me/+saved_invite",
            source="source_100%",
        )
        channel_tg_id, link_id, code = channel.telegram_id, link.id, link.code
        for foreign in owners[1:]:
            with pytest.raises(ObjectUnavailable):
                snapshot(session, foreign, 123456, link_id)
        assert search_links(session, owners[0], 123456, "100%")[0].id == link_id
        assert search_links(session, owners[1], 123456, "100%") == []
        assert search_links(session, owners[0], 123456, "_100%")
        assert search_links(session, owners[0], 999, "100%") == []
    client = create_app(config, engine).test_client()
    assert client.get(f"/go/{code}").status_code == 302

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        response = ChatInviteLink(
            invite_link="https://t.me/+saved_invite",
            creator=User(id=bot.id, is_bot=True, first_name="Bot"),
            creates_join_request=False,
            is_primary=False,
            is_revoked=True,
        )
        method = RevokeChatInviteLink(chat_id=channel_tg_id, invite_link=response.invite_link)
        error = (
            TelegramNetworkError(method=method, message="timeout")
            if failure == "timeout"
            else TelegramBadRequest(method=method, message="rejected")
        )
        revoke = AsyncMock(side_effect=[error, response])
        try:
            with patch.object(bot, "revoke_chat_invite_link", revoke):
                for foreign in owners[1:]:
                    with pytest.raises(ObjectUnavailable):
                        await revoke_invite(bot, engine, foreign, link_id)
                assert revoke.await_count == 0
                result = await revoke_invite(bot, engine, owners[0], link_id)
                assert result.status == (
                    "revoke_uncertain" if failure == "timeout" else "revoke_failed"
                )
                assert client.get(f"/go/{code}").status_code == 404
                assert (await revoke_invite(bot, engine, owners[0], link_id)).status == "revoked"
                assert (await revoke_invite(bot, engine, owners[0], link_id)).status == "revoked"
                assert revoke.await_count == 2
        finally:
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    "old_status,new_status", [("creating", "uncertain"), ("revoking", "revoke_uncertain")]
)
def test_saved_invite_crashed_operations_and_membership_are_safe(
    link_fixture, old_status, new_status
):
    engine, _, actors, owners, projects = link_fixture
    with Session(engine) as session, session.begin():
        channel = Channel(
            owner_user_id=owners[0],
            telegram_id=-1000000000000 - actors[0],
            title="Synthetic",
            join_url="https://t.me/test_channel",
            bot_rights="ok",
        )
        session.add(channel)
        session.flush()
        now = datetime.now(UTC)
        link = saved_link(
            session,
            owners[0],
            projects[0],
            kind="invite",
            channel_id=channel.id,
            destination="" if old_status == "creating" else "https://t.me/+saved_invite",
            status=old_status,
            enabled=False,
            operation_at=now - timedelta(minutes=6),
        )
        recent = saved_link(
            session,
            owners[0],
            projects[0],
            kind="invite",
            channel_id=channel.id,
            destination="",
            status=old_status,
            enabled=False,
            operation_at=now,
        )
        recover_operations(session, 999, now)
        session.refresh(link)
        assert link.status == old_status
        recover_operations(session, 123456, now)
        session.refresh(link)
        session.refresh(recent)
        assert link.status == new_status and not link.enabled
        assert recent.status == old_status
        observe_membership(
            session,
            123456,
            20000000 + actors[0],
            channel.telegram_id,
            now,
            actors[1],
            None,
            False,
        )
        assert "вступления: 0" in join_summary(session, link)
        assert "Вступления: нет данных" not in join_summary(session, link)
