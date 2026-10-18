import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import test_links_postgres
from aiogram import Bot, Dispatcher
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leedbox.application.audience import (
    create_source,
    people_page,
    source_counts,
    source_for,
    source_page,
)
from leedbox.application.links import record_start
from leedbox.application.materials import archive, publish
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import Content
from leedbox.infrastructure.models import LinkEvent, OutboxMessage
from leedbox.telegram.delivery import find_material
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage

link_fixture = test_links_postgres.link_fixture


def material(session, owner, project, bot):
    return publish(
        session, owner, project, bot, Content("Чек-лист", "", "text", "Подарок"), "everyone", None
    )


@pytest.mark.integration
def test_sources_ownership_idempotency_history_and_legacy_delivery(link_fixture):
    engine, config, _, owners, projects = link_fixture
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        gift = material(db, owners[0], projects[0], bot_id)
        slug, material_id = gift.slug, gift.id
        args = (
            db,
            owners[0],
            bot_id,
            gift.id,
            "YouTube",
            "Видео о рекламе",
            "https://youtube.com/watch?v=example",
            "test_bot",
            "source:test:" + gift.slug,
        )
        first = create_source(*args)
        assert create_source(*args).id == first.id
        second = create_source(
            db,
            owners[0],
            bot_id,
            gift.id,
            "Дзен",
            "Статья о рекламе",
            "",
            "test_bot",
            "source:zen:" + gift.slug,
        )
        payload, first_id = first.payload, first.id
        assert len(payload) <= 64 and payload.startswith(slug + "_")
        assert first.external_url == "https://youtube.com/watch?v=example"
        assert record_start(db, bot_id, payload, 777001, "first:" + gift.slug)
        assert record_start(db, bot_id, payload, 777001, "first:" + gift.slug)
        assert record_start(db, bot_id, payload, 777001, "repeat:" + gift.slug)
        assert record_start(db, bot_id, second.payload, 777001, "second:" + gift.slug)
        db.flush()
        # Explicit dates prove history is chronological, not link creation order.
        events = db.scalars(
            select(LinkEvent)
            .where(LinkEvent.link_id.in_([first.id, second.id]))
            .order_by(LinkEvent.id)
        ).all()
        for i, event in enumerate(events):
            event.created_at = datetime.now(UTC) + timedelta(seconds=i)
        db.flush()
        assert source_counts(db, first) == (2, 1)
        people = people_page(db, first)
        assert len(people) == 1
        assert "Первый отмеченный источник: YouTube" in people[0][1]
        assert "Последний: Дзен" in people[0][1]
        assert "Выдача пока не подтверждена" in people[0][1]
        assert len(source_page(db, owners[0], bot_id, gift.id)) == 2
        for actor in owners[1:]:
            with pytest.raises(ObjectUnavailable):
                source_for(db, actor, bot_id, first.id)
            with pytest.raises(ObjectUnavailable):
                source_page(db, actor, bot_id, gift.id)
        with pytest.raises(ObjectUnavailable):
            source_for(db, owners[0], bot_id + 1, first.id)
    assert find_material(engine, bot_id, slug).id == material_id
    assert find_material(engine, bot_id, payload).id == material_id
    with Session(engine) as db, db.begin():
        archive(db, owners[0], material_id)
    assert find_material(engine, bot_id, payload) is None
    with Session(engine) as db, pytest.raises(ObjectUnavailable):
        source_for(db, owners[0], bot_id, first_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "platform,title,url",
    [
        ("", "Видео", ""),
        ("x" * 41, "Видео", ""),
        ("YouTube", "x" * 201, ""),
        ("YouTube", "Первая\nвторая", ""),
        ("Дзен", "Статья", "javascript:alert(1)"),
    ],
)
def test_invalid_source_is_not_saved(link_fixture, platform, title, url):
    engine, config, _, owners, projects = link_fixture
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        gift = material(db, owners[0], projects[0], bot_id)
        with pytest.raises(ValueError):
            create_source(
                db,
                owners[0],
                bot_id,
                gift.id,
                platform,
                title,
                url,
                "test_bot",
                "invalid:" + gift.slug,
            )
        assert not source_page(db, owners[0], bot_id, gift.id)


