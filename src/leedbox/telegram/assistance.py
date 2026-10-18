"""Deterministic help: never consume a request for clarification as interview data."""

import asyncio
import re
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.sections import visible_sections
from leedbox.application.settings import setting
from leedbox.domain import ai_prompts
from leedbox.domain.lead_magnet_agent import QUESTIONS
from leedbox.domain.services import owner_contact_url
from leedbox.infrastructure.models import ContentTask, User
from leedbox.telegram.library import keyboard
from leedbox.telegram.navigation import pause_input


def help_intent(raw: str | None) -> str | None:
    # Exact short requests only: an article, actual answer or quoted question is data.
    text = re.sub(r"[?!.,…]+", "", (raw or "").strip().casefold())
    text = " ".join(text.split())
    if text in {"где мои результаты", "где результаты", "мои результаты"}:
        return "results"
    if text in {"как вернуть деньги", "возврат денег", "связаться с поддержкой", "поддержка"}:
        return "support"
    if text in {"что входит в оплату", "за что я плачу", "что я оплатил"}:
        return "payment"
    if text in {
        "назад",
        "помощь",
        "помоги",
        "как пользоваться",
        "как пользоваться ботом",
        "что сюда писать",
        "что здесь писать",
        "что написать",
        "я не понял что сюда писать",
        "я не поняла что сюда писать",
        "я не понял вопрос",
        "я не поняла вопрос",
        "объясни",
        "поясни",
        "не понял",
        "не поняла",
        "непонятно",
    }:
        return "help"
    return None


def current_question(engine: Engine, data: dict[str, Any], telegram: int) -> str:
    if data.get("paused"):
        return ""
    if data.get("active_tool") == "lead_magnet_agent" and data.get("lead_magnet_token"):
        with Session(engine) as db:
            task = db.scalar(
                select(ContentTask)
                .join(User, User.id == ContentTask.owner_user_id)
                .where(ContentTask.token == data["lead_magnet_token"], User.telegram_id == telegram)
            )
            if task:
                return QUESTIONS.get(
                    task.payload.get("phase", ""),
                    "Напишите пожелания к пяти темам. Затем подтвердите доработку кнопкой.",
                )
    draft = data.get("text_draft") or {}
    step, fields = draft.get("step", 0), draft.get("fields", [])
    if data.get("active_tool") == "texts" and isinstance(step, int) and 0 <= step < len(fields):
        return ai_prompts.QUESTIONS.get(fields[step], "Ответьте своими словами на вопрос выше.")
    if data.get("active_tool") == "wallet":
        return "Введите сумму пополнения в рублях, например 500 или 500,50. Для выхода — /cancel."
    if data.get("active_tool") == "calc":
        from leedbox.telegram.calculators import SCHEMAS

        calc = data.get("calc_draft") or {}
        steps = SCHEMAS.get(calc.get("tool", ""), ())
        index = calc.get("step", 0)
        if isinstance(index, int) and 0 <= index < len(steps):
            return steps[index].prompt
    if data.get("active_tool") == "agents" and data.get("image_draft", {}).get("phase") in {
        "article",
        "confirm",
    }:
        return (
            "Пришлите статью или .txt/.md. Части собираются до кнопки «Статья отправлена». "
            "Создаются промпты, не картинки; фотореализм уже предусмотрен. "
            "На подтверждении используйте кнопки; текстовая команда стиль не меняет."
        )
    return ""


