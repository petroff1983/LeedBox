"""Article text to a saved set of image prompts; explicit quota confirmation."""

import asyncio
from contextlib import suppress
from io import BytesIO
from typing import Any
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application import agents, ai_tokens
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.image_prompts import markdown
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import User
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message


async def show_images(
    message: Message,
    number: int,
    result: dict[str, Any],
    receipt: str,
    from_history: bool = False,
) -> None:
    for item in result["images"]:
        await message.answer(
            f"📌 Под заголовком «{item['heading']}»\n\n🖼 Разместите картинку:\n{item['prompt']}"
        )
    await message.answer(
        receipt,
        reply_markup=keyboard(
            [
                [("🌐 Открыть и скопировать", f"ait:report:{number}")],
                [("📥 Скачать все промпты .md", f"ip:download:{number}")],
                [("🖼 Другая статья", "ip:begin")],
                [("← К списку результатов", "ait:operations")]
                if from_history
                else [("📒 История", "ait:operations"), ("← Агенты", "section:agents")],
            ]
        ),
    )


async def confirm_images(
    message: Message,
    offer: dict[str, Any],
    token: str,
    balance: int,
    payment: tuple[bool, str] | None = None,
) -> None:
    enough, description = payment or (
        balance >= offer["maximum"],
        f"Остаток: {balance:,} токенов. На создание нужно до {offer['maximum']:,}.",
    )
    buttons = (
        [[("✨ Создать промпты", f"ip:generate:{token}")]]
        if enough
        else [[("🪙 Купить токены", "ait:packages")]]
    )
    await next_message(
        message,
        f"🖼 Промпты для картинок\n\nРазделов: {len(offer['inputs']['headings'])}\n"
        + "\n".join(f"{i}. {title}" for i, title in enumerate(offer["inputs"]["headings"], 1))
        + "\n\n"
        + description
        + "\n"
        "Создаются промпты на английском, а не сами изображения."
        + ("\nНе хватает токенов — пополните баланс и вернитесь к задаче." if not enough else ""),
        reply_markup=keyboard(buttons + [[("← Дополнить статью", f"ip:edit:{token}")]]),
    )


async def collecting_article(message: Message, draft: dict[str, Any]) -> None:
    parts = draft.get("parts", [])
    size = len("\n\n".join(part["text"] for part in parts))
    await next_message(
        message,
        f"📥 Статья: получено частей — {len(parts)}, символов — {size} из 20 000.\n\n"
        "Отправьте остальные части. Когда вся статья отправлена, нажмите кнопку ниже.\n"
        "ИИ ещё не запущен, токены не списываются.",
        reply_markup=keyboard(
            [
                [("✅ Статья отправлена", f"ip:finish:{draft['token']}")],
                [("🧹 Очистить текст", "ip:begin")],
                [("← Агенты", "section:agents")],
            ]
        ),
    )


