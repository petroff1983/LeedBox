"""Seven deterministic questions, explicit paid adaptation and reusable saved prompt."""

import asyncio
from contextlib import suppress
from typing import Any
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application import agents, ai_tokens, ai_usage, writing_library
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.writing_profile import FIELDS
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import User
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message

LABELS = dict(
    zip(
        (key for key, _ in FIELDS),
        (
            "Тематика",
            "Аудитория и цель",
            "Повествование и обращение",
            "Стиль",
            "Объём",
            "Факты автора",
            "Особые правила",
        ),
        strict=True,
    )
)


def build_writing_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    def owner(db: Session, telegram: int) -> int:
        result = db.scalar(select(User.id).where(User.telegram_id == telegram))
        if result is None:
            raise ObjectUnavailable
        return result

    async def question(message: Message, draft: dict[str, Any]) -> None:
        index, token = draft["index"], draft["token"]
        rows = [[("← Предыдущий вопрос", f"wp:back:{token}")]] if index else []
        await next_message(
            message,
            f"📝 Промпт моего канала · {index + 1}/{len(FIELDS)}\n\n"
            + FIELDS[index][1]
            + "\n\nОтвет: до 600 символов. Опрос без ИИ и списаний.",
            reply_markup=keyboard(rows + [[("← Агенты", "section:agents")]]),
        )

    async def confirmation(
        message: Message, draft: dict[str, Any], telegram: int, bot: int, state: FSMContext
    ) -> None:
        with Session(engine) as db:
            enough, payment = agents.payment_preview(
                db, config, owner(db, telegram), bot, draft["offer"]
            )
        await state.update_data(writing_draft=draft)
        maximum = draft["offer"]["maximum"]
        buttons = (
            [[("✨ Создать и сохранить промпт", f"wp:generate:{draft['token']}")]]
            if enough
            else [[("🪙 Купить токены", "ait:packages")]]
        )
        await next_message(
            message,
            "📝 Вводные собраны\n\n"
            + "\n".join(
                f"{LABELS[k]}: {v[:250]}" + ("…" if len(v) > 250 else "")
                for k, v in draft["brief"].items()
            )
            + f"\n\nНа создание нужно до {maximum:,} токенов.\n"
            + payment
            + "\n"
            "Предыдущий промпт останется доступным, если новая генерация не удастся."
            + ("\nНедостаточно токенов для резерва." if not enough else ""),
            reply_markup=keyboard(
                buttons
                + [
                    [("← Изменить последний ответ", f"wp:back:{draft['token']}")],
                    [("← Агенты", "section:agents")],
                ]
            ),
        )

    @router.message(
        StateFilter("agent_writing_profile", "agent_writing_name"),
        ~F.text.startswith("/"),
        flags={"requires_access": True},
    )
    async def answer(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if (
            message.chat.type != "private"
            or data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "agents"
        ):
            return
        if await state.get_state() == "agent_writing_name":
            raw = (message.text or "").strip()
            try:
                assert message.from_user is not None
                number = int(data["writing_rename_id"])
                with Session(engine) as db, db.begin():
                    writing_library.rename(
                        db,
                        owner(db, message.from_user.id),
                        bot.id,
                        agents.test_mode(config),
                        number,
                        raw,
                    )
                await state.set_state(None)
                await message.answer(
                    "✅ Название сохранено",
                    reply_markup=keyboard(
                        [
                            [("📝 Открыть промпт", f"wp:view:{number}")],
                            [("📂 Мои промпты", "wp:open")],
                        ]
                    ),
                )
            except (ValueError, ObjectUnavailable) as error:
                await message.answer("⚠️ " + (str(error) or "Промпт недоступен"))
            return
        draft = data.get("writing_draft", {})
        if draft.get("phase") != "questions":
            return
        raw = (message.text or "").strip()
        if not 1 <= len(raw) <= 600:
            await message.answer("Ответ должен содержать 1–600 символов. Попробуйте ещё раз.")
            return
        index = draft["index"]
        brief = {**draft["brief"], FIELDS[index][0]: raw}
        draft = {**draft, "brief": brief, "index": index + 1}
        if draft["index"] == len(FIELDS):
            with Session(engine) as db:
                offer = agents.quote_profile(db, brief, draft.get("search_token"))
            draft = {**draft, "phase": "confirm", "offer": offer}
            await state.set_state(None)
        await state.update_data(writing_draft=draft)
        if draft["phase"] == "confirm":
            assert message.from_user is not None
            await confirmation(message, draft, message.from_user.id, bot.id, state)
        else:
            await question(message, draft)

    @router.callback_query(F.data.startswith("wp:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data, message = await state.get_data(), callback.message
        if (
            not isinstance(message, Message)
            or message.chat.type != "private"
            or data.get("mode") != "expert"
        ):
            await callback.answer("Откройте /start", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        code = parts[1]
        draft = data.get("writing_draft", {})
        try:
            with Session(engine, expire_on_commit=False) as db, db.begin():
                user = owner(db, callback.from_user.id)
                writing_library.ensure(db, user, bot.id, agents.test_mode(config))
                selected = (
                    writing_library.get(db, user, bot.id, agents.test_mode(config), int(parts[2]))
                    if len(parts) == 3
                    and code in {"view", "download", "rename", "remove", "remove_yes", "clone"}
                    else None
                )
                saved = (
                    agents.operation(
                        db, user, bot.id, agents.test_mode(config), selected.operation_id
                    )
                    if selected
                    else None
                )
                saved_prompt = (saved.result or {}).get("prompt") if saved else None
            if code == "download":
                if not saved_prompt:
                    raise ValueError("Сначала создайте промпт")
                await message.answer_document(
                    BufferedInputFile(saved_prompt.encode(), filename="my-dzen-prompt.md"),
                    caption="Вставьте заголовок вместо [ВСТАВЬТЕ ЗАГОЛОВОК]. Списаний нет.",
                )
            elif code in {"open", "page"}:
                index = int(parts[2]) if code == "page" and len(parts) == 3 else 0
                with Session(engine) as db:
                    prompts = writing_library.page(
                        db, user, bot.id, agents.test_mode(config), index
                    )
                    rows = [[(f"📝 {p.name}", f"wp:view:{p.id}")] for p in prompts[:7]]
                    navigation = []
                    if index:
                        navigation.append(("← Ранее", f"wp:page:{index - 1}"))
                    if len(prompts) > 7:
                        navigation.append(("Далее →", f"wp:page:{index + 1}"))
                    if navigation:
                        rows.append(navigation)
                if draft.get("phase") in {"questions", "confirm"}:
                    rows.append([("↩ Продолжить настройку", "wp:resume")])
                rows += [
                    [("➕ Создать новый промпт", "wp:edit")],
                    [("← Агенты", "section:agents")],
                ]
                analysis = data.get("agent_draft", {})
                if analysis.get("operation_id"):
                    rows.insert(-1, [("← К заголовкам", f"ait:result:{analysis['operation_id']}")])
                await state.update_data(writing_library_page=index)
                await state.set_state(None)
                await next_message(
                    message,
                    "📂 Мои промпты\n\n"
                    "Выберите нужную тематику или создайте новый промпт.\n"
                    "Открытие и скачивание без списаний.",
                    reply_markup=keyboard(rows),
                )
            elif code == "view" and selected and saved:
                await state.set_state(None)
                await next_message(
                    message,
                    "📝 "
                    + selected.name
                    + "\n\n"
                    + "Тематика: "
                    + str(saved.snapshot["inputs"]["brief"]["topic"])[:600]
                    + "\n\nВставьте любой выбранный заголовок в промпт. Открытие бесплатно.",
                    reply_markup=keyboard(
                        [
                            [("📥 Скачать промпт .md", f"wp:download:{selected.id}")],
                            [("🌐 Открыть и скопировать", f"ait:report:{saved.id}")],
                            [("✏️ Переименовать", f"wp:rename:{selected.id}")],
                            [("➕ Создать на основе", f"wp:clone:{selected.id}")],
                            [("Убрать из списка", f"wp:remove:{selected.id}")],
                            [
                                (
                                    "← К списку промптов",
                                    f"wp:page:{data.get('writing_library_page', 0)}",
                                )
                            ],
                        ]
                    ),
                )
            elif code == "rename" and selected:
                await state.update_data(
                    writing_rename_id=selected.id, active_tool="agents", paused=False
                )
                await state.set_state("agent_writing_name")
                await message.answer(
                    "✏️ Напишите новое название (до 80 символов).",
                    reply_markup=keyboard([[("← Мои промпты", "wp:open")]]),
                )
            elif code == "remove" and selected:
                await message.answer(
                    "Убрать «" + selected.name + "» из библиотеки? "
                    "Версия останется в истории результатов.",
                    reply_markup=keyboard(
                        [
                            [("Убрать", f"wp:remove_yes:{selected.id}")],
                            [("Отмена", f"wp:view:{selected.id}")],
                        ]
                    ),
                )
            elif code == "remove_yes" and selected:
                with Session(engine) as db, db.begin():
                    writing_library.get(
                        db, user, bot.id, agents.test_mode(config), selected.id
                    ).archived = True
                await message.answer(
                    "Промпт убран из списка.",
                    reply_markup=keyboard([[("📂 Мои промпты", "wp:open")]]),
                )
            elif code in {"edit", "clone", "resume", "back"}:
                with Session(engine) as db:
                    agents.allowed(db, user, bot.id, agents.test_mode(config), config=config)
                if code in {"edit", "clone"}:
                    draft = {
                        "token": uuid4().hex[:12],
                        "index": 0,
                        "phase": "questions",
                        "search_token": data.get("agent_draft", {}).get("token")
                        if data.get("agent_draft", {}).get("report")
                        else None,
                        "brief": dict(saved.snapshot["inputs"]["brief"])
                        if code == "clone" and saved
                        else {},
                    }
                elif code == "back":
                    if (
                        len(parts) != 3
                        or parts[2] != draft.get("token")
                        or draft.get("phase") not in {"questions", "confirm"}
                    ):
                        raise ValueError("Экран устарел. Откройте «Мой промпт»")
                    draft = {**draft, "index": max(0, draft["index"] - 1), "phase": "questions"}
                elif draft.get("phase") not in {"questions", "confirm"}:
                    raise ValueError("Нет незавершённой настройки")
                if code == "resume" and draft.get("phase") == "confirm":
                    with Session(engine) as db:
                        previous = ai_tokens.previous(
                            db, user, bot.id, agents.test_mode(config), "writing:" + draft["token"]
                        )
                        if previous is None or previous.status == "failed":
                            offer = agents.quote_profile(
                                db, draft["brief"], draft.get("search_token")
                            )
                            if offer != draft.get("offer") or previous is not None:
                                draft = {**draft, "offer": offer, "token": uuid4().hex[:12]}
                await state.update_data(writing_draft=draft, active_tool="agents", paused=False)
                await state.set_state(
                    "agent_writing_profile" if draft["phase"] == "questions" else None
                )
                if draft["phase"] == "confirm":
                    await confirmation(message, draft, callback.from_user.id, bot.id, state)
                else:
                    await question(message, draft)
            elif code == "generate":
                if (
                    len(parts) != 3
                    or parts[2] != draft.get("token")
                    or draft.get("phase") != "confirm"
                    or data.get("paused")
                    or data.get("active_tool") != "agents"
                ):
                    raise ValueError("Экран устарел. Откройте «Мой промпт»")
                await state.update_data(writing_draft={**draft, "phase": "working"})
                with suppress(TelegramBadRequest):
                    await callback.answer()
                await message.answer(
                    "⏳ Создаю подробный промпт. Дождитесь результата; повторный запуск не нужен."
                )
                try:
                    number = await asyncio.to_thread(
                        agents.generate,
                        engine,
                        config,
                        user,
                        bot.id,
                        "writing:" + draft["token"],
                        draft["offer"],
                        require_confirmation=True,
                    )
                except ValueError:
                    with Session(engine) as db:
                        failed = ai_tokens.previous(
                            db, user, bot.id, agents.test_mode(config), "writing:" + draft["token"]
                        )
                        if failed is not None and failed.status == "failed":
                            draft = {**draft, "token": uuid4().hex[:12]}
                    await state.update_data(writing_draft={**draft, "phase": "confirm"})
                    raise
                with Session(engine) as db:
                    row = agents.operation(db, user, bot.id, agents.test_mode(config), number)
                    receipt = ai_usage.operation_receipt(db, row)
                await state.update_data(
                    writing_draft={**draft, "phase": "ready", "operation_id": number}
                )
                await next_message(
                    message,
                    "✅ Промпт сохранён\n\n" + receipt,
                    reply_markup=keyboard(
                        [[("📂 Мои промпты", "wp:open")], [("← Агенты", "section:agents")]]
                    ),
                )
                return
            else:
                raise ValueError("Неизвестное действие")
        except (ValueError, ObjectUnavailable) as error:
            await message.answer(
                "⚠️ " + (str(error) or "Нет доступа"),
                reply_markup=keyboard([[("📝 Мой промпт", "wp:open")]]),
            )
        with suppress(TelegramBadRequest):
            await callback.answer()

    return router
