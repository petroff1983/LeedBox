import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import test_admin_postgres
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.partners import PartnerInput
from leedbox.application.settings import set_setting, setting
from leedbox.application.storefront import install, products, validate_products
from leedbox.application.versions import fingerprint
from leedbox.domain.services import storefront_url
from leedbox.infrastructure.models import Service
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage
from leedbox.telegram.storefront import product_note, product_pages

admin_fixture = test_admin_postgres.admin_fixture


def item(**changes):
    return {
        **asdict(PartnerInput("Тестовый товар", "Описание", "https://example.com")),
        "code": "sample",
        "sort_order": 0,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "javascript:alert(1)"},
        {"enabled": True},
        {"name": "a" * 65},
        {"sort_order": True},
        {"code": "../../other"},
    ],
)
def test_invalid_product_rejected(changes):
    with pytest.raises(ValueError):
        validate_products([item(**changes)])


def test_product_gate_and_unique_codes():
    assert validate_products([item()])[0]["enabled"] is False
    assert validate_products(
        [
            item(
                enabled=True,
                advertiser="Тестовый рекламодатель",
                inn="1234567890",
                erid="test-erid",
                marking_text="Реклама",
            )
        ]
    )
    with pytest.raises(ValueError):
        validate_products([item(), item()])


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///tmp/test", "https://"])
def test_storefront_contact_rejects_unsafe_url(url):
    with pytest.raises(ValueError):
        storefront_url(url, contact=True)


def test_storefront_links_and_compact_pages_keep_marking():
    assert storefront_url("@test_owner", contact=True) == "https://t.me/test_owner"
    assert (
        storefront_url("https://example.com/contact", contact=True) == "https://example.com/contact"
    )
    assert storefront_url("") is None
    drafts = [item(code=f"draft-{index}") for index in range(7)]
    assert len(product_pages(drafts)) == 1
    marked = [
        item(
            code=f"marked-{index}",
            enabled=True,
            advertiser="a" * 300,
            inn="1234567890",
            erid="e" * 200,
            marking_text="Реклама " + "x" * 400,
        )
        for index in range(7)
    ]
    pages = product_pages(marked)
    assert len(pages) > 1
    assert sum(map(len, pages)) == 7
    assert "1234567890" in product_note(marked[0]) and "erid" in product_note(marked[0])


@pytest.mark.integration
def test_product_editor_publication_preview_conflicts_and_seed(admin_fixture):
    client, csrf, config, context, _ = admin_fixture
    engine, _, actors, *_ = context
    with Session(engine) as db:
        before = set(db.scalars(select(Service.id)))
    try:
        with Session(engine) as db, db.begin():
            install(db, config, actors[0])
            assert len(products(db, True)) == 7 and products(db) == []
            seeded = set(db.scalars(select(Service.id)))
            first = db.get(Service, next(iter(seeded - before)))
            first.title = "Имя изменено владельцем"
            db.flush()
            install(db, config, actors[0])
            assert set(db.scalars(select(Service.id))) == seeded
        assert client.get("/owner/products/").status_code == 200
        assert client.get("/owner/products/edit?code=syntx").status_code == 200
        assert client.get("/owner/settings/edit?key=store_products").status_code == 302
        with Session(engine) as db:
            original = setting(db, "store_products")
            source = next(row for row in original if row["code"] == "syntx")
        payload = {
            **source,
            "expected": fingerprint(original),
            "csrf_token": csrf,
            "confirmed": "1",
            "action": "save",
            "enabled": "1",
        }
        url = "/owner/products/edit?code=syntx"
        assert client.post(url, data={**payload, "csrf_token": "bad"}).status_code == 403
        assert "маркировку" in client.post(url, data=payload).text
        marked = {
            **payload,
            "advertiser": "Тестовый рекламодатель",
            "inn": "1234567890",
            "erid": "synthetic-erid",
            "marking_text": "Реклама",
        }
        assert client.post(url, data={**marked, "action": "preview"}).status_code == 200
        with Session(engine) as db:
            assert products(db) == []
        assert client.post(url, data=marked).status_code == 302
        with Session(engine) as db:
            assert len(products(db)) == 1
        assert "изменилась" in client.post(url, data=marked).text
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(Service).where(Service.id.not_in(before)))


