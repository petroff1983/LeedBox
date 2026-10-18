import asyncio
import os

import pytest
from aiogram.fsm.storage.base import StorageKey
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application.library import library_page, material_card
from leedbox.application.materials import archive, connect_channel, publish, revise, revise_material
from leedbox.application.projects import create_project, get_project, register_user, set_archived
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import Content
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import MaterialVersion
from leedbox.telegram.delivery import claim, find_material, finish
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app


@pytest.mark.integration
def test_baseline_migration_and_readiness() -> None:
    if not os.getenv("LEEDBOX_DATABASE_URL"):
        pytest.skip("LEEDBOX_DATABASE_URL required for PostgreSQL integration test")
    settings = Settings()
    if settings.environment != "test":
        pytest.fail("Integration tests require LEEDBOX_ENVIRONMENT=test")
    if not (make_url(settings.database_url.get_secret_value()).database or "").endswith("_test"):
        pytest.fail("Destructive migration tests require a database name ending in _test")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(config).get_current_head()
            )
        assert create_app(settings, engine).test_client().get("/ready").status_code == 200

        async def draft_roundtrip() -> None:
            key = StorageKey(bot_id=123, chat_id=101, user_id=101)
            storage = PostgresStorage(engine)
            await storage.set_state(key, "project_name")
            await storage.set_data(key, {"project_id": 42, "mode": "expert"})
            restored = PostgresStorage(engine)
            assert await restored.get_state(key) == "project_name"
            assert (await restored.get_data(key))["project_id"] == 42
            other_user = StorageKey(bot_id=123, chat_id=202, user_id=202)
            other_bot = StorageKey(bot_id=456, chat_id=101, user_id=101)
            assert await restored.get_data(other_user) == {}
            assert await restored.get_data(other_bot) == {}
            await restored.set_state(key, None)
            assert await restored.get_state(key) is None
            assert (await restored.get_data(key))["project_id"] == 42

        asyncio.run(draft_roundtrip())
        with Session(engine) as session, session.begin():
            owner = register_user(session, 901)
            foreign = register_user(session, 902)
            project = create_project(session, owner.id, "Материалы")
            channel = connect_channel(session, owner.id, -100123, "Канал", "https://t.me/test")
            content = Content("Файл", "Описание", "text", "Первая версия")
            material = publish(
                session,
                owner.id,
                project.id,
                123456,
                content,
                "everyone",
                channel.id,
                publication_key="test-publication",
            )
            same = publish(
                session,
                owner.id,
                project.id,
                123456,
                content,
                "everyone",
                channel.id,
                publication_key="test-publication",
            )
            assert same.id == material.id
            slug, material_id, owner_id = material.slug, material.id, owner.id
            foreign_id, project_id = foreign.id, project.id
            with pytest.raises(ObjectUnavailable):
                revise(session, foreign.id, material.id, content)
            with pytest.raises(ObjectUnavailable):
                archive(session, foreign.id, material.id)
            with pytest.raises(ObjectUnavailable):
                connect_channel(session, foreign.id, -100123, "Чужой", "https://t.me/test")
            revise(session, owner.id, material.id, Content("Файл", "", "text", "Новая версия"))
            assert material.slug == slug
            assert len(session.query(MaterialVersion).filter_by(material_id=material.id).all()) == 2
        snapshot = find_material(engine, 123456, slug)
        assert snapshot is not None and snapshot.content == "Новая версия"
        assert find_material(engine, 456789, slug) is None
        assert claim(engine, snapshot, 909)
        assert not claim(engine, snapshot, 909)
        finish(engine, snapshot, 909, "sent", 5)
        assert not claim(engine, snapshot, 909)
        assert claim(engine, snapshot, 910)
        finish(engine, snapshot, 910, "failed")
        assert claim(engine, snapshot, 910)
        finish(engine, snapshot, 910, "uncertain")
        assert not claim(engine, snapshot, 910)
        with Session(engine) as session, session.begin():
            revise(session, owner_id, material_id, Content("Файл", "", "text", "Третья версия"))
        assert not claim(engine, snapshot, 911)
        with Session(engine) as session, session.begin():
            card = material_card(session, owner_id, 123456, material_id)
            assert card.deliveries == 1 and card.recipients == 1
            with pytest.raises(ValueError, match="уже изменён"):
                revise_material(
                    session,
                    owner_id,
                    material_id,
                    123456,
                    1,
                    Content("Новая", "", "text", "Текст"),
                    "everyone",
                    None,
                )
            with pytest.raises(ObjectUnavailable):
                material_card(session, foreign_id, 123456, material_id)
            with pytest.raises(ObjectUnavailable):
                material_card(session, owner_id, 456789, material_id)
            for index in range(6):
                publish(
                    session,
                    owner_id,
                    project_id,
                    123456,
                    Content(f"Материал {index}", "", "text", "Текст"),
                    "everyone",
                    None,
                )
            page = library_page(session, owner_id, 123456, False, 0)
            assert page.total == 7 and len(page.items) == 5
            assert len(library_page(session, owner_id, 123456, False, 1).items) == 2
            assert library_page(session, foreign_id, 123456, False, 0).total == 0
            archive(session, owner_id, material_id)
            with pytest.raises(ValueError, match="лимит"):
                archive(session, owner_id, material_id, False, limit=6)
            archive(session, owner_id, material_id, False, limit=7)
            assert material_card(session, owner_id, 123456, material_id).slug == slug
        with Session(engine) as session, session.begin():
            archive(session, owner_id, material_id)
        assert find_material(engine, 123456, slug) is None
        with Session(engine) as session, session.begin():
            users = [register_user(session, telegram_id) for telegram_id in (101, 202, 303)]
            projects = [create_project(session, user.id, "Test project") for user in users]
            assert register_user(session, 101).id == users[0].id
            for user, project in zip(users, projects, strict=True):
                assert get_project(session, user.id, project.id).id == project.id
                for foreign in users:
                    if foreign.id != user.id:
                        with pytest.raises(ObjectUnavailable):
                            get_project(session, foreign.id, project.id)
                        with pytest.raises(ObjectUnavailable):
                            set_archived(session, foreign.id, project.id, project.revision, True)
                set_archived(session, user.id, project.id, project.revision, True)
                assert project.archived
            session.rollback()
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
