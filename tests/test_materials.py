import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatMember
from aiogram.types import ChatMemberMember, User

from leedbox.domain.materials import Content, http_url
from leedbox.telegram.channels import Subscription, subscription, verify_channel


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///etc/passwd", "https://", "https://x/a b"]
)
def test_material_links_reject_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError):
        http_url(url)


def test_text_material_limits() -> None:
    Content("Название", "", "text", "a" * 4000).validate()
    with pytest.raises(ValueError):
        Content("Название", "", "text", "a" * 4001).validate()
    with pytest.raises(ValueError):
        Content("a" * 61, "", "text", "Материал").validate()


def test_subscription_failure_is_not_missing_subscription() -> None:
    async def scenario() -> None:
        bot = MagicMock(id=123)
        bot.get_chat_member = AsyncMock(
            side_effect=TelegramNetworkError(
                method=GetChatMember(chat_id=-100123, user_id=123), message="network unavailable"
            )
        )
        assert await subscription(bot, -100123, 101) == Subscription.CHECK_FAILED
        bot.get_chat_member = AsyncMock(
            return_value=ChatMemberMember(user=User(id=123, first_name="Bot", is_bot=True))
        )
        assert await subscription(bot, -100123, 101) == Subscription.BOT_NO_RIGHTS

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "bot_status,expert_status,accepted",
    [
        ("administrator", "creator", True),
        ("member", "administrator", False),
        ("administrator", "member", False),
    ],
)
def test_channel_requires_bot_and_expert_privileges(bot_status, expert_status, accepted):
    async def scenario():
        bot = MagicMock(id=123)
        bot.get_chat = AsyncMock(
            return_value=SimpleNamespace(
                id=-100987, type="channel", username="test_channel", title="Канал"
            )
        )
        bot.get_chat_member = AsyncMock(
            side_effect=[SimpleNamespace(status=bot_status), SimpleNamespace(status=expert_status)]
        )
        if accepted:
            assert (await verify_channel(bot, 456, "@test_channel")).telegram_id == -100987
        else:
            with pytest.raises(ValueError):
                await verify_channel(bot, 456, "@test_channel")

    asyncio.run(scenario())
