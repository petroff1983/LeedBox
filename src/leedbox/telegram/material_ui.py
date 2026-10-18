"""Native Telegram presentation; never rewrite an earlier conversation step."""

from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)

CREATE = "➕ Создать лид-магнит"
LIBRARY = "📚 Мои лид-магниты"
HOME = "🏠 Главное меню"
KINDS = {
    "document": "📎 Файл",
    "photo": "🖼 Фото",
    "video": "🎬 Видео",
    "text": "📝 Текст",
    "url": "🔗 Ссылка",
}


def shortcuts() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=CREATE), KeyboardButton(text=LIBRARY)],
            [KeyboardButton(text=HOME)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или отправьте ответ",
    )


async def retire(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    # An old message can have been deleted or already have no keyboard.
    with suppress(TelegramBadRequest):
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )


async def next_message(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> Message:
    if message.bot and message.from_user and message.from_user.is_bot:
        await retire(message.bot, message.chat.id, message.message_id)
    return await message.answer(
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
