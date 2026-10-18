import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.profile import delete_my_data, update_profile
from leedbox.application.projects import (
    create_project,
    ensure_project,
    get_project,
    save_brief_answer,
    select_project,
    set_archived,
)
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import ProductEvent, Project, ProjectSelection
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage


@pytest.mark.integration
def test_projects_selection_brief_ownership_versions_and_archival(request):
    engine, _, _, owners, projects, _, _ = request.getfixturevalue("service_fixture")
    with Session(engine) as session, session.begin():
        chosen = create_project(session, owners[1], "Второй", action_key="test-new-project")
        id = chosen.id
        assert create_project(session, owners[1], "Второй", action_key="test-new-project").id == id
        with pytest.raises(ValueError):
            create_project(session, owners[1], "Другой", action_key="test-new-project")
        select_project(session, owners[1], id, 1)
        assert ensure_project(session, owners[1]).id == id
        for foreign in (owners[0], owners[2]):
            with pytest.raises(ObjectUnavailable):
                get_project(session, foreign, id)
            with pytest.raises(ObjectUnavailable):
                select_project(session, foreign, id, 1)
            with pytest.raises(ObjectUnavailable):
                save_brief_answer(session, foreign, id, 1, "niche", "Секрет")
            with pytest.raises(ObjectUnavailable):
                set_archived(session, foreign, id, 1, True)
        for invalid in (-1, 2**100):
            with pytest.raises(ObjectUnavailable):
                get_project(session, owners[1], invalid)
        save_brief_answer(session, owners[1], id, 1, "niche", "Яндекс Директ")
        assert chosen.brief == {"niche": "Яндекс Директ"} and chosen.revision == 2
        with pytest.raises(ValueError):
            save_brief_answer(session, owners[1], id, 1, "product", "Устаревший ответ")
        with pytest.raises(ValueError):
            update_profile(session, owners[1], id, "name", "Устаревшее имя", 1)
        update_profile(session, owners[1], id, "contact", "@test_author", 2)
        save_brief_answer(session, owners[1], id, 3, "evidence", "Нет подтверждений")
        assert chosen.brief["evidence"] == "Нет подтверждений"
        save_brief_answer(session, owners[1], id, 4, "niche", None)
        assert chosen.brief == {"evidence": "Нет подтверждений"}
        assert get_project(session, owners[1], projects[1]).brief == {}
        set_archived(session, owners[1], id, 5, True)
        set_archived(session, owners[1], id, 5, True)
        assert chosen.revision == 6
        with pytest.raises(ValueError):
            select_project(session, owners[1], id, 6)
        with pytest.raises(ValueError):
            save_brief_answer(session, owners[1], id, 6, "niche", "x")
        assert ensure_project(session, owners[1]).id == projects[1]
        set_archived(session, owners[1], id, 6, False)
        assert chosen.brief["evidence"] == "Нет подтверждений"
        select_project(session, owners[1], id, 7)
        assert ensure_project(session, owners[1]).id == id


@pytest.mark.integration
def test_default_project_concurrent_creation_and_delete_selection(request):
    engine, _, actors, owners, projects, _, _ = request.getfixturevalue("service_fixture")
    with Session(engine) as session, session.begin():
        set_archived(session, owners[1], projects[1], 1, True)

    def create_default(_):
        with Session(engine) as session, session.begin():
            return ensure_project(session, owners[1]).id

    with ThreadPoolExecutor(max_workers=3) as pool:
        ids = list(pool.map(create_default, range(3)))
    assert len(set(ids)) == 1
    with Session(engine) as session, session.begin():
        assert (
            session.scalar(
                select(func.count())
                .select_from(Project)
                .where(Project.owner_user_id == owners[1], Project.archived.is_(False))
            )
            == 1
        )
        assert session.get(ProjectSelection, owners[1]).project_id == ids[0]
        delete_my_data(session, actors[1])
    with Session(engine) as session:
        assert session.get(ProjectSelection, owners[1]) is None
        assert session.get(Project, ids[0]) is None
        assert session.get(Project, projects[0]) is not None


