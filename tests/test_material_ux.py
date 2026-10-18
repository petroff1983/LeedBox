"""Conversation contract: native buttons, chronological steps, no consent screen."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import test_services_postgres
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetChat,
    GetChatMember,
    GetMe,
    SendDocument,
    SendMessage,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    Document,
    Message,
    MessageOriginChannel,
    ReplyKeyboardMarkup,
    Update,
    User,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.infrastructure.models import DeliveryEvent, Material
from leedbox.telegram.handlers import build_router
from leedbox.telegram.material_ui import CREATE, HOME, LIBRARY
from leedbox.telegram.storage import PostgresStorage

service_fixture = test_services_postgres.service_fixture


@pytest.mark.integration
def test_lead_magnet_list_pagination_and_deleted_filter(service_fixture):
    from leedbox.application.library import library_page
    from leedbox.application.materials import archive, publish
    from leedbox.domain.materials import Content

    engine, config, _, owners, projects, *_ = service_fixture
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        ids = [
            publish(
                db,
                owners[0],
                projects[0],
                bot_id,
                Content(f"Подарок {i}", "", "url", "https://example.com/gift"),
                "everyone",
                None,
            ).id
            for i in range(6)
        ]
        publish(
            db,
            owners[1],
            projects[1],
            bot_id,
            Content("Чужой подарок", "", "text", "Не показывать"),
            "everyone",
            None,
        )
        first = library_page(db, owners[0], bot_id, False, 0)
        second = library_page(db, owners[0], bot_id, False, 1)
        assert first.total == second.total == 6
        assert len(first.items) == 5 and len(second.items) == 1
        assert {id for id, _ in first.items + second.items} == set(ids)
        assert [id for id, _ in first.items + second.items] == ids
        # Deleting the only item on the last page clamps the view to the remaining page.
        archive(db, owners[0], ids[-1], archived=True)
        after = library_page(db, owners[0], bot_id, False, 1)
        assert after.page == 0 and after.total == 5
        assert ids[-1] not in {id for id, _ in after.items}
        assert all(title != "Чужой подарок" for _, title in after.items)


@pytest.mark.integration
@pytest.mark.parametrize("kind,restricted", [("document", True), ("url", False)])
def test_native_lead_magnet_owner_and_recipient(service_fixture, kind, restricted):
    engine, config, actors, *_ = service_fixture

    async def run():
        bot = Bot(config.bot_token.get_secret_value())
        bot_user = User(id=bot.id, is_bot=True, first_name="Test bot", username="synthetic_bot")
        dispatcher = Dispatcher(storage=PostgresStorage(engine))
        dispatcher.include_router(build_router(engine, config))
        key = StorageKey(bot.id, actors[0], actors[0])
        state = dispatcher.fsm.get_context(bot=bot, chat_id=actors[0], user_id=actors[0])
        calls, screens = [], {}
        sequence, next_id = 0, 1000
        subscribed = False
        channel = Chat(
            id=-1000000000000 - actors[0],
            type="channel",
            title="Тестовый канал",
            username="fixture_channel",
        )

        async def api(_bot, method, timeout=None):
            nonlocal next_id
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, GetChat):
                assert method.chat_id == channel.id
                return channel
            if isinstance(method, GetChatMember):
                member = User(
                    id=method.user_id, is_bot=method.user_id == bot.id, first_name="Synthetic"
                )
                if method.user_id in {bot.id, actors[0]}:
                    return ChatMemberOwner(user=member, is_anonymous=False)
                return ChatMemberMember(user=member) if subscribed else ChatMemberLeft(user=member)
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            next_id += 1
            screens[int(method.chat_id)] = next_id
            return Message(
                message_id=next_id,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def message(text=None, actor=None, **extra):
            nonlocal sequence
            actor = actor or actors[0]
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    message=Message(
                        message_id=sequence,
                        date=datetime.now(UTC),
                        chat=Chat(id=actor, type="private"),
                        from_user=User(id=actor, is_bot=False, first_name="Expert"),
                        text=text,
                        **extra,
                    ),
                ),
            )

        async def callback(value, actor=None, screen=None):
            nonlocal sequence
            actor = actor or actors[0]
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        chat_instance="test",
                        data=value,
                        from_user=User(id=actor, is_bot=False, first_name="Synthetic"),
                        message=Message(
                            message_id=screen or screens[actor],
                            date=datetime.now(UTC),
                            chat=Chat(id=actor, type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        async def action(value):
            draft = (await state.get_data())["material_draft"]
            await callback(f"mw:{draft['revision']}:{value}")

        try:
            with patch.object(bot.session, "make_request", api):
                await message("/start")
                start_menu = calls[-1].reply_markup
                assert any(
                    button.text == "🎁 Лид-магниты" and button.callback_data == "section:materials"
                    for row in start_menu.inline_keyboard
                    for button in row
                )
                assert "🎁 Материалы" not in str(start_menu)
                await callback("section:materials")
                intro = [item for item in calls if isinstance(item, SendMessage)][-1]
                assert isinstance(intro.reply_markup, ReplyKeyboardMarkup)
                assert [[button.text for button in row] for row in intro.reply_markup.keyboard] == [
                    [CREATE, LIBRARY],
                    [HOME],
                ]
                await message(CREATE)
                previous = screens[actors[0]]
                draft = (await state.get_data())["material_draft"]
                stale = f"mw:{draft['revision']}:kind:{kind}"
                await action("kind:" + kind)
                assert screens[actors[0]] > previous
                await callback(stale, screen=previous)
                assert calls[-1].show_alert
                if kind == "document":
                    await message(
                        document=Document(
                            file_id="fixture-file", file_unique_id="fixture", file_name="gift.pdf"
                        )
                    )
                else:
                    await message("example.com/gift")
                    assert (await state.get_data())["material_draft"]["step"] == 1
                    assert "⚠️" in calls[-1].text and calls[-1].reply_markup
                    await message("https://example.com/gift")
                await message("Тестовый подарок")
                await action("skip")
                if restricted:
                    await action("connect")
                    await callback("m:resume")  # direct post input on channel selection also works
                    await message(
                        forward_origin=MessageOriginChannel(
                            date=datetime.now(UTC), chat=channel, message_id=7
                        )
                    )
                    assert (await state.get_data())["material_draft"]["channel_id"]
                else:
                    await action("no_channel")
                await action("mode:" + ("subscribers" if restricted else "everyone"))
                await action("publish")
                owner_messages = [
                    item
                    for item in calls
                    if isinstance(item, SendMessage) and item.chat_id == actors[0]
                ]
                assert "Ссылка для поста" in owner_messages[-1].text
                assert owner_messages[-1].link_preview_options.is_disabled
                assert not any(isinstance(item, EditMessageText) for item in calls)
                with Session(engine) as db:
                    material = db.scalar(
                        select(Material).where(Material.owner_user_id == service_fixture[3][0])
                    )
                    slug, material_id = material.slug, material.id
                recipient_start = len(calls)
                await message("/start " + slug, actor=actors[1])
                if restricted:
                    assert "Проверить подписку" in str(calls[-1].reply_markup)
                    await callback("get:" + slug, actor=actors[1])
                    assert "Подписка пока не найдена" in calls[-1].text
                    with Session(engine) as db:
                        assert (
                            db.scalar(
                                select(DeliveryEvent.id).where(
                                    DeliveryEvent.material_id == material_id
                                )
                            )
                            is None
                        )
                    # Test the real cooldown safely by releasing only this fixture bucket.
                    from sqlalchemy import delete

                    from leedbox.infrastructure.models import RateBucket

                    with Session(engine) as db, db.begin():
                        db.execute(
                            delete(RateBucket).where(
                                RateBucket.key == f"{bot.id}:{actors[1]}:subscription"
                            )
                        )
                    subscribed = True
                    delivery_start = len(calls)
                    await callback("get:" + slug, actor=actors[1])
                else:
                    delivery_start = recipient_start
                recipient_calls = calls[recipient_start:]
                gift_calls = [
                    item
                    for item in calls[delivery_start:]
                    if isinstance(item, (SendMessage, SendDocument))
                ]
                assert len(gift_calls) == 1
                gift = gift_calls[0]
                assert "Выдано через @synthetic_bot" in (
                    gift.caption if isinstance(gift, SendDocument) else gift.text
                )
                assert ("Получить ещё раз" in str(gift.reply_markup)) == restricted
                assert not any(
                    "consent:" in str(getattr(item, "reply_markup", "")) for item in recipient_calls
                )
                assert not any(
                    isinstance(getattr(item, "reply_markup", None), ReplyKeyboardMarkup)
                    for item in recipient_calls
                )
                if restricted:
                    assert any(
                        isinstance(item, SendDocument) and item.document == "fixture-file"
                        for item in recipient_calls
                    )
                else:
                    assert any(
                        isinstance(item, SendMessage)
                        and "Ваш подарок" in item.text
                        and "Открыть подарок" in str(item.reply_markup)
                        for item in recipient_calls
                    )
                with Session(engine) as db:
                    assert (
                        db.scalar(
                            select(DeliveryEvent.status).where(
                                DeliveryEvent.material_id == material_id
                            )
                        )
                        == "sent"
                    )
                await message(LIBRARY)
                listed = calls[-1]
                assert "Тестовый подарок" in listed.text
                assert f"https://t.me/synthetic_bot?start={slug}" in listed.text
                assert listed.link_preview_options.is_disabled
                assert "Архив" not in str(listed.reply_markup)
                remove = next(
                    button.callback_data
                    for row in listed.reply_markup.inline_keyboard
                    for button in row
                    if button.text == "🗑 Удалить"
                )
                # A second expert cannot open a peer's object even with a forged callback.
                await message("/start", actor=actors[2])
                await callback(f"mat:{material_id}", actor=actors[2])
                assert calls[-1].show_alert
                await callback(remove)
                assert "ссылка перестанет выдавать подарок" in calls[-2].text
                confirmation = calls[-2]
                cancel = confirmation.reply_markup.inline_keyboard[1][0].callback_data
                await callback(cancel)
                with Session(engine) as db:
                    assert not db.get(Material, material_id).archived
                listed = calls[-2]
                remove = next(
                    button.callback_data
                    for row in listed.reply_markup.inline_keyboard
                    for button in row
                    if button.text == "🗑 Удалить"
                )
                await callback(remove)
                confirm = calls[-2].reply_markup.inline_keyboard[0][0].callback_data
                await callback(confirm)
                assert "Лид-магнит удалён" in calls[-1].text
                assert "Подарков пока нет" in calls[-2].text
                await callback(confirm)
                assert calls[-1].show_alert
                with Session(engine) as db:
                    assert db.get(Material, material_id).archived
                    assert (
                        db.scalar(
                            select(DeliveryEvent.status).where(
                                DeliveryEvent.material_id == material_id
                            )
                        )
                        == "sent"
                    )
                await message("/start " + slug, actor=actors[1])
                assert calls[-1].text == "Материал сейчас недоступен"
        finally:
            await dispatcher.storage.set_state(key, None)
            await bot.session.close()

    asyncio.run(run())
