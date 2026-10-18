"""Lead-magnet interview, five drafts, explicit approval or automatic critic pass."""

from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application import agents, ai_tokens, ai_usage
from leedbox.application import content_agent as tasks
from leedbox.application import lead_magnet_agent as app
from leedbox.domain import lead_magnet_agent as domain
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.content_agent import message_chunks
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import ContentTask, User
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.storage import PostgresStorage

BACK = [("← Агенты", "section:agents")]


def screen(
    db: Session, config: Settings, row: ContentTask
) -> tuple[str, list[list[tuple[str, str]]], str | None]:
    phase = row.payload.get("phase", "niche")
    rows: list[list[tuple[str, str]]] = []
    text = "🎁 Агент лид-магнитов\n\n"
    state = None
    if row.status == "draft" and phase in domain.QUESTIONS:
        state = "agent_lm_input"
        text += domain.QUESTIONS[phase]
        if phase == "niche":
            options = app.audience_options(db, row)
            if options:
                text += "\n\nИли выберите свои сохранённые вводные:"
                rows += [[(o["label"], f"lm:{row.token}:use:{o['code']}")] for o in options]
        if phase == "product":
            rows.append([("Пропустить", f"lm:{row.token}:skip")])
        phases = list(domain.QUESTIONS)
        if phases.index(phase):
            rows.append([("← Назад", f"lm:{row.token}:back")])
    elif row.status == "draft" and phase in {"confirm", "lm_review"}:
        revision = phase == "lm_review"
        state = "agent_lm_feedback" if revision else None
        _, digest, cost, enough = app.confirmation(db, config, row, revision)
        text += (
            "Устраивают пять вариантов или отправить их на критику?\n"
            "Одобрение ничего не стоит. "
            "Критика и доработка — отдельная операция ИИ.\n\n"
            if revision
            else "Подберу 5 тем: название, короткое описание пользы и формат. "
            "Сам материал вы подготовите самостоятельно.\n\n"
        ) + cost
        if revision:
            text += (
                "\n\nСообщение в этом окне — пожелания к текущим темам. "
                "Для другого продукта или аудитории нажмите «Новая подборка»."
            )
            if row.payload.get("feedback"):
                text += "\n\nВаши пожелания:\n" + row.payload["feedback"]
        if revision:
            rows.append([("✅ Одобрить", f"lm:{row.token}:approve")])
        if enough:
            rows.append(
                [
                    (
                        (
                            "✨ Доработать по пожеланиям"
                            if row.payload.get("feedback")
                            else "🔎 Критиковать"
                        )
                        if revision
                        else "✨ Подобрать 5 тем",
                        f"lm:{row.token}:{'critic' if revision else 'start'}:{digest}",
                    )
                ]
            )
        else:
            text += "\nДля генерации не хватает токенов. Ответы сохранены."
            rows.append([("🪙 Купить токены", "ait:packages")])
        if revision:
            rows.append([("📄 Открыть варианты", f"lm:{row.token}:show")])
        else:
            rows.append([("← Изменить ответы", f"lm:{row.token}:back")])
    else:
        text += app.label(row)
        if row.status == "completed":
            state = "agent_lm_feedback"
            text = (
                "🎁 Пять тем для лид-магнитов сохранены."
                if saved(row).get("output_kind") == "ideas"
                else "🎁 Пять лид-магнитов готовы и сохранены."
            )
            if row.payload.get("approved"):
                text += "\nВы одобрили эти варианты."
            text += (
                "\nНапишите пожелания для доработки этих тем. "
                "Для другой задачи нажмите «Новая подборка»."
            )
            rows += [
                [("📄 Открыть варианты", f"lm:{row.token}:show")],
            ]
        elif row.status not in tasks.TERMINAL:
            rows.append([("Остановить", f"lm:{row.token}:cancel")])
        if row.steps and row.status in {"failed", "uncertain"}:
            text += "\nПервоначальные материалы сохранены."
            rows.append([("📄 Первоначальные варианты", f"lm:{row.token}:show")])
    has_result = bool(saved(row))
    if has_result:
        text += "\n\n" + ai_usage.task_receipt(db, row)
    if (has_result and row.status == "draft") or row.status in tasks.TERMINAL:
        rows.append([("➕ Новая подборка", f"lm:{row.token}:new")])
    if row.status == "draft" and not has_result:
        rows.append([("Отменить подборку", f"lm:{row.token}:cancel")])
    rows += [[("📒 Мои подборки", "lm:history")], BACK]
    return text, rows, state


