"""Platform → one domain → one UTM URL. Saved results retain read/export compatibility."""

import asyncio
from html import escape
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    LinkPreviewOptions,
    Message,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.links import save_utm, utm_batch
from leedbox.application.projects import ensure_project
from leedbox.application.results import get_result
from leedbox.application.settings import product_event
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.links import PRESETS, existing_utm, website_url
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import User
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import retire

PLATFORMS = {"yandex": "Яндекс Директ", "vk": "ВКонтакте"}


def build_utm_router(engine: Engine, settings: Settings) -> Router:
    router = Router()

    def owner(session: Session, telegram_id: int) -> int:
        value = session.scalar(select(User.id).where(User.telegram_id == telegram_id))
        if value is None:
            raise ObjectUnavailable
        return value

    async def screen(
        bot: Bot,
        state: FSMContext,
        chat_id: int,
        draft: dict[str, Any],
        text: str,
        rows: list[list[tuple[str, str]]],
        *,
        copy: str | None = None,
        code: str | None = None,
    ) -> None:
        await retire(bot, chat_id, draft.get("screen"))
        markup = keyboard(rows)
        if copy and len(copy) <= 256:
            markup.inline_keyboard.insert(
                0,
                [
                    InlineKeyboardButton(
                        text="📋 Копировать ссылку",
                        copy_text=CopyTextButton(text=copy),
                    )
                ],
            )
        formatted = escape(text)
        if code:
            formatted = formatted.replace(escape(code), f"<pre>{escape(code)}</pre>", 1)
        first, _, body = formatted.partition("\n")
        formatted = f"<b>{first}</b>\n{body}"
        sent = await bot.send_message(
            chat_id,
            formatted,
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        draft["screen"] = sent.message_id
        await state.update_data(utm_draft=draft, active_tool="utm", paused=False)

    async def render(bot: Bot, state: FSMContext, chat_id: int, error: str = "") -> None:
        draft = (await state.get_data())["utm_draft"]
        if not draft.get("quick") or draft.get("phase") not in {"address", "replace", "ready"}:
            await state.update_data(paused=True)
            await bot.send_message(
                chat_id, "Этот мастер убран. Откройте /utm и выберите Директ или ВК."
            )
            return
        prefix = f"ut:{draft['revision']}:"
        phase, values = draft["phase"], draft["values"]
        rows: list[list[tuple[str, str]]] = []
        copy = None
        code = None
        if phase == "address":
            text = (
                f"🔗 UTM · {PLATFORMS[draft['platform']]}\n\n🌐 Отправьте адрес вашей страницы.\n"
            )
            text += "Например: example.ru или https://example.ru/course\n\n"
            text += "Если протокол не указан, добавлю https://. "
            await state.set_state("utm_input")
        elif phase == "replace":
            text = "⚠️ В адресе уже есть UTM-метки.\n\nЗаменить их новыми? "
            text += "Остальные параметры страницы и якорь сохранятся."
            rows.append([("✅ Заменить UTM", prefix + "replace")])
            rows.append([("🌐 Изменить адрес", prefix + "address")])
            await state.set_state("utm_choose")
        else:

            def save() -> tuple[int, str]:
                with Session(engine) as session, session.begin():
                    user_id = owner(session, chat_id)
                    if draft.get("result_id"):
                        result = get_result(session, user_id, bot.id, draft["result_id"])
                    else:
                        result = save_utm(
                            session,
                            user_id,
                            draft["project_id"],
                            bot.id,
                            {**values, "platform": draft["platform"]},
                            f"utm-quick:{draft['token']}:{draft['revision']}",
                        )
                    return result.id, result.result

            result_id, full = await asyncio.to_thread(save)
            draft["result_id"] = result_id
            platform = draft["platform"]
            visible = len(full) <= 2800
            if visible:
                code = full
            copy = full
            visible_link = full if visible else "Длинная ссылка — в файле MD."
            text = f"✅ UTM · {PLATFORMS[platform]}\n\n{visible_link}"
            text += "\n\nВставьте ссылку в рекламное объявление."
            if platform == "yandex":
                text += " Директ подставит ID кампании, объявления и ключевую фразу при переходе."
            else:
                text += " Метки ВК статические: vk / paid_social / promotion."
            rows = [[("🔗 Другие ссылки", "section:links")]]
            if len(full) > 256:
                text += "\n\nДлинную ссылку скопируйте из блока или скачайте файл."
                rows.insert(0, [("📄 Скачать ссылку", f"ur:md:{result_id}")])
            await state.set_state(None)
        if phase != "ready":
            rows.append([("❌ Отмена", prefix + "cancel")])
        rows.append([("🏠 В меню", "menu")])
        if error:
            text = "⚠️ " + error + "\n\n" + text
        await screen(bot, state, chat_id, draft, text, rows, copy=copy, code=code)
        if phase == "ready":
            from leedbox.telegram.monetization import recommend

            await recommend(engine, settings, bot, chat_id, state, "utm")

    async def begin(
        bot: Bot,
        state: FSMContext,
        message: Message,
        telegram_id: int,
        platform: str | None = None,
    ) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert":
            await message.answer("Для инструментов откройте /start")
            return
        old = data.get("utm_draft")
        if platform not in {"yandex", "vk"}:
            from leedbox.telegram.links import menu_rows

            await retire(bot, telegram_id, old.get("screen") if old else None)
            await state.update_data(paused=True)
            await message.answer(
                "🔗 Ссылки\n\nВыберите инструмент — затем пришлите адрес.",
                reply_markup=keyboard(menu_rows()),
            )
            return

        if (
            old
            and old.get("quick")
            and old.get("platform") == platform
            and old.get("phase") != "ready"
        ):
            old["revision"] = int(data.get("utm_revision", old["revision"])) + 1
            await state.update_data(utm_draft=old, utm_revision=old["revision"])
            await render(bot, state, telegram_id)
            return

        def context() -> int:
            with Session(engine) as session, session.begin():
                project = ensure_project(session, owner(session, telegram_id))
                product_event(session, bot.id, telegram_id, "tool_started", tool="utm")
                return project.id

        draft: dict[str, Any] = {
            "values": {},
            "phase": "address",
            "platform": platform,
            "quick": True,
            "revision": int(data.get("utm_revision", 0)) + 1,
            "token": uuid4().hex,
            "project_id": await asyncio.to_thread(context),
            "screen": message.message_id
            if message.from_user and message.from_user.is_bot
            else old.get("screen")
            if old
            else None,
        }
        await state.update_data(utm_draft=draft, utm_revision=draft["revision"])
        await render(bot, state, telegram_id)

    @router.message(Command("utm"), flags={"requires_access": True})
    async def command(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user:
            await begin(bot, state, message, message.from_user.id)

    @router.callback_query(
        (F.data == "ut:new") | F.data.startswith("ut:new:"), flags={"requires_access": True}
    )
    async def new(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if isinstance(callback.message, Message):
            platform = (callback.data or "").split(":")[-1]
            if platform not in {"yandex", "vk", "new"}:
                await callback.answer("Выберите Директ или ВК", show_alert=True)
                return
            await begin(bot, state, callback.message, callback.from_user.id, platform)
        await callback.answer()

    @router.callback_query(F.data == "ut:resume", flags={"requires_access": True})
    async def resume(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not data.get("utm_draft"):
            await callback.answer("Черновик недоступен", show_alert=True)
            return
        draft = data["utm_draft"]
        draft["revision"] += 1
        await state.update_data(utm_draft=draft, utm_revision=draft["revision"])
        await render(bot, state, callback.from_user.id)
        await callback.answer()

    @router.callback_query(F.data.startswith("ut:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("utm_draft")
        parts = (callback.data or "").split(":")
        if (
            data.get("mode") != "expert"
            or not draft
            or len(parts) != 3
            or parts[1] != str(draft["revision"])
            or not isinstance(callback.message, Message)
            or callback.message.message_id != draft.get("screen")
            or (data.get("paused") and parts[2] not in {"restart", "cancel"})
        ):
            await callback.answer("Экран устарел. Откройте /utm", show_alert=True)
            return
        value, phase = parts[2], draft["phase"]
        if value in {"cancel", "restart"}:
            await retire(bot, callback.from_user.id, draft.get("screen"))
            await state.set_state(None)
            await state.update_data(
                utm_draft=None, utm_revision=draft["revision"] + 1, active_tool=None, paused=True
            )
            if value == "restart":
                await begin(bot, state, callback.message, callback.from_user.id)
            else:
                await callback.message.answer(
                    "Создание UTM-ссылки отменено.",
                    reply_markup=keyboard([[("🏠 В меню", "menu")]]),
                )
            await callback.answer()
            return
        if value == "address":
            draft["phase"] = "address"
        elif value == "replace" and phase == "replace":
            draft["values"]["replace"] = True
            draft["phase"] = "ready"
        else:
            await callback.answer("Экран устарел", show_alert=True)
            return
        if draft["phase"] == "ready":
            try:
                utm_batch(
                    draft["values"]["urls"],
                    draft["values"],
                    replace=draft["values"].get("replace") is True,
                )
            except ValueError as error:
                await callback.answer(str(error), show_alert=True)
                return
        draft["revision"] += 1
        if value in {"replace", "address"}:
            draft.pop("result_id", None)
        await state.update_data(utm_draft=draft, utm_revision=draft["revision"])
        await render(bot, state, callback.from_user.id)
        await callback.answer()

    @router.message(
        StateFilter("utm_input"), ~F.text.startswith("/"), flags={"requires_access": True}
    )
    async def input_value(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("utm_draft")
        if data.get("mode") != "expert" or data.get("paused") or not draft:
            return
        try:
            if not message.text:
                raise ValueError("Пришлите адрес или значение текстом")
            if draft["phase"] == "address":
                values = [
                    website_url(value) for value in message.text.splitlines() if value.strip()
                ]
                if len(values) != 1:
                    raise ValueError("Пришлите один адрес страницы")
                if len(values[0]) > 4000:
                    raise ValueError("Адрес не должен превышать 4000 символов")
                draft["values"] = {
                    "urls": "\n".join(values),
                    "replace": False,
                    "name": (urlsplit(values[0]).hostname or "UTM")[:60],
                }
                draft["values"].update(PRESETS[draft["platform"]])
                if draft["platform"] == "vk":
                    draft["values"]["campaign"] = "promotion"
                draft["phase"] = "replace" if existing_utm(values[0]) else "ready"
                if draft["phase"] == "ready":
                    utm_batch(draft["values"]["urls"], draft["values"], replace=False)
            else:
                return
        except ValueError as error:
            await render(bot, state, message.chat.id, str(error))
            return
        draft["revision"] += 1
        draft.pop("result_id", None)
        await state.update_data(utm_draft=draft, utm_revision=draft["revision"])
        await render(bot, state, message.chat.id)

    return router
