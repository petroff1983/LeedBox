import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from secrets import randbelow

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application.entitlements import (
    access_for,
    configure_policy,
    extend_access,
    start_expert,
)
from leedbox.application.materials import connect_channel, publish
from leedbox.application.profile import delete_my_data
from leedbox.application.projects import create_project, register_user
from leedbox.domain.materials import Content
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import AccessGrant, AuditLog
from leedbox.telegram.delivery import find_material


@pytest.mark.integration
def test_access_start_settings_expiry_and_extension():
    if not os.getenv("LEEDBOX_DATABASE_URL"):
        pytest.skip("PostgreSQL required")
    settings = Settings(owner_ids=frozenset({999}))
    assert settings.environment == "test"
    assert (make_url(settings.database_url.get_secret_value()).database or "").endswith("_test")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_database_engine(settings)
    now = datetime.now(UTC)
    first_actor = 4500000000 + randbelow(100000000)
    second_actor = first_actor + 100000000
    channel_id = -1000000000000 - first_actor

    def configure(session, mode="paid", price=Decimal("1234.50"), **overrides):
        values = dict(
            mode=mode,
            price=price,
            currency="RUB",
            period_days=30,
            trial_days=60,
            grace_days=0,
            activation_grace_days=0,
            expired_delivery="bypass",
            now=now,
        )
        values.update(overrides)
        configure_policy(session, settings, 999, **values)

    try:
        with Session(engine) as session, session.begin():
            user = register_user(session, first_actor)
            user_id = user.id
            assert user.expert_started_at is None
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AccessGrant)
                    .where(AccessGrant.user_id == user.id)
                )
                == 0
            )
            start_expert(session, user, now - timedelta(days=100))
            start_expert(session, user, now)
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AccessGrant)
                    .where(AccessGrant.user_id == user.id)
                )
                == 1
            )
            assert access_for(session, user.id, now).state == "free"
            project = create_project(session, user.id, "Доступ")
            channel = connect_channel(session, user.id, channel_id, "Канал", "https://t.me/channel")
            material = publish(
                session,
                user.id,
                project.id,
                123450,
                Content("Материал", "", "text", "Текст"),
                "subscribers",
                channel.id,
            )
            slug = material.slug
            configure(session)
            assert access_for(session, user.id, now).state == "expired"
            with pytest.raises(ValueError, match="Доступ закончился"):
                publish(
                    session,
                    user.id,
                    project.id,
                    123450,
                    Content("Новый", "", "text", "Текст"),
                    "everyone",
                    None,
                )
            with pytest.raises(PermissionError):
                extend_access(session, settings, 998, user.id, 30, "unauthorized", now)
        assert find_material(engine, 123450, slug).mode == "everyone"
        with Session(engine) as session, session.begin():
            first = extend_access(
                session, settings, 999, user_id, 30, f"extension-{first_actor}", now
            )
            duplicate = extend_access(
                session, settings, 999, user_id, 30, f"extension-{first_actor}", now
            )
            assert first.id == duplicate.id and first.ends_at == now + timedelta(days=30)
            second = extend_access(
                session, settings, 999, user_id, 10, f"extension-{first_actor}-b", now
            )
            assert second.ends_at == now + timedelta(days=40)
            configure(session, price=Decimal("9876.1234"))
            assert access_for(session, user_id, now).ends_at == second.ends_at
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.subject_user_id == user_id)
                )
                == 2
            )
            fresh = register_user(session, second_actor)
            start_expert(session, fresh, now)
            assert access_for(session, fresh.id, now).ends_at == now + timedelta(days=60)
        assert find_material(engine, 123450, slug).mode == "subscribers"
    finally:
        with Session(engine) as session, session.begin():
            configure(session, mode="free")
            delete_my_data(session, first_actor)
            delete_my_data(session, second_actor)
        engine.dispose()