@pytest.mark.integration
def test_storefront_screens_owner_command_and_recipient_isolation(admin_fixture):
    _, _, config, context, _ = admin_fixture
    engine, _, actors, *_ = context
    with Session(engine) as db, db.begin():
        set_setting(
            db,
            config,
            actors[0],
            "store_products",
            [item(code=f"p{index}", name=f"Товар {index}", sort_order=index) for index in range(7)],
        )

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        storage = PostgresStorage(engine)
        dp = Dispatcher(storage=storage)
        dp.include_router(build_router(engine, config))
        calls, sequence = [], 0
        bot_user = User(id=123456, is_bot=True, first_name="Test", username="testbot")

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            return Message(
                message_id=len(calls),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def feed(value, actor, command=False):
            nonlocal sequence
            sequence += 1
            user = User(id=actor, is_bot=False, first_name="Test")
            message = Message(
                message_id=sequence,
                date=datetime.now(UTC),
                chat=Chat(id=actor, type="private"),
                from_user=user,
                text=value,
            )
            if command:
                update = Update(update_id=sequence, message=message)
            else:
                update = Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        from_user=user,
                        chat_instance="test",
                        data=value,
                        message=message.model_copy(update={"from_user": bot_user}),
                    ),
                )
            await dp.feed_update(bot, update)

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await feed("/start", actors[1], True)
                await feed("section:services", actors[1])
                menu = calls[-2]
                assert isinstance(menu, SendMessage)
                assert {
                    button.callback_data
                    for row in menu.reply_markup.inline_keyboard
                    for button in row
                } == {"sn:list:0", "store:products", "menu"}
                await feed("store:products", actors[1])
                assert not any(
                    item.url == "https://example.com"
                    for call in calls
                    if isinstance(call, SendMessage) and call.reply_markup
                    for row in call.reply_markup.inline_keyboard
                    for item in row
                )
                await feed("/start", actors[0], True)
                start = len(calls)
                await feed("store:products", actors[0])
                messages = [call for call in calls[start:] if isinstance(call, SendMessage)]
                assert len(messages) == 1 and "Черновик" in messages[0].text
                assert "Описание" not in messages[0].text
                grid = messages[0].reply_markup.inline_keyboard
                assert [len(row) for row in grid[:-1]] == [2, 2, 2, 1]
                assert all(button.url for row in grid[:-1] for button in row)
                await feed("sn:list:0", actors[1])
                assert calls[-2].reply_markup.inline_keyboard[1][0].text == "ℹ️ Обо мне"
                with Session(engine) as db, db.begin():
                    set_setting(
                        db, config, actors[0], "service_contact_url", "https://example.com/contact"
                    )
                    set_setting(
                        db, config, actors[0], "service_about_url", "https://example.com/about"
                    )
                start = len(calls)
                await feed("sn:list:0", actors[1])
                service_messages = [call for call in calls[start:] if isinstance(call, SendMessage)]
                assert len(service_messages) == 1
                assert service_messages[0].text.count("Что получите:") == 2
                assert "━━━━━━━━━━━━━━━━━━" in service_messages[0].text
                button = service_messages[0].reply_markup.inline_keyboard[0][0]
                assert button.text == "💬 Связаться"
                assert button.url == "https://example.com/contact"
                assert button.style == "success"
                assert service_messages[0].reply_markup.inline_keyboard[-1][0].style == "danger"
                assert (
                    service_messages[-1].reply_markup.inline_keyboard[1][0].url
                    == "https://example.com/about"
                )
                assert "sn:new" not in str([call.reply_markup for call in service_messages])
                await feed("section:consultations", actors[1])
                content = getattr(calls[-2], "caption", None) or calls[-2].text
                assert "1 000 ₽" in content and "6️⃣" in content
                assert calls[-2].reply_markup.inline_keyboard[0][0].url == "https://t.me/test_owner"
                await feed("/owner", actors[1], True)
                assert calls[-1].text == "Доступ закрыт"
                await feed("/owner", actors[0], True)
                assert "последние 24 часа" in calls[-1].text
                from aiogram.fsm.storage.base import StorageKey

                await storage.update_data(
                    StorageKey(bot.id, actors[2], actors[2]), {"mode": "recipient"}
                )
                for value in ("store:products", "section:consultations", "section:services"):
                    start = len(calls)
                    await feed(value, actors[2])
                    assert not any(isinstance(call, SendMessage) for call in calls[start:])
                    assert calls[-1].show_alert
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())
