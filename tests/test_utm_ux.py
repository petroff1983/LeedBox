"""Domain-first generation uses mock Telegram and the isolated PostgreSQL fixture."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import test_links_postgres
from aiogram import Bot, Dispatcher
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    GetMe,
    SendDocument,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.domain.links import website_url
from leedbox.infrastructure.clck import ShortenedLink
from leedbox.infrastructure.models import ToolResult, TrackedLink
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage

link_fixture = test_links_postgres.link_fixture


@pytest.mark.parametrize(
    "value,expected",
    [
        ("example.ru", "https://example.ru"),
        ("пример.рф/path?x=1#top", "https://пример.рф/path?x=1#top"),
        ("example.com:8443/path", "https://example.com:8443/path"),
        ("http://example.com/path", "http://example.com/path"),
    ],
)
def test_domain_input_adds_https_only_when_missing(value, expected):
    assert website_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "ftp://example.com",
        "file:///tmp/a",
        "example.com/ bad",
        "https://user:pw@example.com",
        "hello",
    ],
)
def test_domain_input_does_not_accept_unsafe_or_non_address_text(value):
    with pytest.raises(ValueError):
        website_url(value)


@pytest.mark.integration
@pytest.mark.parametrize(
    "platform,addresses",
    [
        ("yandex", "habr.com/"),
        ("vk", "https://example.com/page?x=1#top"),
        ("yandex", "example.com/?utm_source=old&x=1#top"),
        ("yandex", "example.com/" + "a" * 300),
        ("yandex", "example.com/" + "a" * 2800),
        ("short", "https://example.com/a?x=1#top"),
    ],
)
def test_platform_domain_ready_link_without_extra_steps(link_fixture, platform, addresses):
    engine, config, actors, owners, _ = link_fixture
    config = config.model_copy(update={"clck_api_key": SecretStr("test-key")})

    async def run():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        bot_user = User(id=bot.id, is_bot=True, first_name="Synthetic", username="fixture_bot")
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        state = dispatcher.fsm.get_context(bot=bot, chat_id=actors[0], user_id=actors[0])
        calls, screen, sequence = [], 1000, 0

        async def api(_bot, method, timeout=None):
            nonlocal screen
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            screen += 1
            return Message(
                message_id=screen,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def message(text):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    message=Message(
                        message_id=sequence,
                        date=datetime.now(UTC),
                        chat=Chat(id=actors[0], type="private"),
                        from_user=User(id=actors[0], is_bot=False, first_name="Expert"),
                        text=text,
                    ),
                ),
            )

        async def callback(code, screen_id=None):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        chat_instance="fixture",
                        data=code,
                        from_user=User(id=actors[0], is_bot=False, first_name="Expert"),
                        message=Message(
                            message_id=screen_id or screen,
                            date=datetime.now(UTC),
                            chat=Chat(id=actors[0], type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        async def action(value):
            draft = (await state.get_data())["utm_draft"]
            await callback(f"ut:{draft['revision']}:{value}", draft["screen"])

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch(
                    "leedbox.application.short_links.shorten",
                    return_value=ShortenedLink("https://clck.su/test-link", "42"),
                ),
            ):
                await message("/start")
                await callback("section:links")
                menu = str(calls[-2].reply_markup)
                for action_code in (
                    "ut:new:yandex",
                    "ut:new:vk",
                    "sl:new:short",
                ):
                    assert action_code in menu
                assert "Продолжить" not in menu and "ln:new" not in menu
                await message("/utm")
                assert "sl:new:deeplink" not in str(calls[-1].reply_markup)
                await callback("sl:new:deeplink")
                assert calls[-1].show_alert
                if platform == "short":
                    await callback("sl:new:" + platform)
                    await message("ftp://example.com")
                    assert "⚠️" in calls[-1].text
                    await message(addresses)
                    gift = [call for call in calls if isinstance(call, SendMessage)][-1]
                    with Session(engine) as db:
                        links = list(
                            db.scalars(
                                select(TrackedLink).where(
                                    TrackedLink.owner_user_id == owners[0],
                                    TrackedLink.kind == platform,
                                )
                            )
                        )
                        assert len(links) == 1 and links[0].destination == addresses
                        assert links[0].external_url == "https://clck.su/test-link"
                        assert links[0].external_url in gift.text
                    assert any(
                        button.copy_text
                        for row in gift.reply_markup.inline_keyboard
                        for button in row
                    )
                    assert "Продолжить" not in str(gift.reply_markup)
                    assert "Локальная проверка" not in gift.text
                    await state.update_data(mode="recipient")
                    await callback("sl:new:" + platform)
                    assert calls[-1].show_alert
                    return
                await callback("ut:new:" + platform)
                assert (
                    "адрес вашей страницы"
                    in [c for c in calls if isinstance(c, SendMessage)][-1].text
                )
                await message("ftp://example.com")
                assert "⚠️" in calls[-1].text
                assert (await state.get_data())["utm_draft"]["phase"] == "address"
                await message("example.com/a\nexample.com/b")
                assert (await state.get_data())["utm_draft"]["phase"] == "address"
                before = (await state.get_data())["utm_draft"]
                stale_code, stale_screen = f"ut:{before['revision']}:cancel", before["screen"]
                await message(addresses)
                draft = (await state.get_data())["utm_draft"]
                if "utm_source=old" in addresses:
                    assert draft["phase"] == "replace"
                    with Session(engine) as db:
                        assert not db.scalar(
                            select(ToolResult.id).where(
                                ToolResult.owner_user_id == owners[0], ToolResult.tool == "utm"
                            )
                        )
                    await action("replace")
                assert (await state.get_data())["utm_draft"]["phase"] == "ready"
                gift = [call for call in calls if isinstance(call, SendMessage)][-1]
                assert "✅ UTM ·" in gift.text
                assert gift.parse_mode == "HTML"
                assert ("<pre>" in gift.text) == (len(addresses) < 2700)
                draft = (await state.get_data())["utm_draft"]
                result_id = draft["result_id"]
                with Session(engine) as db:
                    result = db.get(ToolResult, result_id)
                    urls = result.result.split("\n\n")
                    parsed = parse_qs(urlsplit(urls[0]).query)
                    assert len(urls) == len(addresses.splitlines())
                    assert parsed["utm_source"] == [platform]
                    if platform == "yandex":
                        assert parsed["utm_campaign"] == ["{campaign_id}"]
                        assert parsed["utm_content"] == ["{ad_id}"]
                        assert parsed["utm_term"] == ["{keyword}"]
                    else:
                        assert parsed["utm_campaign"] == ["promotion"]
                        assert parsed["utm_medium"] == ["paid_social"]
                    if "x=1" in addresses:
                        assert parsed["x"] == ["1"] and urls[0].endswith("#top")
                    assert "old" not in parsed["utm_source"]
                copies = [
                    button.copy_text.text
                    for row in gift.reply_markup.inline_keyboard
                    for button in row
                    if button.copy_text
                ]
                assert copies == ([urls[0]] if len(urls) == 1 and len(urls[0]) <= 256 else [])
                assert "Изменить" not in str(gift.reply_markup)
                assert "шаблон" not in str(gift.reply_markup)
                assert "Продолжить" not in str(gift.reply_markup)
                await callback(stale_code, stale_screen)
                assert calls[-1].show_alert
                await callback(f"ur:md:{result_id}", draft["screen"])
                assert any(isinstance(call, SendDocument) for call in calls)
                await message("/start")
                assert "resume" not in str(calls[-1].reply_markup)
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(run())
