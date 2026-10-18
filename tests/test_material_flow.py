import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application.materials import archive
from leedbox.application.profile import delete_my_data
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import (
    AccessGrant,
    AccessPolicy,
    DeliveryEvent,
    Material,
    MaterialVersion,
)
from leedbox.infrastructure.models import (
    User as DbUser,
)
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage


@pytest.mark.integration
def test_publish_and_deliver_text_through_dispatcher() -> None:
    if not os.getenv("LEEDBOX_DATABASE_URL"):
        pytest.skip("PostgreSQL required")
    settings = Settings()
    if settings.environment != "test":
        pytest.fail("Test database required")
    if not (make_url(settings.database_url.get_secret_value()).database or "").endswith("_test"):
        pytest.fail("Test database name must end with _test")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_database_engine(settings)
    with Session(engine) as session, session.begin():
        for telegram_id in (541, 542, 543):
            delete_my_data(session, telegram_id)

    async def scenario() -> None:
        bot = Bot("123456:TEST-TOKEN-ONLY")
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, settings))
        calls = []
        sequence = 0
        screen_id = 1
        bot_user = User(id=123456, is_bot=True, first_name="LeedBox", username="leedbox_test")

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

        async def message(text: str, user_id: int = 541) -> None:
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    message=Message(
                        message_id=sequence + 100,
                        date=datetime.now(UTC),
                        chat=Chat(id=user_id, type="private"),
                        from_user=User(id=user_id, is_bot=False, first_name="Expert"),
                        text=text,
                    ),
                ),
            )

        async def callback(value: str) -> None:
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        chat_instance="test",
                        data=value,
                        from_user=User(id=541, is_bot=False, first_name="Expert"),
                        message=Message(
                            message_id=screen_id,
                            date=datetime.now(UTC),
                            chat=Chat(id=541, type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        async def action(value: str) -> None:
            data = await storage.get_data(StorageKey(bot.id, 541, 541))
            await callback(f"mw:{data['material_draft']['revision']}:{value}")

        try:
            with patch.object(bot.session, "make_request", api):
                await message("/start")
                await callback("section:materials")
                await callback("m:new")
                await callback("m:resume")
                await action("kind")  # malformed callback must not raise or advance the draft
                await callback("m:new")  # starting over requires explicit confirmation
                data = await storage.get_data(StorageKey(bot.id, 541, 541))
                assert data["material_draft"]["step"] == 0
                await callback("m:resume")
                await action("kind:text")
                await message("Полезный текст")
                await message("Чек-лист")
                await action("skip")
                await action("no_channel")
                await action("mode:everyone")
                data = await storage.get_data(StorageKey(bot.id, 541, 541))
                assert data["material_draft"]["step"] == 6
                await action("publish")
                await callback("m:resume")  # completed draft must not be resumed
                with Session(engine) as session:
                    material = session.scalar(select(Material).where(Material.bot_id == bot.id))
                    assert material is not None
                    slug = material.slug
                await message(f"/start {slug}", user_id=542)
                await message(f"/start {slug}", user_id=542)
                with Session(engine) as session:
                    recipient_id = session.scalar(
                        select(DbUser.id).where(DbUser.telegram_id == 542)
                    )
                    assert recipient_id is None  # recipient route does not create an expert
                sent = [
                    call
                    for call in calls
                    if isinstance(call, SendMessage)
                    and call.text.startswith("Полезный текст\n\nВыдано через @")
                ]
                assert len(sent) == 1
                assert "Получить ещё раз" in str(sent[0].reply_markup)
                with Session(engine) as session:
                    assert (
                        session.scalar(
                            select(func.count())
                            .select_from(DeliveryEvent)
                            .where(
                                DeliveryEvent.material_id == material.id,
                                DeliveryEvent.status == "sent",
                            )
                        )
                        == 1
                    )
                await callback("m:library")
                await callback(f"mat:{material.id}")
                assert "Уникальных получателей: 1" in calls[-2].text
                data = await storage.get_data(StorageKey(bot.id, 541, 541))
                await callback(f"mc:{material.id}:1:x:{data['library_revision']}")
                data = await storage.get_data(StorageKey(bot.id, 541, 541))
                archive_callback = f"ma:{material.id}:1:x:{data['library_revision']}"
                await callback(archive_callback)
                assert "Подарков пока нет" in calls[-2].text
                await callback(archive_callback)
                assert calls[-1].show_alert  # stale mutation cannot execute twice
                await message(f"/start {slug}", user_id=542)
                assert calls[-1].text == "Материал сейчас недоступен"
                await callback("lib:x:0")  # old archive links now lead to the active list
                assert "Подарков пока нет" in calls[-2].text
                # Restoration stays a domain operation, not an exposed Telegram action.
                with Session(engine) as session, session.begin():
                    archive(session, material.owner_user_id, material.id, archived=False)
                await callback(f"mat:{material.id}")
                assert "Статус: активен" in calls[-2].text
                data = await storage.get_data(StorageKey(bot.id, 541, 541))
                await callback(f"me:{material.id}:1:{data['library_revision']}")
                await action("keep")  # current type
                await message("Обновлённый текст")
                await action("keep")  # title
                await callback("menu")
                restored = PostgresStorage(engine)
                assert (await restored.get_data(StorageKey(bot.id, 541, 541)))["material_draft"][
                    "step"
                ] == 3
                await message("/start")
                await callback("m:resume")
                await action("keep")  # description
                await action("keep")  # channel
                await action("keep")  # mode
                await action("publish")
                with Session(engine) as session:
                    updated = session.get(Material, material.id)
                    assert updated.slug == slug and updated.current_version == 2
                    assert (
                        session.scalar(
                            select(MaterialVersion.content).where(
                                MaterialVersion.material_id == material.id,
                                MaterialVersion.number == 1,
                            )
                        )
                        == "Полезный текст"
                    )
                await message(f"/start {slug}", user_id=542)
                assert any(
                    isinstance(call, SendMessage)
                    and call.text.startswith("Обновлённый текст\n\nВыдано через @")
                    for call in calls
                )
                # Simulate an expired expert to verify the actual middleware, not only domain rules.
                with Session(engine) as session, session.begin():
                    owner_id = session.scalar(select(DbUser.id).where(DbUser.telegram_id == 541))
                    trial = session.scalar(
                        select(AccessGrant).where(AccessGrant.user_id == owner_id)
                    )
                    trial.ends_at = trial.starts_at
                    config = session.get(AccessPolicy, 1)
                    config.mode, config.activation_grace_days = "paid", 0
                    config.paid_enabled_at = None
                try:
                    await callback("m:new")
                    assert calls[-1].show_alert and "Доступ закончился" in calls[-1].text
                    await callback("profile:access")
                    assert "Подписка не активирована" in calls[-2].text
                    await message(f"/start {slug}", user_id=543)
                    assert any(
                        isinstance(call, SendMessage)
                        and call.chat_id == 543
                        and call.text.startswith("Обновлённый текст\n\nВыдано через @")
                        for call in calls
                    )
                finally:
                    with Session(engine) as session, session.begin():
                        session.get(AccessPolicy, 1).mode = "free"
                await message("/start")
                await callback("section:profile")
                profile = calls[-2]
                assert isinstance(profile, SendMessage) and "👤 Профиль" in profile.text
                assert "Проект:" not in profile.text and "Часовой пояс:" not in profile.text
                assert "Контакт для получателей: не задан" in profile.text
                labels = [
                    button.text for row in profile.reply_markup.inline_keyboard for button in row
                ]
                actions = {
                    button.callback_data
                    for row in profile.reply_markup.inline_keyboard
                    for button in row
                }
                assert actions == {
                    "ob:list:0",
                    "project:contact",
                    "notify:open",
                    "account:open",
                    "purchase:orders:0",
                    "help:open",
                    "legal:open",
                    "profile:delete",
                    "menu",
                }
                assert "✉️ Контакт для получателей" in labels
                await callback("project:contact")
                assert isinstance(calls[-2], SendMessage)
                assert "Связаться с автором" in calls[-2].text
                assert "Контакт необязателен" in calls[-2].text
                await message("@test_author")
                assert isinstance(calls[-1], SendMessage)
                assert "Контакт сохранён: @test_author" in calls[-1].text
                await callback("section:profile")
                assert "Контакт для получателей: @test_author" in calls[-2].text
                await callback("project:timezone")
                await message("Europe/Moscow")
                await message("/calc")
                await callback("calc:new:ads")
                data = await storage.get_data(StorageKey(bot.id, 541, 541))
                await callback(f"cw:{data['calc_draft']['revision']}:platform:yandex")
                for answer in ("30к", "1000", "33%", "10%", "15"):
                    await message(answer)
                assert "Рекламный бюджет: 13 636,36 ₽" in calls[-1].text
                data = await storage.get_data(StorageKey(bot.id, 541, 541))
                await callback(f"cr:export:{data['calc_draft']['result_id']}")
                assert calls[-2].__class__.__name__ == "SendDocument"
        finally:
            await bot.session.close()

    try:
        asyncio.run(scenario())
    finally:
        with Session(engine) as session, session.begin():
            for telegram_id in (541, 542, 543):
                delete_my_data(session, telegram_id)
        engine.dispose()
