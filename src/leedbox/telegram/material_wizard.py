import asyncio
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    User,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import can_check_subscriptions
from leedbox.application.materials import connect_channel, owned_material, publish, revise_material
from leedbox.application.projects import ensure_project, register_user
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import Content, http_url
from leedbox.infrastructure.models import Channel, MaterialVersion
from leedbox.telegram.button_styles import button_style
from leedbox.telegram.channels import verify_channel
from leedbox.telegram.material_ui import CREATE, KINDS, next_message, retire
from leedbox.telegram.texts import ui_text


def buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text, callback_data=callback, style=button_style(text))
                for text, callback in row
            ]
            for row in rows
        ]
    )


def build_material_router(engine: Engine) -> Router:
    router = Router()

    def context(telegram_id: int, username: str | None) -> tuple[int, int, list[tuple[int, str]]]:
        with Session(engine) as session, session.begin():
            user = register_user(session, telegram_id, username)
            project = ensure_project(session, user.id)
            channels = [
                (row.id, row.title)
                for row in session.scalars(
                    select(Channel).where(Channel.owner_user_id == user.id).order_by(Channel.id)
                )
            ]
            return user.id, project.id, channels

    async def render(bot: Bot, chat_id: int, state: FSMContext, error: str = "") -> None:
        data = await state.get_data()
        draft = data["material_draft"]
        step, revision = int(draft["step"]), int(draft["revision"])
        await state.set_state("material_input")
        await state.update_data(paused=False, wizard_revision=revision)
        prefix = f"mw:{revision}:"
        rows: list[list[tuple[str, str]]] = []
        editing = draft.get("material_id") is not None
        label = "✏️ Редактирование лид-магнита" if editing else "🎁 Создание лид-магнита"
        question = await asyncio.to_thread(ui_text, engine, f"material_{step}")
        text = f"{label}\nШаг {step + 1} из 7\n\n{question}"
        if error:
            text = f"⚠️ {error}\n\n" + text
        if step == 1:
            text += (
                "\n\n"
                + {
                    "document": "Прикрепите файл через 📎 → «Файл»: PDF, документ или архив.",
                    "photo": "Пришлите одно фото.",
                    "video": "Пришлите одно видео.",
                    "text": "Напишите текст подарка одним сообщением — до 4000 символов.",
                    "url": "Пришлите полную ссылку, например https://disk.yandex.ru/…\n"
                    "Проверьте, что материал доступен по ссылке.",
                }[draft["kind"]]
            )
        if step == 0:
            rows = [
                [("📎 Файл", prefix + "kind:document"), ("🖼 Фото", prefix + "kind:photo")],
                [("🎬 Видео", prefix + "kind:video"), ("📝 Текст", prefix + "kind:text")],
                [("🔗 Ссылка", prefix + "kind:url")],
            ]
        elif step == 2 and draft.get("suggested_title"):
            text += f"\nИз идеи: {draft['suggested_title']}"
            rows = [[("Оставить название идеи", prefix + "suggested")]]
        elif step == 3:
            rows = [[("Пропустить", prefix + "skip")]]
        elif step == 4:
            rows = [[(title[:30], prefix + f"channel:{id}")] for id, title in draft["channels"]]
            rows += [
                [("📢 Подключить канал", prefix + "connect")],
                [("🎁 Без канала — выдавать всем", prefix + "no_channel")],
            ]
        elif step == 5:
            if draft.get("channel_id"):
                rows.append([("✅ Только подписчикам", prefix + "mode:subscribers")])
            rows.append([("🎁 Всем без проверки", prefix + "mode:everyone")])
        elif step == 6:
            text += (
                f"\n\nНазвание: {draft['title']}\nОписание: {draft.get('description', '')}"
                f"\nТип: {KINDS[draft['kind']]}\nРежим: "
                + ("Только подписчикам" if draft["mode"] == "subscribers" else "Всем")
            )
            text += "\n\n" + (
                draft["content"][:600] if draft["kind"] in {"text", "url"} else "Медиафайл сохранён"
            )
            rows.append(
                [
                    (
                        "✅ Сохранить" if editing else "✅ Опубликовать и получить ссылку",
                        prefix + "publish",
                    )
                ]
            )
        if editing and step < 6:
            rows.insert(0, [("Оставить текущее значение", prefix + "keep")])
        if step in {5, 6} and draft.get("channel_id"):

            def checking_available() -> bool:
                with Session(engine) as session:
                    return can_check_subscriptions(session, int(draft["owner_id"]))

            if not await asyncio.to_thread(checking_available):
                text += (
                    "\n\n⚠️ Для проверки подписки нужен оплаченный доступ. "
                    "Пока подарок будет выдаваться всем по ссылке. "
                    "После оплаты выбранный режим «Только подписчикам» включится автоматически."
                )
                rows.append([("🔑 Доступ к проверке подписки", "profile:access")])
        navigation = [("❌ Отмена", prefix + "cancel")]
        if step:
            navigation.insert(0, ("← Предыдущий шаг", prefix + "back"))
        rows.append(navigation)
        await retire(bot, chat_id, data.get("screen_message_id"))
        sent = await bot.send_message(
            chat_id,
            text,
            reply_markup=buttons(rows),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        await state.update_data(screen_message_id=sent.message_id)

    @router.callback_query(
        F.data.in_({"m:new", "m:resume", "m:restart"}), flags={"requires_access": True}
    )
    async def new(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if isinstance(callback.message, Message):
            await begin(callback.message, callback.from_user, callback.data or "m:new", state, bot)
        await callback.answer()

    @router.message(F.text == CREATE, flags={"requires_access": True})
    async def new_from_keyboard(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user:
            await begin(message, message.from_user, "m:new", state, bot)

    async def begin(anchor: Message, user: User, command: str, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert":
            await anchor.answer("Для создания лид-магнита откройте /start")
            return
        if command == "m:resume" and not data.get("material_draft"):
            await anchor.answer("Черновик уже завершён. Создайте новый лид-магнит.")
            return
        if command == "m:new" and data.get("material_draft"):
            command = "m:resume"
        if command in {"m:new", "m:restart"}:
            owner_id, project_id, channels = await asyncio.to_thread(
                context, user.id, user.username
            )
            await state.update_data(
                material_draft={
                    "step": 0,
                    "revision": int(data.get("wizard_revision", 0)) + 1,
                    "owner_id": owner_id,
                    "project_id": project_id,
                    "channels": channels,
                    "channel_id": None,
                    "publication_key": uuid4().hex,
                }
            )
        await state.update_data(
            screen_message_id=anchor.message_id
            if anchor.from_user and anchor.from_user.is_bot
            else data.get("screen_message_id"),
            active_tool="material",
        )
        await render(bot, anchor.chat.id, state)

    @router.callback_query(F.data.startswith("me:"), flags={"requires_access": True})
    async def edit_material(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        parts = (callback.data or "").split(":")
        if (
            data.get("mode") != "expert"
            or not isinstance(callback.message, Message)
            or len(parts) != 4
            or not parts[1].isdecimal()
            or not parts[2].isdecimal()
            or parts[3] != str(data.get("library_revision"))
            or callback.message.message_id != data.get("library_message_id")
        ):
            await callback.answer("Экран устарел. Откройте карточку заново", show_alert=True)
            return
        if data.get("material_draft"):
            await callback.answer(
                "Сначала завершите или отмените текущий черновик", show_alert=True
            )
            return

        def read() -> dict[str, object]:
            owner_id, _, channels = context(callback.from_user.id, callback.from_user.username)
            with Session(engine) as session:
                material = owned_material(session, owner_id, int(parts[1]))
                if material.bot_id != bot.id or material.current_version != int(parts[2]):
                    raise ObjectUnavailable
                version = session.scalar(
                    select(MaterialVersion).where(
                        MaterialVersion.material_id == material.id,
                        MaterialVersion.number == material.current_version,
                    )
                )
                if version is None:
                    raise ObjectUnavailable
                return {
                    "step": 0,
                    "revision": int(data.get("wizard_revision", 0)) + 1,
                    "owner_id": owner_id,
                    "project_id": material.project_id,
                    "channels": channels,
                    "channel_id": material.channel_id,
                    "material_id": material.id,
                    "expected_version": material.current_version,
                    "kind": version.content_type,
                    "content": version.content,
                    "title": version.title,
                    "description": version.description,
                    "mode": material.delivery_mode,
                }

        try:
            draft = await asyncio.to_thread(read)
        except ObjectUnavailable:
            await callback.answer("Материал недоступен или уже изменён", show_alert=True)
            return
        await state.update_data(
            material_draft=draft,
            active_tool="material",
            screen_message_id=callback.message.message_id,
        )
        await render(bot, callback.message.chat.id, state)
        await callback.answer()

    @router.callback_query(F.data.startswith("mw:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("material_draft")
        parts = (callback.data or "").split(":")
        if (
            len(parts) < 3
            or data.get("mode") != "expert"
            or data.get("paused")
            or not draft
            or parts[1] != str(draft["revision"])
            or not isinstance(callback.message, Message)
            or callback.message.message_id != data.get("screen_message_id")
        ):
            await callback.answer("Экран устарел. Откройте раздел заново", show_alert=True)
            return
        command, step = parts[2], int(draft["step"])
        argument = parts[3] if len(parts) == 4 else ""
        if command == "cancel":
            await state.set_state(None)
            await state.update_data(
                material_draft=None, active_tool=None, wizard_revision=draft["revision"] + 1
            )
            await next_message(
                callback.message,
                "❌ Создание отменено. Подарок не опубликован.",
                reply_markup=buttons(
                    [[("📚 Мои лид-магниты", "m:library")], [("🏠 В меню", "menu")]]
                ),
            )
            await callback.answer()
            return
        if command == "keep" and draft.get("material_id") and step < 6:
            if step == 1 and not draft.get("content"):
                await callback.answer("Пришлите новое содержимое", show_alert=True)
                return
            if step == 5 and draft["mode"] == "subscribers" and not draft.get("channel_id"):
                await callback.answer("Выберите режим выдачи", show_alert=True)
                return
            draft["step"] = step + 1
        elif command == "suggested" and step == 2 and draft.get("suggested_title"):
            draft["title"], draft["step"] = draft["suggested_title"], 3
        elif command == "back" and step:
            draft["step"] = step - 1
        elif (
            command == "kind"
            and step == 0
            and argument in {"text", "url", "document", "photo", "video"}
        ):
            if argument != draft.get("kind"):
                draft.pop("content", None)
            draft["kind"], draft["step"] = argument, 1
        elif command == "skip" and step == 3:
            draft["description"], draft["step"] = "", 4
        elif command == "no_channel" and step == 4:
            draft["channel_id"], draft["step"] = None, 5
        elif command == "channel" and step == 4:
            if not argument.isdecimal():
                await callback.answer("Канал недоступен", show_alert=True)
                return
            selected = int(argument)
            if selected not in {int(row[0]) for row in draft["channels"]}:
                await callback.answer("Канал недоступен", show_alert=True)
                return
            draft["channel_id"], draft["step"] = selected, 5
        elif command == "connect" and step == 4:
            await state.set_state("material_channel")
            draft["revision"] += 1
            await state.update_data(material_draft=draft)
            sent = await next_message(
                callback.message,
                "📢 Подключение канала\n\n"
                "1. Добавьте этого бота в администраторы вашего канала.\n"
                "2. Пришлите ссылку https://t.me/my_channel, @my_channel "
                "или перешлите пост из канала.\n\n"
                "Для закрытого канала: числовой ID и пригласительная ссылка через пробел, "
                "например -1001234567890 https://t.me/+…",
                reply_markup=buttons([[("← Назад", "m:resume")], [("В меню", "menu")]]),
            )
            await state.update_data(screen_message_id=sent.message_id)
            await callback.answer()
            return
        elif command == "mode" and step == 5 and argument in {"subscribers", "everyone"}:
            if argument == "subscribers" and draft.get("channel_id") is None:
                await callback.answer("Нужен подключённый канал", show_alert=True)
                return
            draft["mode"], draft["step"] = argument, 6
        elif command == "publish" and step == 6:
            me = await bot.me()

            def save() -> str:
                with Session(engine) as session, session.begin():
                    user = register_user(
                        session, callback.from_user.id, callback.from_user.username
                    )
                    if draft.get("material_id"):
                        return revise_material(
                            session,
                            user.id,
                            int(draft["material_id"]),
                            bot.id,
                            int(draft["expected_version"]),
                            Content(
                                draft["title"],
                                draft.get("description", ""),
                                draft["kind"],
                                draft["content"],
                            ),
                            draft["mode"],
                            draft.get("channel_id"),
                        )
                    material = publish(
                        session,
                        user.id,
                        int(draft["project_id"]),
                        bot.id,
                        Content(
                            draft["title"],
                            draft.get("description", ""),
                            draft["kind"],
                            draft["content"],
                        ),
                        draft["mode"],
                        draft.get("channel_id"),
                        publication_key=draft["publication_key"],
                    )
                    return material.slug

            try:
                slug = await asyncio.to_thread(save)
            except (ValueError, ObjectUnavailable) as error:
                await callback.answer(
                    str(error) if isinstance(error, ValueError) else "Материал недоступен",
                    show_alert=True,
                )
                return
            await state.set_state(None)
            await state.update_data(
                material_draft=None, active_tool=None, wizard_revision=draft["revision"] + 1
            )
            notice = (
                "Новая версия сохранена" if draft.get("material_id") else "Материал опубликован"
            )
            await next_message(
                callback.message,
                f"✅ {notice}!\n\n🎁 {draft['title']}\n\n"
                f"🔗 Ссылка для поста:\nhttps://t.me/{me.username}?start={slug}\n\n"
                "Скопируйте ссылку и добавьте её в текст или кнопку поста в вашем канале.\n"
                "Читатель откроет бота по этой ссылке и получит именно этот подарок.",
                reply_markup=buttons(
                    [[("📚 Мои лид-магниты", "m:library")], [("🏠 В меню", "menu")]]
                ),
            )
            await callback.answer()
            return
        else:
            await callback.answer("Экран устарел", show_alert=True)
            return
        draft["revision"] += 1
        await state.update_data(material_draft=draft)
        await render(bot, callback.message.chat.id, state)
        await callback.answer()

    @router.message(
        StateFilter("material_input"), ~F.text.startswith("/"), flags={"requires_access": True}
    )
    async def input_value(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or data.get("paused") or not message.bot:
            return
        draft = data["material_draft"]
        step = int(draft["step"])
        value = (message.text or "").strip()
        if step == 4:
            await channel_input(message, state)
            return
        try:
            if step == 1:
                kind = draft["kind"]
                if kind == "document" and message.document:
                    value = message.document.file_id
                elif kind == "photo" and message.photo:
                    value = message.photo[-1].file_id
                elif kind == "video" and message.video:
                    value = message.video.file_id
                elif kind == "url":
                    value = http_url(value)
                elif kind == "text" and value and len(value) <= 4000:
                    pass
                else:
                    raise ValueError("Пришлите содержимое выбранного типа; текст до 4000 символов")
                draft["content"] = value
            elif step == 2:
                if not value or len(value) > 60:
                    raise ValueError("Название: от 1 до 60 символов")
                draft["title"] = value
            elif step == 3:
                if len(value) > 300 or not value:
                    raise ValueError("Описание до 300 символов или нажмите «Пропустить»")
                draft["description"] = value
            else:
                return
        except ValueError as error:
            await render(message.bot, message.chat.id, state, str(error))
            return
        draft["step"], draft["revision"] = step + 1, draft["revision"] + 1
        await state.update_data(material_draft=draft)
        await render(message.bot, message.chat.id, state)

    @router.message(
        StateFilter("material_channel"), ~F.text.startswith("/"), flags={"requires_access": True}
    )
    async def channel_input(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        if (
            data.get("mode") != "expert"
            or data.get("paused")
            or not message.from_user
            or not message.bot
        ):
            return
        origin = message.forward_origin
        forwarded_channel = getattr(origin, "chat", None)
        fields = (
            str(forwarded_channel.id)
            if forwarded_channel and forwarded_channel.type == "channel"
            else message.text or ""
        ).split(maxsplit=1)
        if not fields:
            await message.answer("Пришлите @имя или числовой ID канала")
            return
        try:
            verified = await verify_channel(
                message.bot, message.from_user.id, fields[0], fields[1] if len(fields) > 1 else ""
            )
            draft = data["material_draft"]

            def save() -> int:
                with Session(engine) as session, session.begin():
                    channel = connect_channel(
                        session,
                        draft["owner_id"],
                        verified.telegram_id,
                        verified.title,
                        verified.join_url,
                    )
                    return channel.id

            channel_id = await asyncio.to_thread(save)
        except ValueError as error:
            await message.answer(str(error))
            return
        draft["channels"].append([channel_id, verified.title])
        draft["channel_id"], draft["step"] = channel_id, 5
        draft["revision"] += 1
        await state.update_data(material_draft=draft)
        await render(message.bot, message.chat.id, state)

    return router
