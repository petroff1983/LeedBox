import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from secrets import randbelow
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application.entitlements import start_expert
from leedbox.application.profile import delete_my_data
from leedbox.application.projects import create_project, register_user
from leedbox.application.services import (
    catalog,
    change_status,
    consent_document,
    get_request,
    owner_request,
    save_service,
    submit_request,
    withdraw_consent,
)
from leedbox.application.settings import set_setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.services import ServiceInput
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import (
    AdminAccount,
    AuditLog,
    OutboxMessage,
    ProductEvent,
    RateBucket,
    Service,
    ServiceRequest,
    Setting,
)
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage
from leedbox.telegram.worker import send_one


@pytest.fixture
def service_fixture():
    if not os.getenv("LEEDBOX_DATABASE_URL"):
        pytest.skip("PostgreSQL required")
    config = Settings()
    assert config.environment == "test"
    assert (make_url(config.database_url.get_secret_value()).database or "").endswith("_test")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_database_engine(config)
    actors = [1100000000 + randbelow(10000000) for _ in range(3)]
    config = config.model_copy(update={"owner_ids": frozenset({actors[0]})})
    owners, projects, ids = [], [], []
    keys = ["owner_details", "owner_contact", "service_consent_text", "services_enabled"]
    fields = ServiceInput(
        "Тестовая услуга",
        "Эксперты",
        "Тестовый состав работ",
        "По согласованию",
        "Тестовая цена",
        "Тестовый исполнитель",
        "Тестовые условия",
        enabled=True,
    )
    with Session(engine) as session, session.begin():
        previous = {
            key: row.value for key in keys if (row := session.get(Setting, key)) is not None
        }
        for actor in actors:
            user = register_user(session, actor)
            start_expert(session, user)
            owners.append(user.id)
            projects.append(create_project(session, user.id, "Тест услуг").id)
        set_setting(session, config, actors[0], "owner_details", "Тестовый владелец сервиса")
        set_setting(session, config, actors[0], "owner_contact", "@test_owner")
        set_setting(session, config, actors[0], "services_enabled", True)
        for name in ("Услуга A", "Услуга B"):
            ids.append(save_service(session, config, actors[0], replace(fields, title=name)).id)
    try:
        yield engine, config, actors, owners, projects, ids, fields
    finally:
        with Session(engine) as session, session.begin():
            session.execute(delete(AdminAccount).where(AdminAccount.telegram_id.in_(actors)))
            session.execute(delete(RateBucket).where(RateBucket.key.startswith("admin:")))
            for actor in actors:
                delete_my_data(session, actor)
            session.execute(delete(Service).where(Service.id.in_(ids)))
            for key in keys:
                session.execute(delete(Setting).where(Setting.key == key))
                if key in previous:
                    session.add(Setting(key=key, value=previous[key]))
        engine.dispose()


def submit(session, fixture, **overrides):
    _, config, _, owners, projects, ids, _ = fixture
    versions = {str(id): session.get(Service, id).revision for id in ids}
    consent_version, _ = consent_document(session, config)
    args = dict(
        config=config,
        owner_id=owners[1],
        project_id=projects[1],
        bot_id=123456,
        ids=ids,
        revisions=versions,
        contact="@test_client",
        comment="Личный комментарий",
        consent=True,
        consent_version=consent_version,
        action_key=f"request:{owners[1]}",
    )
    args.update(overrides)
    return submit_request(session, **args)


@pytest.mark.integration
def test_requests_require_consent_are_scoped_and_idempotent(service_fixture):
    engine, config, actors, owners, projects, ids, fields = service_fixture
    with Session(engine) as session, session.begin():
        with pytest.raises(ValueError, match="согласие"):
            submit(session, service_fixture, consent=False)
        with pytest.raises(ObjectUnavailable):
            submit(session, service_fixture, project_id=projects[0])
        with pytest.raises(ValueError, match="согласия"):
            submit(session, service_fixture, consent_version="old")
        with pytest.raises(ValueError, match="Условия"):
            submit(session, service_fixture, revisions={str(id): 0 for id in ids})
        row = submit(session, service_fixture)
        request_id, snapshot = row.id, row.items
        assert len(snapshot) == 2 and row.consent_purpose == "service_request"
        assert submit(session, service_fixture).id == request_id
        assert get_request(session, 123456, request_id, owners[1]).contact == "@test_client"
        for foreign in (owners[0], owners[2]):
            with pytest.raises(ObjectUnavailable):
                get_request(session, 123456, request_id, foreign)
        with pytest.raises(ObjectUnavailable):
            get_request(session, 999, request_id, owners[1])
        with pytest.raises(PermissionError):
            owner_request(session, config, actors[2], 123456, request_id)
        assert owner_request(session, config, actors[0], 123456, request_id).id == request_id
        save_service(session, config, actors[0], replace(fields, title="Новая версия"), ids[0], 1)
        assert row.items == snapshot and snapshot[0]["title"] == "Услуга A"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.service_request_id == request_id)
            )
            == 1
        )
        event = session.scalar(
            select(ProductEvent).where(
                ProductEvent.name == "service_requested", ProductEvent.telegram_user_id == actors[1]
            )
        )
        assert event.details == {}
        assert (
            session.scalar(
                select(func.count())
                .select_from(ServiceRequest)
                .where(ServiceRequest.owner_user_id == owners[1])
            )
            == 1
        )