def saved(row: ContentTask) -> dict[str, Any]:
    return next(
        (
            step["result"]
            for step in reversed(row.steps)
            if step.get("result", {}).get("lead_magnets")
        ),
        {},
    )


async def show(
    bot: Bot, row: ContentTask, active: Callable[[], Awaitable[bool]] | None = None
) -> bool:
    for item in saved(row).get("lead_magnets", []):
        if active is not None and not await active():
            return False
        if "description" in item:
            text = (
                f"🎁 {item['id']}. {item['title']}\n\n{item['description']}"
                f"\n\n📄 Формат: {item['format']}"
            )
        else:
            text = f"🎁 {item['id']}. {item['title']}\n{item['format']}\n\n{item['benefit']}\n\n"
            text += "\n".join("• " + p for p in item["content"])
            text += "\n\n📣 Анонс получения:\n" + item["announcement"] + "\n\n" + item["bridge"]
        for chunk in message_chunks(text):
            if active is not None and not await active():
                return False
            await bot.send_message(row.chat_id, chunk, parse_mode=None)
    return True


async def render(
    message: Message,
    state: FSMContext,
    bot: Bot,
    engine: Engine,
    config: Settings,
    token: str,
    telegram: int,
) -> None:
    with Session(engine) as db:
        row = tasks.owned(db, config, tasks.owner_id(db, telegram), bot.id, token)
        if row.payload.get("kind") != "lead_magnet":
            raise ObjectUnavailable
        text, rows, waiting = screen(db, config, row)
    await state.set_state(waiting)
    await state.update_data(active_tool="lead_magnet_agent", lead_magnet_token=token, paused=False)
    sent = await next_message(message, text, reply_markup=keyboard(rows))
    with Session(engine) as db, db.begin():
        row = tasks.owned(db, config, tasks.owner_id(db, telegram), bot.id, token)
        row.screen_id = sent.message_id


