from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import ChatMemberRestricted


class Subscription(StrEnum):
    SUBSCRIBED = "subscribed"
    NOT_SUBSCRIBED = "not_subscribed"
    BOT_NO_RIGHTS = "bot_no_rights"
    CHECK_FAILED = "check_failed"


@dataclass(frozen=True)
class VerifiedChannel:
    telegram_id: int
    title: str
    join_url: str


async def verify_channel(bot: Bot, expert_id: int, raw: str, invite: str = "") -> VerifiedChannel:
    value = raw.strip()
    if value.startswith(("https://t.me/", "http://t.me/")):
        value = "@" + urlsplit(value).path.strip("/")
    identifier: int | str = (
        int(value) if value.startswith("-100") and value[1:].isdigit() else value
    )
    try:
        chat = await bot.get_chat(identifier, request_timeout=10)
        if chat.type != "channel":
            raise ValueError("Подключить можно только Telegram-канал")
        bot_member = await bot.get_chat_member(chat.id, bot.id, request_timeout=10)
        if bot_member.status not in {"administrator", "creator"}:
            raise ValueError("Сначала добавьте бота администратором канала")
        expert_member = await bot.get_chat_member(chat.id, expert_id, request_timeout=10)
        if expert_member.status not in {"administrator", "creator"}:
            raise ValueError("Подключить канал может только его владелец или администратор")
    except TelegramAPIError:
        raise ValueError("Не удалось проверить канал и права. Попробуйте позже") from None
    if chat.username:
        join_url = f"https://t.me/{chat.username}"
    else:
        join_url = invite.strip()
        parsed = urlsplit(join_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "t.me"
            or not parsed.path.startswith(("/+", "/joinchat/"))
        ):
            raise ValueError("Для закрытого канала укажите пригласительную ссылку https://t.me/+")
    return VerifiedChannel(chat.id, chat.title or str(chat.id), join_url)


async def subscription(bot: Bot, channel_id: int, user_id: int) -> Subscription:
    try:
        member = await bot.get_chat_member(channel_id, bot.id, request_timeout=10)
        if member.status not in {"administrator", "creator"}:
            return Subscription.BOT_NO_RIGHTS
    except TelegramForbiddenError:
        return Subscription.BOT_NO_RIGHTS
    except TelegramAPIError:
        return Subscription.CHECK_FAILED
    try:
        member = await bot.get_chat_member(channel_id, user_id, request_timeout=10)
    except TelegramAPIError:
        return Subscription.CHECK_FAILED
    if member.status in {"member", "administrator", "creator"}:
        return Subscription.SUBSCRIBED
    if isinstance(member, ChatMemberRestricted) and member.is_member:
        return Subscription.SUBSCRIBED
    return Subscription.NOT_SUBSCRIBED
