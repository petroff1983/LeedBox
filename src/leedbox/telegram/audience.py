"""Small chronological source-management flow, separate from recipient delivery."""

import asyncio
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.audience import (
    create_source,
    material_for,
    people_page,
    source_counts,
    source_for,
    source_page,
)
from leedbox.application.library import material_card
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import User
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.navigation import can_return_to_expert

PLATFORMS = {"yt": "YouTube", "zen": "Дзен", "vk": "ВКонтакте", "tg": "Telegram"}


def build_audience_router(engine: Engine) -> Router:
    router = Router()

    def owner(session: Session, actor: int) -> int:
        result = session.scalar(select(User.id).where(User.telegram_id == actor))
        if result is None:
            raise ObjectUnavailable
        return result

    async def show(
        message: Message, state: FSMContext, text: str, rows: list[list[tuple[str, str]]]
    ) -> None:
        pages = [text[start : start + 3500] for start in range(0, len(text), 3500)] or [""]
        token = uuid4().hex[:8]
        await state.update_data(
            source_text_pages=pages, source_text_rows=rows, source_text_token=token
        )
        await display_page(message, state, 0)

    async def display_page(message: Message, state: FSMContext, page: int) -> None:
        data = await state.get_data()
        pages = data["source_text_pages"]
        rows = list(data["source_text_rows"])
        token = data["source_text_token"]
        nav = []
        if page:
            nav.append(("← Назад", f"aud:text:{token}:{page - 1}"))
        if page + 1 < len(pages):
            nav.append(("Далее →", f"aud:text:{token}:{page + 1}"))
        if nav:
            rows = [nav, *rows]
        anchor = data.get("source_anchor_id")
        if message.bot and anchor:
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=anchor,
                    text=pages[page],
                    reply_markup=keyboard(rows),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
                # A deleted old card falls back to one usable replacement screen.
        sent = await next_message(message, pages[page], reply_markup=keyboard(rows))
        await state.update_data(source_anchor_id=sent.message_id)

    async def prompt(message: Message, state: FSMContext) -> None:
        draft = (await state.get_data())["source_draft"]
        step = draft["step"]
        rows = []
        if step == 0:
            text = (
                "📍 Где разместите ссылку?\n\nВыберите площадку или напишите свою (до 40 символов)."
            )
            rows = [
                [(name, f"aud:platform:{code}:{draft['token']}")]
                for code, name in PLATFORMS.items()
            ]
        elif step == 1:
            text = (
                "🎬 Как называется видео, статья или пост?\n\n"
                "Например: «YouTube — 5 ошибок в рекламе». До 200 символов, одна строка.\n"
                "Это название будет видно в статистике."
            )
        else:
            text = (
                "🔗 Отправьте адрес самого видео или статьи.\n\n"
                "Он сохранится как справочная ссылка, не изменяя выдачу подарка. Можно пропустить."
            )
            rows = [[("Пропустить", f"aud:skip:{draft['token']}")]]
        rows += [[("Отмена", f"aud:list:{draft['material_id']}:0")], [("В меню", "menu")]]
        await show(message, state, text, rows)

    async def consume(
        message: Message, state: FSMContext, bot: Bot, actor: int, value: str
    ) -> None:
        draft = dict((await state.get_data())["source_draft"])
        step = draft["step"]
        if step < 2:
            limit = 40 if step == 0 else 200
            value = value.strip()
            if not 1 <= len(value) <= limit or any(ord(char) < 32 for char in value):
                await message.answer(f"Введите одну строку длиной от 1 до {limit} символов.")
                return
            draft["platform" if step == 0 else "placement"] = value
            draft["step"] += 1
            await state.update_data(source_draft=draft)
            await prompt(message, state)
            return
        username = (await bot.me()).username or ""

        def save() -> str:
            with Session(engine) as session, session.begin():
                link = create_source(
                    session,
                    owner(session, actor),
                    bot.id,
                    draft["material_id"],
                    draft["platform"],
                    draft["placement"],
                    value.strip(),
                    username,
                    draft["action_key"],
                )
                return link.destination

        url = await asyncio.to_thread(save)
        await state.set_state(None)
        await state.update_data(paused=True, source_draft=None)
        await show(
            message,
            state,
            f"✅ Ссылка для размещения готова!\n\n📍 {draft['platform']}\n"
            f"🎬 {draft['placement']}\n\n{url}\n\n"
            "Разместите именно эту ссылку в указанной публикации. "
            "Обычная ссылка подарка продолжает работать.",
            [[("📊 Источники", f"aud:list:{draft['material_id']}:0")], [("В меню", "menu")]],
        )

    @router.callback_query(F.data.startswith("aud:"))
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if not can_return_to_expert(data) or not isinstance(callback.message, Message):
            await callback.answer("Откройте свои лид-магниты через /start", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        try:
            command = parts[1]
            if command == "text":
                if (
                    len(parts) != 4
                    or parts[2] != data.get("source_text_token")
                    or not parts[3].isdecimal()
                    or not 0 <= int(parts[3]) < len(data.get("source_text_pages", []))
                    or callback.message.message_id != data.get("source_anchor_id")
                ):
                    raise ObjectUnavailable
                await state.update_data(mode="expert")
                await display_page(callback.message, state, int(parts[3]))
            elif command in {"platform", "skip"}:
                draft = data.get("source_draft")
                if not draft or data.get("paused") or parts[-1] != draft["token"]:
                    raise ObjectUnavailable
                if command == "platform":
                    if len(parts) != 4 or draft["step"] != 0 or parts[2] not in PLATFORMS:
                        raise ObjectUnavailable
                    value = PLATFORMS[parts[2]]
                else:
                    if len(parts) != 3 or draft["step"] != 2:
                        raise ObjectUnavailable
                    value = ""
                await consume(callback.message, state, bot, callback.from_user.id, value)
            elif command == "new":
                if len(parts) != 3 or not parts[2].isdecimal():
                    raise ObjectUnavailable
                material_id = int(parts[2])

                def check() -> None:
                    with Session(engine) as session:
                        material_for(
                            session, owner(session, callback.from_user.id), bot.id, material_id
                        )

                await asyncio.to_thread(check)
                await state.set_state("source_input")
                await state.update_data(
                    mode="expert",
                    source_anchor_id=callback.message.message_id,
                    paused=False,
                    source_draft={
                        "material_id": material_id,
                        "step": 0,
                        "token": uuid4().hex[:8],
                        "action_key": "source:" + uuid4().hex,
                    },
                )
                await prompt(callback.message, state)
            elif command in {"list", "view"}:
                if len(parts) != 4 or not all(part.isdecimal() for part in parts[2:]):
                    raise ObjectUnavailable
                item_id, page = int(parts[2]), int(parts[3])

                def read() -> tuple[str, list[list[tuple[str, str]]]]:
                    with Session(engine) as session:
                        owner_id = owner(session, callback.from_user.id)
                        rows: list[list[tuple[str, str]]] = []
                        if command == "list":
                            material = material_card(session, owner_id, bot.id, item_id)
                            links = source_page(session, owner_id, bot.id, item_id, page)
                            text = f"📍 Источники · {material.title}\n\n"
                            if not links:
                                text += "Создайте отдельную ссылку для каждого видео или статьи.\n"
                            for link in links[:5]:
                                launches, unique = source_counts(session, link)
                                text += (
                                    f"• {link.source} · {link.campaign}\n"
                                    f"   Запусков: {launches} · людей: {unique}\n\n"
                                )
                                rows.append([(f"📊 {link.name}", f"aud:view:{link.id}:0")])
                            has_next = len(links) > 5
                            rows.append(
                                [("➕ Создать ссылку для размещения", f"aud:new:{item_id}")]
                            )
                            back = f"mat:{item_id}"
                        else:
                            link = source_for(session, owner_id, bot.id, item_id)
                            launches, unique = source_counts(session, link)
                            people = people_page(session, link, page)
                            text = (
                                f"📍 {link.source}\n🎬 {link.campaign}\n\n"
                                f"🔗 Ссылка для размещения:\n{link.destination}\n\n"
                            )
                            if link.external_url:
                                text += f"Публикация: {link.external_url}\n\n"
                            text += f"Запусков: {launches} · уникальных людей: {unique}\n\n"
                            text += (
                                "\n\n".join(person[1] for person in people[:5])
                                or "По этой ссылке пока никто не запускал бота."
                            )
                            text += (
                                "\n\nИсточники определяются по ссылкам. "
                                "Пересланная ссылка сохраняет исходную метку. "
                                "Статус подарка учитывает все выдачи этого материала, "
                                "не только этот источник."
                            )
                            has_next = len(people) > 5
                            back = f"aud:list:{link.material_id}:0"
                        nav = []
                        if page:
                            nav.append(("← Назад", f"aud:{command}:{item_id}:{page - 1}"))
                        if has_next:
                            nav.append(("Далее →", f"aud:{command}:{item_id}:{page + 1}"))
                        if nav:
                            rows.append(nav)
                        rows += [[("⬅️ Назад", back)], [("В меню", "menu")]]
                        return text, rows

                text, rows = await asyncio.to_thread(read)
                await state.set_state(None)
                await state.update_data(paused=True, source_draft=None)
                await state.update_data(mode="expert", source_anchor_id=callback.message.message_id)
                await show(callback.message, state, text, rows)
            else:
                raise ObjectUnavailable
        except (ValueError, ObjectUnavailable) as error:
            await callback.answer(
                str(error)
                if isinstance(error, ValueError)
                else "Источник недоступен. Откройте карточку подарка заново.",
                show_alert=True,
            )
            return
        await callback.answer()

    @router.message(StateFilter("source_input"))
    async def answer(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if not message.from_user or data.get("mode") != "expert" or data.get("paused"):
            return
        if not message.text:
            await message.answer("Отправьте текст или воспользуйтесь кнопками ниже.")
            return
        try:
            await consume(message, state, bot, message.from_user.id, message.text)
        except (ValueError, ObjectUnavailable) as error:
            await message.answer(
                str(error)
                if isinstance(error, ValueError)
                else "Материал недоступен. Вернитесь к своим лид-магнитам."
            )

    return router