def build_lead_magnet_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("lm:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        message = callback.message
        if (
            not isinstance(message, Message)
            or message.chat.type != "private"
            or (await state.get_data()).get("mode") != "expert"
        ):
            with suppress(TelegramBadRequest):
                await callback.answer("Откройте /start для инструментов", show_alert=True)
            return
        with suppress(TelegramBadRequest):
            await callback.answer()
        parts = (callback.data or "").split(":")
        try:
            with Session(engine) as db, db.begin():
                owner = tasks.owner_id(db, callback.from_user.id)
                ai_tokens.account(db, owner, bot.id, agents.test_mode(config))
                if parts[1] == "history":
                    rows = [
                        [(f"{r.created_at:%d.%m} · {app.label(r)[:40]}", f"lm:{r.token}:open")]
                        for r in tasks.latest(db, config, owner, bot.id, "lead_magnet")
                    ]
                    history = True
                else:
                    history = False
                    if parts[1] in {"begin", "new"}:
                        row = app.create(
                            db, config, owner, bot.id, message.chat.id, fresh=parts[1] == "new"
                        )
                    else:
                        row = tasks.owned(db, config, owner, bot.id, parts[1], locked=True)
                        if row.payload.get("kind") != "lead_magnet":
                            raise ObjectUnavailable
                        operation = parts[2]
                        phase = row.payload.get("phase")
                        if operation == "new":
                            row = app.new_selection(db, config, row)
                        elif operation in {"start", "critic"} and len(parts) == 4:
                            app.launch(db, config, row, parts[3], operation == "critic")
                        elif operation == "approve":
                            if row.status == "draft" and phase == "lm_review":
                                row.status = "completed"
                                row.payload = {**row.payload, "approved": True}
                        elif operation == "use" and len(parts) == 4:
                            if row.status != "draft" or phase != "niche":
                                raise ValueError("Сначала откройте новый опрос")
                            choice = next(
                                (o for o in app.audience_options(db, row) if o["code"] == parts[3]),
                                None,
                            )
                            if choice is None:
                                raise ObjectUnavailable
                            tasks.change(
                                row,
                                brief={"niche": choice["niche"], "audience": choice["audience"]},
                                phase="pain",
                            )
                        elif operation == "skip" and phase == "product":
                            tasks.change(
                                row,
                                phase="confirm",
                                brief={**row.payload["brief"], "product": "Не указан"},
                            )
                        elif operation == "back" and row.status == "draft":
                            phases = list(domain.QUESTIONS) + ["confirm"]
                            if phase not in phases or phases.index(phase) == 0:
                                raise ValueError("Начните новую подборку")
                            tasks.change(row, phase=phases[phases.index(phase) - 1])
                        elif operation == "cancel":
                            tasks.cancel(db, row)
                        elif operation not in {"open", "show", "download"}:
                            raise ValueError("Неизвестное действие")
                    token = row.token
                    if len(parts) >= 3 and parts[2] in {"show", "download"}:
                        if not saved(row):
                            raise ValueError("Материалы ещё не готовы")
                        db.expunge(row)
            if history:
                await next_message(
                    message,
                    "📒 Ваши подборки лид-магнитов. Открытие бесплатно.",
                    reply_markup=keyboard(rows + [[("➕ Новая подборка", "lm:new")], BACK]),
                )
                return
            if len(parts) >= 3 and parts[2] == "download":
                await message.answer_document(
                    BufferedInputFile(
                        domain.markdown(saved(row)).encode(), filename="lead-magnets.md"
                    )
                )
                return
            if len(parts) >= 3 and parts[2] == "show":
                await show(bot, row)
                await next_message(
                    message,
                    "Сохранённые варианты: копирование и скачивание без вызова ИИ.",
                    reply_markup=keyboard(
                        [
                            [("🌐 Открыть и скопировать", f"ait:report:{row.operation_id}")],
                            [("📥 Скачать .md", f"lm:{row.token}:download")],
                        ]
                    ),
                )
            await render(message, state, bot, engine, config, token, callback.from_user.id)
        except (ValueError, ObjectUnavailable, IndexError) as error:
            await message.answer("⚠️ " + (str(error) or "Подборка недоступна"), parse_mode=None)

    @router.message(
        StateFilter("agent_lm_input", "agent_lm_feedback"),
        ~F.text.startswith("/"),
        flags={"requires_access": True},
    )
    async def answer(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if (
            not message.from_user
            or data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "lead_magnet_agent"
        ):
            return
        try:
            raw = (message.text or "").strip()
            if not 3 <= len(raw) <= 1000:
                raise ValueError("Ответ: 3–1000 символов, без персональных данных")
            token = data["lead_magnet_token"]
            with Session(engine) as db, db.begin():
                row = tasks.owned(
                    db, config, tasks.owner_id(db, message.from_user.id), bot.id, token, locked=True
                )
                phase = row.payload.get("phase")
                if row.status == "completed" or (row.status == "draft" and phase == "lm_review"):
                    app.feedback(row, raw, message.message_id)
                elif phase not in domain.QUESTIONS:
                    raise ValueError("Опрос уже завершён")
                else:
                    phases = list(domain.QUESTIONS) + ["confirm"]
                    tasks.change(
                        row,
                        brief={**row.payload["brief"], phase: raw},
                        phase=phases[phases.index(phase) + 1],
                    )
            await render(message, state, bot, engine, config, token, message.from_user.id)
        except (ValueError, ObjectUnavailable) as error:
            await message.answer("⚠️ " + (str(error) or "Недоступно"), parse_mode=None)

    return router


async def notify(bot: Bot, engine: Engine, config: Settings, token: str) -> None:
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        if row is None or row.screen_id is None:
            return
        user = db.get(User, row.owner_user_id)
        if user is None:
            return
        telegram = user.telegram_id
        text, rows, waiting = screen(db, config, row)
    storage = PostgresStorage(engine)
    key = StorageKey(bot_id=bot.id, chat_id=row.chat_id, user_id=telegram)

    async def active() -> bool:
        state = await storage.get_data(key)
        return (
            state.get("mode") == "expert"
            and not state.get("paused")
            and state.get("active_tool") == "lead_magnet_agent"
            and state.get("lead_magnet_token") == token
        )

    if not await active():
        return
    await storage.set_state(key, waiting)
    with suppress(TelegramAPIError):
        await bot.edit_message_text(
            text, chat_id=row.chat_id, message_id=row.screen_id, reply_markup=keyboard(rows)
        )
    if (row.status == "completed" or row.payload.get("phase") == "lm_review") and not row.delivered:
        with Session(engine) as db, db.begin():
            current = db.get(ContentTask, token, with_for_update=True)
            if current is None or current.delivered:
                return
            current.delivered = True
        if await show(bot, row, active) and await active():
            await bot.send_message(row.chat_id, text, reply_markup=keyboard(rows), parse_mode=None)