def build_image_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    def owner(db: Session, telegram: int) -> int:
        user = db.scalar(select(User.id).where(User.telegram_id == telegram))
        if user is None:
            raise ObjectUnavailable
        return user

    async def prepare(
        message: Message, state: FSMContext, bot: Bot, telegram: int, raw: str
    ) -> None:
        with Session(engine) as db:
            user = owner(db, telegram)
            agents.allowed(db, user, bot.id, agents.test_mode(config), "image_prompts", config)
            offer = agents.quote_images(db, raw)
            balance = ai_tokens.available(db, user, bot.id, agents.test_mode(config))
            payment = agents.payment_preview(db, config, user, bot.id, offer)
        token = uuid4().hex[:12]
        old = (await state.get_data()).get("image_draft", {})
        parts = old.get("parts") or [{"id": 0, "text": raw}]
        await state.update_data(
            image_draft={"token": token, "phase": "confirm", "offer": offer, "parts": parts}
        )
        await state.set_state(None)
        await confirm_images(message, offer, token, balance, payment)

    @router.message(
        StateFilter("agent_image_article"), ~F.text.startswith("/"), flags={"requires_access": True}
    )
    async def article(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if (
            message.chat.type != "private"
            or data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "agents"
        ):
            return
        try:
            raw = message.text or ""
            draft = data.get("image_draft", {})
            parts = draft.get("parts", [])
            if message.document:
                if parts:
                    raise ValueError(
                        "Уже получены части текста. Завершите их "
                        "или очистите текст перед отправкой файла."
                    )
                doc = message.document
                if (
                    not (doc.file_name or "").lower().endswith((".txt", ".md"))
                    or not doc.file_size
                    or doc.file_size > 80000
                ):
                    raise ValueError("Нужен .txt или .md в UTF-8, до 80 КБ и 20 000 символов.")
                buffer = BytesIO()
                await bot.download(doc.file_id, destination=buffer, timeout=20)
                if buffer.getbuffer().nbytes > 80000:
                    raise ValueError("Файл слишком большой")
                try:
                    raw = buffer.getvalue().decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise ValueError("Сохраните файл в UTF-8 и отправьте ещё раз") from None
            if message.from_user is None:
                return
            if message.document:
                await prepare(message, state, bot, message.from_user.id, raw)
            else:
                if not raw.strip():
                    raise ValueError("Пришлите текст статьи или файл .txt / .md.")
                if any(part["id"] == message.message_id for part in parts):
                    return
                updated = sorted(
                    parts + [{"id": message.message_id, "text": raw}], key=lambda part: part["id"]
                )
                if len("\n\n".join(part["text"] for part in updated)) > 20000:
                    raise ValueError(
                        "Общий текст превышает 20 000 символов. Эта часть не добавлена; "
                        "предыдущие сохранены. Сократите статью или очистите текст."
                    )
                if len(updated) > 32:
                    raise ValueError(
                        "Максимум 32 части. Эта часть не добавлена. "
                        "Пришлите статью файлом после очистки текста."
                    )
                draft = {
                    "phase": "article",
                    "token": draft.get("token") or uuid4().hex[:12],
                    "parts": updated,
                }
                await state.update_data(image_draft=draft)
                await collecting_article(message, draft)
        except (ValueError, ObjectUnavailable) as error:
            await message.answer("⚠️ " + (str(error) or "Нет доступа"))

    @router.callback_query(F.data.startswith("ip:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data, message = await state.get_data(), callback.message
        if (
            not isinstance(message, Message)
            or message.chat.type != "private"
            or data.get("mode") != "expert"
        ):
            await callback.answer("Откройте /start", show_alert=True)
            return
        code = callback.data or ""
        draft = data.get("image_draft", {})
        try:
            with Session(engine) as db:
                user = owner(db, callback.from_user.id)
            if code == "ip:begin":
                with Session(engine) as db:
                    agents.allowed(
                        db, user, bot.id, agents.test_mode(config), "image_prompts", config
                    )
                if not config.deepseek_api_key.get_secret_value():
                    raise ValueError("ИИ пока не настроен владельцем")
                await state.set_state("agent_image_article")
                await state.update_data(
                    active_tool="agents",
                    paused=False,
                    image_draft={"phase": "article", "token": uuid4().hex[:12], "parts": []},
                )
                await next_message(
                    message,
                    "🖼 Промпты для картинок\n\n"
                    "Пришлите готовую статью с заголовками одним или несколькими сообщениями. "
                    "После последней части нажмите «Статья отправлена».\n"
                    "Можно отправить целиком файлом .txt / .md (UTF-8). "
                    "Общий лимит — 20 000 символов.\n\n"
                    "Создам до пяти промптов — к первым заголовкам по порядку. "
                    "Фотореализм, 4:3, без надписей. "
                    "Если заголовков меньше пяти, промптов будет столько же.\n\n"
                    "На этом шаге ИИ не запускается. Перед генерацией покажу резерв токенов.",
                    reply_markup=keyboard([[("← Агенты", "section:agents")]]),
                )
            elif code == "ip:resume":
                if draft.get("phase") not in {"article", "confirm"}:
                    raise ValueError("Нет незавершённой подготовки")
                await state.update_data(active_tool="agents", paused=False)
                if draft["phase"] == "article":
                    await state.set_state("agent_image_article")
                    await collecting_article(message, draft)
                else:
                    await state.set_state(None)
                    with Session(engine) as db:
                        balance = ai_tokens.available(db, user, bot.id, agents.test_mode(config))
                        payment = agents.payment_preview(db, config, user, bot.id, draft["offer"])
                    await state.update_data(image_draft=draft)
                    await confirm_images(message, draft["offer"], draft["token"], balance, payment)
            elif code.startswith("ip:edit:"):
                if (
                    draft.get("phase") != "confirm"
                    or code != f"ip:edit:{draft.get('token')}"
                    or data.get("paused")
                    or data.get("active_tool") != "agents"
                ):
                    raise ValueError("Экран устарел. Вернитесь к статье через «Агенты».")
                # Support drafts created before multipart uploads were introduced.
                parts = draft.get("parts") or [
                    {"id": 0, "text": draft["offer"]["inputs"]["article"]}
                ]
                draft = {"phase": "article", "token": uuid4().hex[:12], "parts": parts}
                await state.update_data(image_draft=draft)
                await state.set_state("agent_image_article")
                await collecting_article(message, draft)
            elif code.startswith("ip:finish:"):
                if (
                    draft.get("phase") != "article"
                    or code != f"ip:finish:{draft.get('token')}"
                    or data.get("paused")
                    or data.get("active_tool") != "agents"
                ):
                    raise ValueError("Экран устарел. Вернитесь к статье через «Агенты».")
                raw = "\n\n".join(part["text"] for part in draft.get("parts", []))
                if not raw.strip():
                    raise ValueError("Сначала отправьте текст статьи.")
                await prepare(message, state, bot, callback.from_user.id, raw)
            elif code.startswith("ip:download:"):
                with Session(engine) as db:
                    row = agents.operation(
                        db, user, bot.id, agents.test_mode(config), int(code.split(":")[2])
                    )
                    if (
                        row.snapshot.get("purpose") != "image_prompts"
                        or row.status != "completed"
                        or not row.result
                    ):
                        raise ObjectUnavailable
                    text = markdown(row.result)
                await message.answer_document(
                    BufferedInputFile(text.encode(), filename="image-prompts.md"),
                    caption="Сохранённый результат. Новых списаний нет.",
                )
            elif code.startswith("ip:generate:"):
                if (
                    code != f"ip:generate:{draft.get('token')}"
                    or draft.get("phase") != "confirm"
                    or data.get("paused")
                    or data.get("active_tool") != "agents"
                ):
                    raise ValueError("Экран устарел. Откройте «Агенты» и вернитесь к задаче.")
                await state.update_data(image_draft={**draft, "phase": "working"})
                with suppress(TelegramBadRequest):
                    await callback.answer()
                await message.answer(
                    "⏳ Создаю промпты для картинок. Дождитесь результата — "
                    "повторно нажимать кнопку не нужно."
                )
                try:
                    number = await asyncio.to_thread(
                        agents.generate,
                        engine,
                        config,
                        user,
                        bot.id,
                        "images:" + draft["token"],
                        draft["offer"],
                        require_confirmation=True,
                    )
                except ValueError:
                    with Session(engine) as db:
                        failed = ai_tokens.previous(
                            db, user, bot.id, agents.test_mode(config), "images:" + draft["token"]
                        )
                        if failed is not None and failed.status == "failed":
                            # Known refunded failures allow a new EXPLICIT confirmation.
                            # Unknown provider usage must never be retried automatically.
                            draft = {**draft, "token": uuid4().hex[:12]}
                    await state.update_data(image_draft={**draft, "phase": "confirm"})
                    raise
                with Session(engine) as db:
                    row = agents.operation(db, user, bot.id, agents.test_mode(config), number)
                    result = row.result or {}
                    receipt = ai_tokens.receipt(row)
                await state.set_state(None)
                await state.update_data(image_draft={"phase": "ready", "operation_id": number})
                await show_images(message, number, result, receipt)
                return
            else:
                raise ValueError("Неизвестное действие")
        except (ValueError, ObjectUnavailable) as error:
            # Callback notifications expire during provider work and are length-limited.
            # Deliver the full error as a persistent message, even after a slow response.
            await message.answer(
                "⚠️ " + (str(error) or "Нет доступа"),
                reply_markup=keyboard(
                    [[("↩ Вернуться к задаче", "ip:resume")], [("← Агенты", "section:agents")]]
                ),
            )
            with suppress(TelegramBadRequest):
                await callback.answer()
            return
        with suppress(TelegramBadRequest):
            await callback.answer()

    return router
