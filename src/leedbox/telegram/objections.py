"""Published objection cards and durable personal answer drafts."""

import asyncio
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    Message,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.objections import save_answer
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.content import text
from leedbox.infrastructure.models import Objection, PersonalObjection
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.material_wizard import buttons
from leedbox.telegram.projects import owner
from leedbox.telegram.services import edit


def build_objection_router(engine: Engine) -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("obc:"), flags={"requires_access": True})
    async def copy_answer(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        saved = data.get("objection_copy")
        parts = (callback.data or "").split(":")
        try:
            if (
                data.get("mode") != "expert"
                or not isinstance(callback.message, Message)
                or not saved
                or len(parts) != 4
                or parts[1] != saved["token"]
            ):
                raise ObjectUnavailable
            index, page = int(parts[2]), int(parts[3])
            if not 0 <= index < len(saved["answers"]):
                raise ObjectUnavailable
            answer = saved["answers"][index]
            pages = (len(answer) + 255) // 256
            if not 0 <= page < pages:
                raise ObjectUnavailable
            chunk = answer[page * 256 : (page + 1) * 256]
            prefix = f"obc:{saved['token']}:{index}:"
            nav = []
            if page:
                nav.append(("← Предыдущая часть", prefix + str(page - 1)))
            if page + 1 < pages:
                nav.append(("Следующая часть →", prefix + str(page + 1)))
            rows = [nav] if nav else []
            rows += [[("💬 Карточка", f"ob:open:{saved['id']}")], [("🏠 В меню", "menu")]]
            markup = buttons(rows)
            markup.inline_keyboard.insert(
                0,
                [
                    InlineKeyboardButton(
                        text=f"📋 Копировать часть {page + 1}/{pages}",
                        copy_text=CopyTextButton(text=chunk),
                    )
                ],
            )
            await next_message(
                callback.message,
                f"📋 Ответ · часть {page + 1}/{pages}\n\n" + chunk,
                reply_markup=markup,
            )
            await callback.answer()
        except (ValueError, ObjectUnavailable):
            await callback.answer("Копирование устарело. Откройте карточку снова.", show_alert=True)

    @router.callback_query(F.data.startswith("ob:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        parts = (callback.data or "").split(":")
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Откройте меню эксперта", show_alert=True)
            return
        try:
            if len(parts) != 3:
                raise ObjectUnavailable
            if parts[1] == "resume":
                pending = data.get("objection_draft")
                if pending is None:
                    raise ObjectUnavailable
                pending["screen"] = callback.message.message_id
                await state.update_data(paused=False, objection_draft=pending)
                await state.set_state("objection_input")
                if pending.get("answer"):
                    await edit(
                        callback.message,
                        "Сохранить личный ответ?\n\n" + pending["answer"],
                        [
                            [("Сохранить", "ob:save:" + pending["token"])],
                            [("Назад", f"ob:write:{pending['id']}")],
                            [("В меню", "menu")],
                        ],
                    )
                else:
                    await edit(
                        callback.message,
                        "Напишите свой ответ (до 1000 символов).",
                        [[("Назад", f"ob:open:{pending['id']}")], [("В меню", "menu")]],
                    )
                await callback.answer()
                return
            if parts[1] == "list":
                page = int(parts[2])
                if not 0 <= page <= 1000:
                    raise ObjectUnavailable

                def listing() -> list[tuple[int, str]]:
                    with Session(engine) as db:
                        return [
                            (row.id, row.title)
                            for row in db.scalars(
                                select(Objection)
                                .where(Objection.enabled.is_(True))
                                .order_by(Objection.id)
                                .offset(page * 5)
                                .limit(6)
                            )
                        ]

                cards = await asyncio.to_thread(listing)
                rows = [[("💬 " + title, f"ob:open:{id}")] for id, title in cards[:5]]
                if page:
                    rows.append([("Назад", f"ob:list:{page - 1}")])
                if len(cards) > 5:
                    rows.append([("Далее", f"ob:list:{page + 1}")])
                rows += [[("← Промпты", "section:texts")], [("🏠 В меню", "menu")]]
                await state.update_data(paused=True, objection_draft=None)
                await state.set_state(None)
                await next_message(
                    callback.message,
                    f"💬 Ответы на возражения · страница {page + 1}\n\n"
                    "Выберите фразу клиента. В карточке — варианты ответа, вопрос для уточнения "
                    "и возможность сохранить свой ответ."
                    if cards
                    else "💬 Ответы пока не добавлены.",
                    reply_markup=buttons(rows),
                )
            elif parts[1] in {"open", "write"}:
                id = int(parts[2])
                if not 0 < id <= 2**63 - 1:
                    raise ObjectUnavailable

                def card() -> tuple[str, int, list[str]]:
                    with Session(engine) as db:
                        uid = owner(db, callback.from_user.id)
                        row = db.get(Objection, id)
                        if row is None or not row.enabled:
                            raise ObjectUnavailable
                        personal = db.scalar(
                            select(PersonalObjection).where(
                                PersonalObjection.owner_user_id == uid,
                                PersonalObjection.objection_id == id,
                            )
                        )
                        body = row.title + "\n\n🔎 Возможная причина\n" + row.payload["reason"]
                        body += "\n\n" + "\n\n".join(
                            f"💡 Ответ {index + 1}\n{answer}"
                            for index, answer in enumerate(row.payload["answers"])
                        )
                        body += "\n\n❓ Что уточнить у клиента\n" + row.payload["question"]
                        if personal:
                            body += "\n\nВаш ответ:\n" + personal.answer
                        answers = list(row.payload["answers"])
                        if personal:
                            answers.append(personal.answer)
                        return body, personal.revision if personal else 0, answers

                body, revision, answers = await asyncio.to_thread(card)
                if parts[1] == "write":
                    await state.update_data(
                        objection_draft={
                            "id": id,
                            "expected": revision,
                            "token": uuid4().hex[:12],
                            "screen": callback.message.message_id,
                        },
                        active_tool="objections",
                        paused=False,
                    )
                    await state.set_state("objection_input")
                    await edit(
                        callback.message,
                        "Напишите свой ответ (до 1000 символов). Исходный каталог не изменится.",
                        [[("Назад", f"ob:open:{id}")], [("В меню", "menu")]],
                    )
                else:
                    await state.update_data(paused=True, objection_draft=None)
                    await state.set_state(None)
                    token = uuid4().hex[:12]
                    await state.update_data(
                        objection_copy={"id": id, "token": token, "answers": answers}
                    )
                    rows = [
                        [("✍️ Мой вариант ответа", f"ob:write:{id}")],
                        [("← Все возражения", "ob:list:0")],
                        [("🏠 В меню", "menu")],
                    ]
                    markup = buttons(rows)
                    for index, answer in reversed(list(enumerate(answers))):
                        label = (
                            "мой ответ"
                            if index >= 3 or (revision and index == len(answers) - 1)
                            else f"ответ {index + 1}"
                        )
                        markup.inline_keyboard.insert(
                            0,
                            [
                                InlineKeyboardButton(
                                    text="📋 Копировать " + label,
                                    copy_text=CopyTextButton(text=answer),
                                )
                                if len(answer) <= 256
                                else InlineKeyboardButton(
                                    text="📋 " + label.capitalize() + " — по частям",
                                    callback_data=f"obc:{token}:{index}:0",
                                )
                            ],
                        )
                    await next_message(
                        callback.message,
                        "💬 "
                        + body[:1800]
                        + ("\n\nПолная карточка — в .md ниже." if len(body) > 1800 else ""),
                        reply_markup=markup,
                    )
                    if len(body) > 1800:
                        await callback.message.answer_document(
                            BufferedInputFile(body.encode(), filename="objection.md")
                        )
            elif parts[1] == "save":
                pending = data.get("objection_draft")
                if (
                    data.get("paused")
                    or not pending
                    or parts[2] != pending["token"]
                    or callback.message.message_id != pending["screen"]
                    or not pending.get("answer")
                ):
                    raise ObjectUnavailable

                def save() -> None:
                    with Session(engine) as db, db.begin():
                        save_answer(
                            db,
                            owner(db, callback.from_user.id),
                            pending["id"],
                            pending["answer"],
                            pending["expected"],
                        )

                await asyncio.to_thread(save)
                await state.update_data(objection_draft=None, paused=True)
                await state.set_state(None)
                await edit(
                    callback.message,
                    "Ваш ответ сохранён.",
                    [[("Карточка", f"ob:open:{pending['id']}")], [("В меню", "menu")]],
                )
            else:
                raise ObjectUnavailable
            await callback.answer()
        except (ObjectUnavailable, ValueError) as error:
            await callback.answer(str(error) or "Карточка недоступна", show_alert=True)

    @router.message(
        StateFilter("objection_input"), ~F.text.startswith("/"), flags={"requires_access": True}
    )
    async def answer(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        pending = data.get("objection_draft")
        if data.get("mode") != "expert" or data.get("paused") or not pending:
            return
        try:
            pending["answer"] = text(message.text or "", 1000)
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.update_data(objection_draft=pending)
        screen = message.model_copy(update={"message_id": pending["screen"]})
        await edit(
            screen,
            "Сохранить личный ответ?\n\n" + pending["answer"],
            [
                [("Сохранить", "ob:save:" + pending["token"])],
                [("Назад", f"ob:write:{pending['id']}")],
                [("В меню", "menu")],
            ],
        )

    return router
