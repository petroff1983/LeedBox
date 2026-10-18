import asyncio

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.library import MaterialCard, library_page, material_card
from leedbox.application.materials import archive, owned_material
from leedbox.application.notifications import queue_update
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import User
from leedbox.telegram.button_styles import button_style
from leedbox.telegram.material_ui import KINDS, LIBRARY, next_message, retire, shortcuts


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=title, callback_data=code, style=button_style(title))
                for title, code in row
            ]
            for row in rows
        ]
    )


def build_library_router(engine: Engine) -> Router:
    router = Router()

    async def edit(message: Message, text: str, rows: list[list[tuple[str, str]]]) -> Message:
        return await next_message(message, text, reply_markup=keyboard(rows))

    def owner(session: Session, telegram_id: int) -> int:
        id = session.scalar(select(User.id).where(User.telegram_id == telegram_id))
        if id is None:
            raise ObjectUnavailable
        return id

    @router.callback_query((F.data == "m:library") | F.data.startswith("lib:"))
    async def library(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if isinstance(callback.message, Message):
            await show_library(
                callback.message, callback.from_user.id, callback.data or "m:library", state, bot
            )
        await callback.answer()

    @router.message(F.text == LIBRARY)
    async def library_from_keyboard(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user:
            await show_library(message, message.from_user.id, "m:library", state, bot)

    async def show_library(
        anchor: Message, telegram_id: int, value: str, state: FSMContext, bot: Bot
    ) -> None:
        if (await state.get_data()).get("mode") != "expert":
            await anchor.answer("Для своих лид-магнитов откройте /start")
            return
        parts = value.split(":")
        if value == "m:library":
            page = 0
        elif len(parts) == 3 and parts[1] in {"a", "x"} and parts[2].isdecimal():
            page = min(int(parts[2]), 1_000_000) if parts[1] == "a" else 0
        else:
            await anchor.answer("Экран устарел. Откройте список заново.")
            return

        def read() -> tuple[list[MaterialCard], int, int]:
            with Session(engine) as session:
                owner_id = owner(session, telegram_id)
                result = library_page(session, owner_id, bot.id, False, page)
                return (
                    [material_card(session, owner_id, bot.id, id) for id, _ in result.items],
                    result.page,
                    result.total,
                )

        try:
            cards, page, total = await asyncio.to_thread(read)
        except ObjectUnavailable:
            await anchor.answer("Материалы недоступны. Откройте /start")
            return
        data = await state.get_data()
        revision = int(data.get("library_revision", 0)) + 1
        for message_id in data.get("library_item_messages", {}).values():
            await retire(bot, anchor.chat.id, message_id)
        await retire(bot, anchor.chat.id, data.get("library_message_id"))
        await state.update_data(
            paused=True,
            library_revision=revision,
            library_pending=None,
            library_item_messages={},
            library_page=page,
            material_shortcuts=True,
        )
        text = f"📚 Мои лид-магниты · {total}"
        if not cards:
            text += "\n\nПодарков пока нет. Нажмите «➕ Создать лид-магнит» на клавиатуре ниже."
        else:
            text += "\n\nСкопируйте ссылку нужного подарка в пост или кнопку вашего канала."
        await next_message(anchor, text, reply_markup=shortcuts())
        username = (await bot.me()).username if cards else None
        item_messages = {}
        for card in cards:
            sent = await anchor.answer(
                f"📌 {card.title}\n"
                f"Тип: {KINDS.get(card.kind, card.kind)}\nКанал: {card.channel}\n"
                f"Выдача: {'за подписку' if card.mode == 'subscribers' else 'без подписки'}\n\n"
                + (card.warning + "\n\n" if card.warning else "")
                + f"🔗 Ссылка для поста:\nhttps://t.me/{username}?start={card.slug}",
                reply_markup=keyboard(
                    [
                        [
                            ("📊 Подробнее", f"mat:{card.id}"),
                            ("🗑 Удалить", f"md:{card.id}:{card.version}:{revision}"),
                        ],
                        [("📍 Источники", f"aud:list:{card.id}:0")],
                    ]
                ),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            item_messages[str(card.id)] = sent.message_id
        await state.update_data(library_item_messages=item_messages)
        if total > 5:
            navigation = []
            if page:
                navigation.append(("← Назад", f"lib:a:{page - 1}"))
            if (page + 1) * 5 < total:
                navigation.append(("Далее →", f"lib:a:{page + 1}"))
            await edit(anchor, f"Страница {page + 1}/{(total + 4) // 5}", [navigation])

    @router.callback_query(
        F.data.startswith("mat:")
        | F.data.startswith("ma:")
        | F.data.startswith("mc:")
        | F.data.startswith("mn:")
        | F.data.startswith("mq:")
        | F.data.startswith("md:")
    )
    async def card(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Экран устарел. Откройте /start", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        prefix = parts[0]
        mutation = prefix in {"ma", "mc"}
        action = prefix != "mat"
        if (
            len(parts) != (5 if mutation else 4 if action else 2)
            or not parts[1].isdecimal()
            or (
                action
                and (
                    not parts[2].isdecimal()
                    or (mutation and parts[3] != "x")
                    or parts[-1] != str(data.get("library_revision"))
                    or callback.message.message_id
                    != (
                        data.get("library_item_messages", {}).get(parts[1])
                        if prefix == "md"
                        else data.get("library_message_id")
                    )
                )
            )
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        descriptor = ":".join(parts[1:-1])
        if prefix in {"ma", "mq"} and data.get("library_pending") != prefix + ":" + descriptor:
            await callback.answer("Подтвердите действие из карточки", show_alert=True)
            return
        if prefix in {"mc", "md", "mn"}:

            def check() -> MaterialCard:
                with Session(engine) as session:
                    item = material_card(
                        session, owner(session, callback.from_user.id), bot.id, int(parts[1])
                    )
                    if item.archived or item.version != int(parts[2]):
                        raise ObjectUnavailable
                    return item

            try:
                item = await asyncio.to_thread(check)
            except ObjectUnavailable:
                await callback.answer("Материал недоступен или экран устарел", show_alert=True)
                return
            if prefix == "md":
                descriptor += ":x"
            revision = int(data.get("library_revision", 0)) + 1
            target = "ma" if prefix in {"mc", "md"} else "mq"
            await state.update_data(
                library_revision=revision, library_pending=target + ":" + descriptor
            )
            text = (
                f"🗑 Удалить «{item.title}»?\n\n"
                "Лид-магнит исчезнет из списка, а ссылка перестанет выдавать подарок. "
                "Уже полученный материал у клиентов останется."
                if prefix in {"mc", "md"}
                else "Уведомить прежних получателей? Только давших отдельное согласие автору."
            )
            sent = await edit(
                callback.message,
                text,
                [
                    [
                        (
                            "🗑 Да, удалить" if target == "ma" else "Подтвердить",
                            f"{target}:{descriptor}:{revision}",
                        )
                    ],
                    [("Отмена", f"lib:a:{data.get('library_page', 0)}")],
                ],
            )
            await state.update_data(library_message_id=sent.message_id)
            await callback.answer()
            return

        def read() -> tuple[str, bool, int]:
            with Session(engine) as session, session.begin():
                owner_id = owner(session, callback.from_user.id)
                notice = ""
                if prefix == "mq":
                    count = queue_update(
                        session, owner_id, bot.id, int(parts[1]), int(parts[2]), username or ""
                    )
                    notice = f"В очередь добавлено: {count}\n\n"
                if prefix == "ma":
                    # Serialize with publication/restoration limits in the same owner lock order.
                    session.execute(select(User).where(User.id == owner_id).with_for_update())
                    material = owned_material(session, owner_id, int(parts[1]))
                    if (
                        material.bot_id != bot.id
                        or material.current_version != int(parts[2])
                        or material.archived
                    ):
                        raise ObjectUnavailable
                    archive(session, owner_id, material.id, archived=parts[3] == "x")
                result = material_card(session, owner_id, bot.id, int(parts[1]))
                if result.archived and prefix != "ma":
                    raise ObjectUnavailable
                text = (
                    notice + f"📌 {result.title}\n\n{result.description}\n\n"
                    f"Статус: {'в архиве' if result.archived else 'активен'}\n"
                    f"Версия: {result.version} · Тип: {KINDS.get(result.kind, result.kind)}\n"
                    f"Режим: {'подписчикам' if result.mode == 'subscribers' else 'всем'}\n"
                    f"Успешных выдач: {result.deliveries}\n"
                    f"Уникальных получателей: {result.recipients}\n\n"
                    f"Запусков: {result.starts}\nКанал: {result.channel}\n"
                    f"{result.warning}\n"
                    + "\n".join(f"Отказ {reason}: {count}" for reason, count in result.refusals)
                    + "\n\n"
                    f"🔗 Ссылка для поста:\nhttps://t.me/{username}?start={result.slug}\n\n"
                    "Скопируйте эту ссылку в пост или кнопку в вашем канале."
                )
                return text, result.archived, result.version

        username = (await bot.me()).username
        try:
            text, archived, version = await asyncio.to_thread(read)
        except (ObjectUnavailable, ValueError) as error:
            await callback.answer(
                str(error)
                if isinstance(error, ValueError)
                else "Материал недоступен или экран устарел",
                show_alert=True,
            )
            return
        if prefix == "ma":
            await show_library(
                callback.message,
                callback.from_user.id,
                f"lib:a:{data.get('library_page', 0)}",
                state,
                bot,
            )
            await callback.answer("Лид-магнит удалён")
            return
        revision = int(data.get("library_revision", 0)) + 1
        await state.update_data(
            paused=True,
            library_revision=revision,
            library_message_id=callback.message.message_id,
            library_pending=None,
        )
        rows = [
            [("Редактировать", f"me:{parts[1]}:{version}:{revision}")],
            [("📍 Источники", f"aud:list:{parts[1]}:0")],
            [("Уведомить об обновлении", f"mn:{parts[1]}:{version}:{revision}")],
            [
                (
                    "🗑 Удалить",
                    f"mc:{parts[1]}:{version}:x:{revision}",
                )
            ],
            [("К списку", "lib:a:0")],
            [("В меню", "menu")],
        ]
        if prefix == "mat" and data.get("source_anchor_id") == callback.message.message_id:
            try:
                edited = await callback.message.edit_text(
                    text,
                    reply_markup=keyboard(rows),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except TelegramBadRequest as error:
                edited = (
                    callback.message if "message is not modified" in str(error).lower() else False
                )
            sent = (
                edited if isinstance(edited, Message) else await edit(callback.message, text, rows)
            )
        else:
            sent = await edit(callback.message, text, rows)
        await state.update_data(library_message_id=sent.message_id)
        await callback.answer()

    return router