@pytest.mark.integration
def test_project_wizard_durable_brief_and_new_masters_use_selection(request):
    engine, config, actors, owners, projects, _, _ = request.getfixturevalue("service_fixture")

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        bot_user = User(id=bot.id, is_bot=True, first_name="Test")
        sequence, screen_id, calls = 0, 1, []
        key = StorageKey(bot.id, actors[1], actors[1])

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

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await message("/start")
                await callback("section:profile")
                assert "pr:list:0:0" not in str(calls[-2].reply_markup)
                assert "project:rename" not in str(calls[-2].reply_markup)
                assert "rec:off" not in str(calls[-2].reply_markup)
                assert "ob:list:0" in str(calls[-2].reply_markup)
                await callback("calc:new:ads")
                old_draft = (await storage.get_data(key))["calc_draft"]
                assert old_draft["project_id"] == projects[1]
                await callback("menu")
                await callback("project:rename")
                await callback("menu")
                await callback("pr:new")
                await callback("menu")
                fresh_storage = PostgresStorage(engine)
                assert (await fresh_storage.get_data(key))["profile_draft"]["kind"] == "new"
                await message("/start")
                assert "pr:resume" not in str(calls[-1].reply_markup)
                await callback("pr:new")
                await message("Вайбкодинг")
                with Session(engine) as session:
                    id = session.get(ProjectSelection, owners[1]).project_id
                    assert id != projects[1] and session.get(Project, id).name == "Вайбкодинг"
                await callback("project:resume")
                await message("Первый проект переименован")
                with Session(engine) as session:
                    assert session.get(Project, projects[1]).name == "Первый проект переименован"
                    assert session.get(Project, id).name == "Вайбкодинг"
                await callback(f"pr:edit:{id}:1:niche")
                await callback("menu")
                await callback("pr:resume")
                await message("Вайбкодеры")
                with Session(engine) as session:
                    assert session.get(Project, id).brief == {"niche": "Вайбкодеры"}
                await callback(f"pr:edit:{id}:1:audience")
                assert calls[-1].show_alert
                await callback(f"pr:card:{projects[0]}")
                assert calls[-1].show_alert
                await callback(f"pr:archive:{projects[0]}:1:1")
                assert calls[-1].show_alert
                await callback("pr:card:999999999999999999999999999999999999")
                assert calls[-1].show_alert
                await callback("calc:resume")
                assert (await storage.get_data(key))["calc_draft"]["project_id"] == projects[1]
                await callback("calc:restart:ads")
                assert (await storage.get_data(key))["calc_draft"]["project_id"] == id
                for command, draft_name in (
                    ("ut:new:yandex", "utm_draft"),
                    ("sn:new", "service_draft"),
                    ("m:new", "material_draft"),
                ):
                    await callback("menu")
                    await callback(command)
                    assert (await storage.get_data(key))[draft_name]["project_id"] == id
                await callback("menu")
                await callback(f"pr:edit:{id}:2:niche")
                draft = (await storage.get_data(key))["profile_draft"]
                await callback("pr:clear:" + draft["token"])
                with Session(engine) as session:
                    assert session.get(Project, id).brief == {}
                await callback("pr:clear:" + draft["token"])
                assert calls[-1].show_alert
                await callback(f"pr:archive:{id}:3:1")
                pending = (await storage.get_data(key))["profile_pending"]
                with Session(engine) as session:
                    assert not session.get(Project, id).archived
                await callback("pr:confirm:" + pending["token"])
                with Session(engine) as session:
                    assert session.get(Project, id).archived
                await message("/start lm_missing", actors[2])
                await callback("pr:list:0:0", actors[2])
                assert calls[-1].show_alert
                await callback("pr:new", actors[2])
                assert calls[-1].show_alert
                with Session(engine) as session:
                    events = list(
                        session.scalars(
                            select(ProductEvent).where(ProductEvent.telegram_user_id == actors[1])
                        )
                    )
                    assert all("Вайбкодеры" not in str(row.details) for row in events)
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())