@pytest.mark.integration
def test_status_changes_and_outbox_respect_owner_allowlist(service_fixture):
    engine, config, actors, owners, _, _, _ = service_fixture
    with Session(engine) as session, session.begin():
        id = submit(session, service_fixture).id
        with pytest.raises(PermissionError):
            change_status(session, config, actors[1], 123456, id, 1, "working")
        with pytest.raises(ValueError, match="изменилась"):
            change_status(session, config, actors[0], 123456, id, 0, "working")
        row = change_status(session, config, actors[0], 123456, id, 1, "working")
        assert row.revision == 2
        assert change_status(session, config, actors[0], 123456, id, 1, "working").revision == 2
        change_status(session, config, actors[0], 123456, id, 2, "closed")
        with pytest.raises(ValueError):
            change_status(session, config, actors[0], 123456, id, 3, "working")

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        with patch.object(bot, "send_message", AsyncMock()) as send:
            try:
                assert await send_one(bot, engine, config)
                assert not await send_one(bot, engine, config)
                assert send.await_count == 1
                assert send.call_args.args[0] == actors[0]
                assert "@test_client" not in send.call_args.args[1]
                assert f"ows:open:{id}" in str(send.call_args.kwargs["reply_markup"])
            finally:
                await bot.session.close()

    asyncio.run(scenario())
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "service_status_changed",
                    AuditLog.subject_user_id == owners[1],
                )
            )
            == 2
        )


@pytest.mark.integration
def test_consent_changes_withdrawal_disabled_catalog_and_deletion(service_fixture):
    engine, config, actors, owners, _, ids, _ = service_fixture
    with Session(engine) as session, session.begin():
        old, _ = consent_document(session, config)
        set_setting(
            session, config, actors[0], "service_consent_text", "Новый тестовый текст согласия"
        )
        with pytest.raises(ValueError, match="изменился"):
            submit(session, service_fixture, consent_version=old)
        set_setting(session, config, actors[0], "services_enabled", False)
        assert catalog(session) == []
        with pytest.raises(ValueError, match="отключён"):
            submit(session, service_fixture)
        set_setting(session, config, actors[0], "services_enabled", True)
        row = submit(session, service_fixture)
        id = row.id
        with pytest.raises(ObjectUnavailable):
            withdraw_consent(session, 123456, id, owners[2])
        withdraw_consent(session, 123456, id, owners[1])
        withdraw_consent(session, 123456, id, owners[1])
        assert row.consent_revoked_at and row.revision == 2
        assert (
            session.scalar(
                select(OutboxMessage.status).where(OutboxMessage.service_request_id == id)
            )
            == "cancelled"
        )
    with Session(engine) as session, session.begin():
        delete_my_data(session, actors[1])
    with Session(engine) as session:
        assert session.get(ServiceRequest, id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.service_request_id == id)
            )
            == 0
        )
        assert session.get(Service, ids[0]) is not None


