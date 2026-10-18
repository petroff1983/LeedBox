"""Telegram-native button styles, with the same semantics in every keyboard."""

from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup


def button_style(label: str) -> str | None:
    text = label.casefold()
    if any(word in text for word in ("назад", "отмена", "в меню", "главное меню", "удалить")):
        return "danger"
    if any(
        word in text
        for word in (
            "связаться",
            "написать",
            "создать",
            "получить",
            "проверить",
            "сохранить",
            "сформировать",
            "копировать",
            "скачать",
            "отправить",
            "оплатить",
            "продлить",
            "начать",
            "поехали",
            "опубликовать",
        )
    ):
        return "success"
    return None


class ButtonStyleMiddleware(BaseRequestMiddleware):
    async def __call__(
        self, make_request: NextRequestMiddlewareType[Any], bot: Bot, method: TelegramMethod[Any]
    ) -> Any:
        markup = getattr(method, "reply_markup", None)
        if isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
            rows = (
                markup.inline_keyboard
                if isinstance(markup, InlineKeyboardMarkup)
                else markup.keyboard
            )
            for row in rows:
                for button in row:
                    if button.style is None:
                        button.style = button_style(button.text)
        return await make_request(bot, method)
