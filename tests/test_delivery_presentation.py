"""A successful gift is one message, not a gift plus a duplicate success screen."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from leedbox.telegram.delivery import DeliveryMaterial
from leedbox.telegram.recipient import result


@pytest.mark.parametrize("kind", ["url", "document", "photo", "video", "text"])
def test_successful_gift_has_inline_signature_without_duplicate(kind):
    async def run():
        material = DeliveryMaterial(
            id=1,
            version_id=2,
            slug="lm_fixture",
            mode="everyone",
            content_type=kind,
            content="https://example.com/gift" if kind == "url" else "Подарок",
            title="Чек-лист",
            channel_tg_id=None,
            join_url=None,
            contact="fixture_author",
        )
        bot = Mock(id=123)
        bot.me = AsyncMock(return_value=Mock(username="fixture_bot"))
        for method in ("send_message", "send_document", "send_photo", "send_video"):
            setattr(bot, method, AsyncMock(return_value=Mock(message_id=42)))
        engine = Mock()
        with (
            patch("leedbox.telegram.delivery.find_material", return_value=material),
            patch("leedbox.telegram.delivery.claim", return_value=True),
            patch("leedbox.telegram.delivery.finish") as finish,
        ):
            await result(bot, engine, 456, "lm_fixture")
        method = {
            "url": "send_message",
            "text": "send_message",
            "document": "send_document",
            "photo": "send_photo",
            "video": "send_video",
        }[kind]
        sent = getattr(bot, method)
        assert (
            sum(
                getattr(bot, name).await_count
                for name in ("send_message", "send_document", "send_photo", "send_video")
            )
            == 1
        )
        args, kwargs = sent.await_args
        text = kwargs.get("caption", args[1])
        assert text.count("Выдано через @fixture_bot") == 1
        assert "Готово! Выдано через" not in text
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        assert any(button.text == "💬 Связаться с автором" for button in buttons)
        assert any((button.callback_data or "").startswith("again:") for button in buttons) == (
            kind != "url"
        )
        if kind == "url":
            assert any(
                button.text == "🎁 Открыть подарок" and button.url == material.content
                for button in buttons
            )
        elif kind == "text":
            assert text.startswith(material.content + "\n\n")
        finish.assert_called_once_with(engine, material, 456, "sent", 42, None)

    asyncio.run(run())