@pytest.mark.integration
def test_service_wizard_resumes_and_recipient_cannot_access(service_fixture):
    engine, config, actors, owners, _, ids, _ = service_fixture

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        bot_user = User(id=bot.id, is_bot=True, first_name="Test")
        sequence, screen_id, calls = 0, 1, []

        async def api(_bot, method, timeout=None):
            nonlocal screen_id
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            screen_id = getattr(method, "message_id", None) or screen_id + 1
            return Message(
                message_id=screen_id,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def message(text, actor=actors[1]):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    message=Message(
                        message_id=100 + sequence,
                        date=datetime.now(UTC),
                        chat=Chat(id=actor, type="private"),
                        from_user=User(id=actor, first_name="Test", is_bot=False),
                        text=text,
                    ),
                ),
            )

        async def callback(value, actor=actors[1]):
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
                        from_user=User(id=actor, first_name="Test", is_bot=False),
                        message=Message(
                            message_id=screen_id,
                            date=datetime.now(UTC),
                            chat=Chat(id=actor, type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        async def draft():
            return (await storage.get_data(StorageKey(bot.id, actors[1], actors[1])))[
                "service_draft"
            ]

        async def action(command):
            await callback(f"sf:{(await draft())['revision']}:{command}")

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await message("/start")
                await callback("section:services")
                await callback("sn:list:0")
                await callback("sn:new")
                await action(f"details:{ids[0]}")
                assert "Тестовые условия" in calls[-2].text
                await action("details_close")
                await action(f"toggle:{ids[0]}")
                await action(f"toggle:{ids[1]}")
                await action("next")
                await message("not a contact")
                assert (await draft())["step"] == 1
                await message("@test_client")
                await callback("menu")
                fresh_storage = PostgresStorage(engine)
                assert (await fresh_storage.get_data(StorageKey(bot.id, actors[1], actors[1])))[
                    "service_draft"
                ]["contact"] == "@test_client"
                await message("/start")
                assert "sn:resume" not in str(calls[-1].reply_markup)
                await callback("sn:new")
                await message("Комментарий\nиз двух строк")
                await action(f"details:{ids[0]}")
                assert (await draft())["view_item"]["price_label"] == "Тестовая цена"
                await action("details_close")
                old_revision = (await draft())["revision"]
                await action("next")
                await callback(f"sf:{old_revision}:submit")
                assert calls[-1].show_alert
                with Session(engine) as session:
                    assert (
                        session.scalar(
                            select(func.count())
                            .select_from(ServiceRequest)
                            .where(ServiceRequest.owner_user_id == owners[1])
                        )
                        == 0
                    )
                submission = f"sf:{(await draft())['revision']}:submit"
                await callback(submission)
                await callback(submission)
                with Session(engine) as session:
                    row = session.scalar(
                        select(ServiceRequest).where(ServiceRequest.owner_user_id == owners[1])
                    )
                    assert len(row.items) == 2 and row.status == "new"
                    id = row.id
                    assert (
                        session.scalar(
                            select(func.count())
                            .select_from(ServiceRequest)
                            .where(ServiceRequest.owner_user_id == owners[1])
                        )
                        == 1
                    )
                await callback(f"ows:open:{id}")
                assert calls[-1].show_alert
                await message("/start", actors[0])
                await callback(f"ows:open:{id}", actors[0])
                assert "https://t.me/test_client" in str(calls[-2].reply_markup)
                await callback(f"ows:change:{id}:1:working", actors[0])
                with Session(engine) as session:
                    assert session.get(ServiceRequest, id).status == "new"
                await callback(f"ows:confirm:{id}:1:working", actors[0])
                with Session(engine) as session:
                    assert session.get(ServiceRequest, id).status == "working"
                await message("/start lm_missing", actors[2])
                await callback("sn:list:0", actors[2])
                assert calls[-1].show_alert
                await callback(f"sn:request:{id}", actors[2])
                assert calls[-1].show_alert
                await callback(f"sn:withdraw:{id}")
                await callback(f"sw:confirm:{id}")
                with Session(engine) as session:
                    assert session.get(ServiceRequest, id).consent_revoked_at
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("reason", ["owner_removed", "withdrawn"])
def test_service_notification_never_sends_without_current_permission(service_fixture, reason):
    engine, config, _, owners, _, _, _ = service_fixture
    with Session(engine) as session, session.begin():
        id = submit(session, service_fixture).id
        if reason == "withdrawn":
            withdraw_consent(session, 123456, id, owners[1])
        else:
            config = config.model_copy(update={"owner_ids": frozenset()})

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        with patch.object(bot, "send_message", AsyncMock()) as send:
            try:
                await send_one(bot, engine, config)
                send.assert_not_awaited()
            finally:
                await bot.session.close()

    asyncio.run(scenario())
    with Session(engine) as session:
        assert (
            session.scalar(
                select(OutboxMessage.status).where(OutboxMessage.service_request_id == id)
            )
            == "cancelled"
        )