async def show_help(
    message: Message, state: FSMContext, engine: Engine, intent: str, intro: str = ""
) -> None:
    if not message.from_user or message.chat.type != "private":
        return
    data = await state.get_data()
    if data.get("mode") != "expert":
        await message.answer(
            "Для подарка воспользуйтесь кнопками в сообщении выдачи. "
            "Для инструментов эксперта отправьте /start."
        )
        return

    def settings() -> tuple[set[str], str | None]:
        with Session(engine) as db:
            return visible_sections(db), owner_contact_url(str(setting(db, "owner_contact")))

    visible, contact = await asyncio.to_thread(settings)
    rows: list[list[tuple[str, str]]] = []
    if intent == "results":
        text = (
            "📚 Ваши результаты\n\nПромпты и расчёты — в истории профиля. "
            "Результаты ИИ — в истории агентов."
        )
        if "profile" in visible:
            rows.append([("📚 Промпты и расчёты", "hr:list:0:0")])
        rows.append([("🤖 Результаты агентов", "ait:operations")])
    elif intent == "payment":
        text = (
            "💳 Доступ и баланс\n\nПодписка даёт доступ к инструментам на свой срок. "
            "У ИИ отдельный баланс токенов; стоимость показывается до запуска. "
            "Рубли на внутреннем балансе и ИИ-токены — разные счета. "
            "Промпты для вашей нейросети не запускают ИИ. "
            "Ваш срок и условия продления — в «Мой доступ»."
        )
        rows.append([("🔑 Мой доступ", "profile:access")])
    elif intent == "support":
        text = (
            "💬 Поддержка и платежи\n\nПо вопросу оплаты или возврата напишите владельцу: "
            "укажите номер заказа и проблему. Не присылайте пароли и данные карты. "
            "Это сообщение само по себе не оформляет возврат."
        )
        if not contact:
            text += "\nКонтакт поддержки пока не настроен владельцем."
    else:
        question = await asyncio.to_thread(current_question, engine, data, message.from_user.id)
        text = (
            "❓ Как пользоваться\n\nВыберите инструмент кнопкой и отвечайте на его вопросы. "
            "Это набор инструментов, а не общий чат с ИИ.\n"
            "/help — пояснение без списания токенов.\n"
            "/cancel — выйти из ввода, сохранив черновик. Запущенная задача не отменяется.\n"
            "/menu — главное меню."
        )
        if question:
            text += "\n\nСейчас нужно:\n" + question
        image = data.get("image_draft", {})
        if (
            not data.get("paused")
            and data.get("active_tool") == "agents"
            and image.get("phase") == "confirm"
        ):
            rows.append([("← Дополнить статью", f"ip:edit:{image['token']}")])
    rows.append([("🏠 Главное меню", "menu")])
    markup = keyboard(rows)
    if contact:
        markup.inline_keyboard.insert(
            0, [InlineKeyboardButton(text="💬 Написать в поддержку", url=contact)]
        )
    # Do not retire the wizard's keyboard or mutate its revision when explaining it.
    await message.answer((intro + "\n\n" if intro else "") + text, reply_markup=markup)


def install_assistance(router: Router, engine: Engine) -> None:
    @router.message(Command("help"))
    @router.message(lambda message: help_intent(message.text) is not None)
    async def help_message(message: Message, state: FSMContext) -> None:
        await show_help(message, state, engine, help_intent(message.text) or "help")

    @router.callback_query(F.data == "help:open")
    async def help_button(callback: CallbackQuery, state: FSMContext) -> None:
        if isinstance(callback.message, Message):
            # Callback message belongs to the bot; use the requester for owner-scoped help.
            message = callback.message.model_copy(update={"from_user": callback.from_user})
            await show_help(message, state, engine, "help")
        await callback.answer()

    @router.message(Command("cancel"))
    async def cancel_input(message: Message, state: FSMContext) -> None:
        if (await state.get_data()).get("mode") != "expert":
            await show_help(message, state, engine, "help")
            return
        await pause_input(state)
        await state.set_state(None)
        await state.update_data(ai_package_offer={}, wallet_offer=None, wallet_nonce=None)
        await message.answer(
            "Ввод приостановлен. Ответы и результаты сохранены. "
            "Вернуться к черновику можно через его инструмент. "
            "Уже запущенные задачи продолжают работу.",
            reply_markup=keyboard([[("🏠 Главное меню", "menu")]]),
        )


def build_fallback_router(engine: Engine) -> Router:
    router = Router(name="expert_help_fallback")

    @router.message(
        F.content_type.in_(
            {
                "text",
                "voice",
                "audio",
                "photo",
                "video",
                "document",
                "sticker",
                "video_note",
                "contact",
                "location",
            }
        )
    )
    async def unknown(message: Message, state: FSMContext) -> None:
        if (await state.get_data()).get("mode") != "expert":
            return
        intro = "Не удалось применить сообщение к текущему шагу. Это пояснение бесплатно."
        if message.voice or message.audio:
            intro += " Голосовой ввод пока не поддерживается. Отправьте текст."
        await show_help(message, state, engine, "help", intro)

    return router