@pytest.mark.integration
def test_source_wizard_and_recipient_delivery_without_outbox(link_fixture):
    engine, config, actors, owners, projects = link_fixture

    async def run():
        bot = Bot(config.bot_token.get_secret_value())
        bot_user = User(id=bot.id, is_bot=True, first_name="Test", username="test_bot")
        with Session(engine) as db, db.begin():
            gift_id = material(db, owners[0], projects[0], bot.id).id
        dispatcher = Dispatcher(storage=PostgresStorage(engine))
        dispatcher.include_router(build_router(engine, config))
        state = dispatcher.fsm.get_context(bot=bot, chat_id=actors[0], user_id=actors[0])
        await state.update_data(mode="expert", paused=True)
        calls = []
        number = 0

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            return Message(
                message_id=1000 + len(calls),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def feed(text=None, callback=None, actor=None):
            nonlocal number
            number += 1
            actor = actor or actors[0]
            message = Message(
                message_id=number,
                date=datetime.now(UTC),
                chat=Chat(id=actor, type="private"),
                from_user=User(id=actor, is_bot=False, first_name="Synthetic"),
                text=text,
            )
            update = (
                Update(update_id=number, message=message)
                if text
                else Update(
                    update_id=number,
                    callback_query=CallbackQuery(
                        id=str(number),
                        chat_instance="test",
                        from_user=message.from_user,
                        data=callback,
                        message=message.model_copy(update={"from_user": bot_user}),
                    ),
                )
            )
            await dispatcher.feed_update(bot, update)

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await feed(callback=f"aud:new:{gift_id}")
                draft = (await state.get_data())["source_draft"]
                await feed(callback=f"aud:platform:yt:{draft['token']}")
                await feed(text="Видео: реклама для экспертов")
                await feed(text="javascript:bad")
                assert (await state.get_data())["source_draft"]["step"] == 2
                await feed(callback=f"aud:skip:{draft['token']}")
                with Session(engine) as db:
                    link = source_page(db, owners[0], bot.id, gift_id)[0]
                    payload, link_id = link.payload, link.id
                assert await state.get_state() is None
                panels = [c for c in calls if isinstance(c, EditMessageText)]
                assert len(panels) >= 4
                assert {c.message_id for c in panels} == {1}
                assert "Ссылка для размещения готова" in panels[-1].text
                await feed(callback=f"aud:skip:{draft['token']}")
                assert calls[-1].show_alert  # old callback cannot create another source
                with Session(engine) as db:
                    assert len(source_page(db, owners[0], bot.id, gift_id)) == 1
                before = len(calls)
                await feed(text=f"/start {payload}", actor=actors[1])
                texts = [c.text for c in calls[before:] if isinstance(c, SendMessage)]
                assert any("Подарок" in t for t in texts)
                assert not any("Источники" in t or "рассылка" in t for t in texts)
                with Session(engine) as db:
                    source = source_for(db, owners[0], bot.id, link_id)
                    assert source_counts(db, source) == (1, 1)
                    assert "Подарок получен" in people_page(db, source)[0][1]
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(OutboxMessage)
                            .where(OutboxMessage.material_id == gift_id)
                        )
                        == 0
                    )
                await feed(callback=f"aud:view:{link_id}:0")
                assert any(
                    isinstance(c, EditMessageText) and "Первый отмеченный источник" in c.text
                    for c in calls
                )
                await feed(callback=f"aud:view:{link_id}:0", actor=actors[1])
                assert calls[-1].show_alert
                await feed(callback="menu", actor=actors[1])
                assert calls[-1].show_alert  # ordinary recipients still cannot enter expert menu
                # Expert tests their own gift, then returns without typing /start again.
                await feed(text=f"/start {payload}")
                assert (await state.get_data())["mode"] == "recipient"
                assert (await state.get_data())["expert_return_allowed"] is True
                await feed(callback="menu")
                assert (await state.get_data())["mode"] == "expert"
        finally:
            await bot.session.close()

    asyncio.run(run())


@pytest.mark.integration
def test_source_and_people_pagination_and_foreign_creation(link_fixture):
    engine, config, _, owners, projects = link_fixture
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        gift = material(db, owners[0], projects[0], bot_id)
        links = [
            create_source(
                db,
                owners[0],
                bot_id,
                gift.id,
                "YouTube",
                f"Видео {i}",
                "",
                "test_bot",
                f"pagination:{gift.slug}:{i}",
            )
            for i in range(7)
        ]
        assert len(source_page(db, owners[0], bot_id, gift.id, 0)) == 6  # lookahead
        assert len(source_page(db, owners[0], bot_id, gift.id, 1)) == 2
        for i in range(7):
            record_start(db, bot_id, links[0].payload, 777000 + i, f"person:{gift.slug}:{i}")
        db.flush()
        assert len(people_page(db, links[0], 0)) == 6
        assert len(people_page(db, links[0], 1)) == 2
        assert source_counts(db, links[0]) == (7, 7)
        for owner_id in owners[1:]:
            with pytest.raises(ObjectUnavailable):
                create_source(
                    db,
                    owner_id,
                    bot_id,
                    gift.id,
                    "Дзен",
                    "Чужой подарок",
                    "",
                    "test_bot",
                    f"foreign:{gift.slug}:{owner_id}",
                )
        with pytest.raises(ValueError):
            source_page(db, owners[0], bot_id, gift.id, -1)
