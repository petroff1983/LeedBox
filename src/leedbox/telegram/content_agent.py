"""One upload, one confirmation, automatic processing, three finished publications."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from html import escape
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application import agents, ai_tokens, ai_usage, credits
from leedbox.application import content_agent as app
from leedbox.application.settings import setting
from leedbox.application.versions import fingerprint
from leedbox.domain import content_agent as domain
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIOperation, ContentTask, User
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message, retire
from leedbox.telegram.storage import PostgresStorage

BACK = [("← Агенты", "section:agents")]


def request_screen(row: ContentTask) -> tuple[str, list[list[tuple[str, str]]]]:
    return (
        domain.analysis_feedback(row.steps[0]["result"])
        + "\n\nМогу написать уникальные посты на основе анализа. Сколько и на какие темы?"
        + "\nНапример: «3 поста про маркетинг и 2 про бизнес» или просто «5»."
        + "\nЗа один запуск — 1–10 постов. Стоимость покажу до генерации.",
        [
            [
                (
                    f"{n} пост" if n == 1 else f"{n} поста" if n == 3 else f"{n} постов",
                    f"ca:{row.token}:count:{n}",
                )
                for n in (1, 3, 5)
            ]
        ],
    )


def receipt(db: Session, row: ContentTask, operation: AIOperation | None) -> str:
    if operation is None:
        return ""
    if row.payload.get("workflow") == "competitor":
        return (
            "♾ Безлимитный доступ. Баланс не списывается."
            if operation.billing_mode == "unlimited"
            else "🎁 Бесплатная попытка анализа конкурентов. Баланс не списывается."
            if operation.billing_mode == "trial"
            else (
                f"Поиск: {operation.charged_credits} кредитов. Осталось: {operation.balance_after}."
            )
            if credits.is_credit(operation.snapshot)
            else f"Поиск: {operation.reserve_tokens} токенов. Осталось: {operation.balance_after}."
        )
    return ai_usage.task_receipt(db, row)


async def show_posts(
    bot: Bot,
    row: ContentTask,
    guard: Callable[[], Awaitable[bool]] | None = None,
    controls: InlineKeyboardMarkup | None = None,
    footer: str = "",
) -> bool:
    if row.payload.get("workflow") == "competitor":
        text = (
            domain.competitor_markdown(
                {
                    "source_links": row.payload.get("parts", []),
                    "channel": row.payload["url"],
                    "checked": row.provider.get("checked"),
                    "days": row.payload.get("days"),
                }
            )
            .replace("# Анализ конкурента", "🔎 Анализ конкурента")
            .replace("## ", "")
        )
        if footer:
            text += "\n\n" + footer
        chunks = domain.message_chunks(text)
        for index, chunk in enumerate(chunks):
            if guard is not None and not await guard():
                return False
            await bot.send_message(
                row.chat_id,
                chunk,
                parse_mode=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=controls if index == len(chunks) - 1 else None,
            )
        return True
    result = row.steps[-1]["result"]
    if row.payload.get("phase") == "request":
        await bot.send_message(row.chat_id, domain.analysis_feedback(result), parse_mode=None)
        return True
    for i, post in enumerate(result["posts"], 1):
        if guard is not None and not await guard():
            return False
        for n, part in enumerate(post["parts"], 1):
            if guard is not None and not await guard():
                return False
            label = ""
            if len(post["parts"]) > 1:
                label = " · " + ("Хук" if n == 1 else f"Продолжение {n - 1}")
            markup = None
            if domain.units(part) <= 256:
                markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📋 Скопировать", copy_text=CopyTextButton(text=part)
                            )
                        ]
                    ]
                )
            for chunk in domain.message_chunks(part):
                if guard is not None and not await guard():
                    return False
                await bot.send_message(
                    row.chat_id,
                    f"✍️ Пост {i}. {escape(post['title'])}{label}\n\n<pre>{escape(chunk)}</pre>",
                    parse_mode="HTML",
                    reply_markup=markup,
                )
    return True


async def render(
    message: Message,
    state: FSMContext,
    bot: Bot,
    engine: Engine,
    config: Settings,
    token: str,
    telegram: int,
    *,
    edit: bool = False,
) -> None:
    with Session(engine) as db, db.begin():
        user = app.owner_id(db, telegram)
        row = app.owned(db, config, user, bot.id, token)
        if row.payload.get("kind", "content") != "content":
            raise ObjectUnavailable
        if (
            row.status == "draft"
            and row.payload.get("mode") == "manual"
            and not row.operation_id
            and not row.steps
            and "patterns_version" not in row.payload
        ):
            app.change(row, patterns_version=2)
        data = dict(row.payload)
        if row.status == "draft" and (
            data.get("target") == "threads" or data.get("source_platform") == "threads"
        ):
            app.change(
                row,
                target="telegram",
                source_platform=None,
                url="",
                phase="materials",
                period_selected=False,
            )
            data = dict(row.payload)
        if row.status == "draft" and (
            data.get("phase") not in {"materials", "source", "period", "confirm"}
            or data.get("workflow") not in {"competitor", "patterns"}
            or (data.get("mode") == "link" and data.get("workflow") != "competitor")
        ):
            app.change(
                row,
                phase="materials",
                brief=domain.AUTO_BRIEF,
                target="telegram",
                workflow="competitor" if data["mode"] == "link" else "patterns",
            )
            data = dict(row.payload)
        competitor = data.get("workflow") == "competitor"
        status = row.status
        rows: list[list[tuple[str, str]]] = []
        text = "🔎 Анализ конкурентов\n\n" if competitor else "✍️ Генератор паттернов\n\n"
        waiting_state = None
        if status == "completed" and data.get("phase") == "request":
            body, rows = request_screen(row)
            text += body + "\n\n" + receipt(db, row, db.get(AIOperation, row.operation_id))
            waiting_state = "agent_content_request"
            rows += [[("📒 Мои подборки", "ca:history")], [("➕ Новые исходники", "ca:new:manual")]]
        elif status != "draft":
            text += app.state_text(row)
            if status == "completed":
                operation = db.get(AIOperation, row.operation_id)
                text += "\n\n" + receipt(db, row, operation)
                if row.search_operation_id and not competitor:
                    search = db.get(AIOperation, row.search_operation_id)
                    text += (
                        "\nСбор по ссылке: безлимитный доступ."
                        if search and search.billing_mode == "unlimited"
                        else f"\nСбор по ссылке: {data['offer']['search_price']} токенов отдельно."
                    )
                rows = [
                    [
                        (
                            "🔗 Ссылки и метрики" if competitor else "📄 Показать посты",
                            f"ca:{token}:posts",
                        )
                    ],
                    [("🌐 Открыть и скопировать", f"ait:report:{row.operation_id}")],
                    [("📥 Скачать .md", f"ca:{token}:download")],
                ]
                if data.get("generation_only"):
                    waiting_state = "agent_content_request"
                    text += (
                        "\n\nДля следующей партии напишите количество и темы или откройте анализ."
                    )
                    rows.append(
                        [("✍️ Ещё посты по этому анализу", f"ca:{data['analysis_token']}:open")]
                    )
            elif status not in app.TERMINAL:
                rows = [[("Остановить задачу", f"ca:{token}:cancel")]]
            rows += [[("📒 Мои подборки", "ca:history")], [("➕ Новая подборка", "ca:begin")]]
        else:
            waiting_state = "agent_content_input"
            if data.get("generation_only"):
                waiting_state = None
                text += "Ваш заказ:\n" + "\n".join(
                    f"• {r['count']} · {r['topic']}" for r in data["requests"]
                )
                text += (
                    "\n\nИспользую сохранённый анализ. Повторного анализа и списания за него нет."
                )
                rows.append([("← Изменить заказ", f"ca:{token}:edit_request")])
            elif data["mode"] == "manual":
                parts = data.get("parts", [])
                text += (
                    f"📥 Получено сообщений/файлов: {len(parts)}\n"
                    f"Объём: {domain.byte_size(parts):,} из {domain.MAX_BYTES:,} байт.\n\n"
                    "Перешлите выбранные посты, вставьте тексты несколькими сообщениями "
                    "или отправьте .txt/.md в UTF-8. "
                    "Длинный пост может приходить частями — все они сохраняются. "
                    "Для явного разделения публикаций в одном файле используйте строку --- .\n\n"
                    "Когда всё отправлено, нажмите «Изучить посты». "
                    "Сначала покажу темы и паттерны, затем вы выберете количество новых постов. "
                    "Apify не используется. До запуска списаний нет."
                )
                text += "\n\nТематику и подачу определим по вашим постам."
                text += "\nФормат результата: цельные посты для Telegram."
            elif competitor:
                text += "Пришлите ссылку на публичный Telegram-канал."
                rows = []
                if data.get("url"):
                    text = (
                        "🔎 Анализ конкурентов\n\nЗа какой период найти публикации?\n\n"
                        + data["url"]
                    )
                    waiting_state = "agent_content_period"
                    offer = app.quote(db, row)
                    mode = ai_tokens.funding(db, config, user, bot.id, row.test_mode, offer)
                    balance = ai_tokens.available(db, user, bot.id, row.test_mode)
                    digest = fingerprint({**offer, "billing": mode})[:12]
                    enough = mode in {"unlimited", "trial"} or balance >= credits.amount(offer)
                    if mode == "trial":
                        text += "\n\n🎁 Бесплатная попытка анализа. Баланс не списывается."
                    elif mode != "unlimited":
                        unit = "кредитов" if credits.is_credit(offer) else "токенов"
                        text += f"\n\nПоиск: {credits.amount(offer)} {unit}. Остаток: {balance}."
                    if enough:
                        text += "\n\nВыберите кнопку или напишите число дней (1–365)."
                        rows = [
                            [(f"{d} дней", f"ca:{token}:period:{d}:{digest}") for d in days]
                            for days in ((7, 30), (90, 365))
                        ]
                    else:
                        text += "\nНедостаточно средств на AI-балансе для поиска."
                        rows = [[("🪙 Купить токены", "ait:packages")]]
                    rows.append([("← Другая ссылка", f"ca:{token}:source_reset")])
                    await state.update_data(content_period_digest=digest)
            if not competitor and (data.get("parts") or data.get("url")):
                offer = app.quote(db, row)
                mode = ai_tokens.funding(db, config, user, bot.id, row.test_mode, offer)
                balance = ai_tokens.available(db, user, bot.id, row.test_mode)
                total = credits.amount(offer) + (0 if competitor else offer["search_price"])
                confirmation = fingerprint({**offer, "billing": mode})[:12]
                if mode == "unlimited":
                    text += "\n\n♾ Безлимитный доступ. Пользовательский баланс не списывается."
                elif mode == "trial":
                    text += "\n\n🎁 Бесплатная попытка. Баланс не списывается."
                elif competitor:
                    text += (
                        f"\n\n🪙 Остаток: {balance:,}. Поиск: {total} токенов. ИИ не вызывается."
                    )
                elif credits.is_credit(offer):
                    text += (
                        f"\n\nСтоимость: {credits.amount(offer)} кредитов. Остаток: {balance}. "
                        "Цена фиксирована."
                    )
                else:
                    text += (
                        f"\n\n🪙 Остаток: {balance:,}. "
                        f"Резерв ИИ: до {offer['maximum']:,} токенов.\n"
                        "Спишем фактический расход API, не весь резерв."
                        + (
                            " При ошибке формата включена одна корректировка "
                            "в пределах этого резерва."
                            if offer.get("max_repairs")
                            else ""
                        )
                    )
                if offer["search_price"] and not competitor:
                    text += (
                        f"\nСбор через Apify: {offer['search_price']} токенов "
                        "отдельно при получении постов. "
                        "Даже если последующая генерация не завершится."
                    )
                if mode in {"unlimited", "trial"} or balance >= total:
                    rows.insert(
                        0,
                        [
                            (
                                "🔎 Найти публикации"
                                if competitor
                                else (
                                    "✨ Написать посты"
                                    if data.get("generation_only")
                                    else "🔎 Изучить посты"
                                    if data.get("patterns_version") == 2
                                    else "✨ Создать 3 поста"
                                ),
                                f"ca:{token}:start:{confirmation}",
                            )
                        ],
                    )
                else:
                    text += "\nНе хватает токенов для запуска. Материалы сохранены."
                    rows.insert(0, [("🪙 Купить токены", "ait:packages")])
                rows.append([("Обновить расчёт", f"ca:{token}:open")])
            if not competitor and not data.get("generation_only"):
                rows.append([("Очистить подборку", f"ca:{token}:clear")])
        if status == "draft" and not competitor:
            rows.append([("Отменить подборку", f"ca:{token}:cancel")])
        rows.append(BACK)
        screen = row.screen_id
        markup = keyboard(rows)
    await state.set_state(waiting_state)
    await state.update_data(active_tool="content_agent", content_token=token, paused=False)
    if edit and screen:
        try:
            await bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=screen,
                reply_markup=markup,
                parse_mode=None,
            )
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error):
                return
    sent = await next_message(message, text, reply_markup=markup)
    with Session(engine) as db, db.begin():
        row = app.owned(db, config, user, bot.id, token)
        row.screen_id = sent.message_id


def build_content_router(engine: Engine, config: Settings) -> Router:
    router = Router()
    receipts: dict[int, asyncio.Task[None]] = {}

    async def upload_receipt(message: Message, state: FSMContext, bot: Bot, token: str) -> None:
        await asyncio.sleep(1.2)
        data = await state.get_data()
        if (
            data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "content_agent"
            or data.get("content_token") != token
            or message.from_user is None
        ):
            return
        with Session(engine) as db:
            user = app.owner_id(db, message.from_user.id)
            row = app.owned(db, config, user, bot.id, token)
            if row.status != "draft":
                return
            screen = row.screen_id
        await retire(bot, message.chat.id, screen)
        await render(message, state, bot, engine, config, token, message.from_user.id)

    @router.callback_query(F.data.startswith("ca:"), flags={"requires_access": True})
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
        # An expired query must not prevent running or reopening a durable task.
        with suppress(TelegramBadRequest):
            await callback.answer()
        code = (callback.data or "").split(":")
        try:
            if code[1] in {"begin", "history"}:
                with Session(engine) as db:
                    user = app.owner_id(db, callback.from_user.id)
                    available = setting(db, "ai_content_enabled")
                    tasks = app.latest(db, config, user, bot.id)
                    history = [
                        [
                            (
                                f"{r.created_at:%d.%m %H:%M} · {app.state_text(r)[:35]}",
                                f"ca:{r.token}:open",
                            )
                        ]
                        for r in tasks
                    ]
                if not available:
                    raise ValueError("Контент-аналитик пока выключен владельцем")
                await state.set_state(None)
                await state.update_data(active_tool="content_agent", paused=False)
                buttons = (
                    history
                    if code[1] == "history"
                    else [
                        [("✍️ Генератор паттернов", "ca:new:manual")],
                        [("🔎 Анализ конкурентов", "ca:new:link")],
                        [("📒 Мои подборки", "ca:history")],
                    ]
                    + history[:1]
                )
                await next_message(
                    message,
                    "✍️ Контент-аналитик\n\n"
                    + (
                        "Ваши подборки и задачи. Открытие готового результата без списаний."
                        if code[1] == "history"
                        else "Два отдельных инструмента:\n\n"
                        "✍️ Генератор паттернов — принимает ваши посты, находит приёмы подачи "
                        "и готовит новые посты по цепочке промптов.\n\n"
                        "🔎 Анализ конкурентов — принимает ссылку Telegram-канала, "
                        "показывает ссылки на публикации и их метрики. Без генерации контента."
                    ),
                    reply_markup=keyboard(buttons + [BACK]),
                )
                return
            if code[1] == "new" and len(code) == 3:
                with Session(engine) as db, db.begin():
                    user = app.owner_id(db, callback.from_user.id)
                    task = app.create(db, config, user, bot.id, message.chat.id, code[2])
                    token = task.token
                await render(message, state, bot, engine, config, token, callback.from_user.id)
                return
            if len(code) < 3:
                raise ValueError("Неизвестное действие")
            token, operation = code[1:3]
            with Session(engine) as db, db.begin():
                user = app.owner_id(db, callback.from_user.id)
                # Consistent lock order with reservations and the worker.
                ai_tokens.account(db, user, bot.id, agents.test_mode(config))
                row = app.owned(db, config, user, bot.id, token, locked=True)
                if row.payload.get("kind", "content") != "content":
                    raise ObjectUnavailable
                if operation == "count" and len(code) == 4:
                    row = app.generation_task(db, config, row, code[3], "callback:" + callback.id)
                    token = row.token
                elif operation == "edit_request":
                    if not row.payload.get("generation_only") or row.status != "draft":
                        raise ValueError("Заказ уже запущен")
                    app.cancel(db, row)
                    token = row.payload["analysis_token"]
                elif row.payload.get("generation_only") and operation in {
                    "target",
                    "profile",
                    "source",
                    "clear",
                    "source_reset",
                }:
                    raise ValueError("Для изменения заказа нажмите «Изменить заказ».")
                elif (
                    operation == "target" and len(code) == 4 and code[3] in domain.ACTIVE_PLATFORMS
                ):
                    app.change(row, target=code[3], phase="materials", brief=domain.AUTO_BRIEF)
                elif operation == "profile" and len(code) == 4:
                    app.change(row, phase="materials", brief=domain.AUTO_BRIEF)
                elif operation == "source" and len(code) == 4 and code[3] == "telegram":
                    app.change(row, source_platform=code[3], phase="materials")
                elif operation == "days" and len(code) == 4 and code[3] in {"7", "30", "90"}:
                    raise ValueError(
                        "Сначала пришлите ссылку. Затем выберите период на новом экране."
                    )
                elif operation == "period" and len(code) == 5:
                    if not code[3].isascii() or not code[3].isdecimal() or len(code[3]) > 3:
                        raise ValueError("Период: от 1 до 365 дней")
                    app.period_launch(db, config, row, int(code[3]), code[4])
                elif operation == "source_reset":
                    app.change(row, url=None, phase="source", period_selected=False)
                elif operation == "clear":
                    app.change(row, parts=[], url=None)
                elif operation == "cancel":
                    app.cancel(db, row)
                elif operation == "start" and len(code) == 4:
                    app.launch(db, config, user, bot.id, token, code[3])
                elif operation not in {"open", "posts", "download"}:
                    raise ValueError("Неизвестное действие")
                if operation in {"posts", "download"} and row.status != "completed":
                    raise ValueError("Посты ещё не готовы")
                if operation == "download":
                    document = (
                        domain.competitor_markdown(
                            {
                                "source_links": row.payload["parts"],
                                "channel": row.payload["url"],
                                "checked": row.provider.get("checked"),
                                "days": row.payload.get("days"),
                            }
                        )
                        if row.payload.get("workflow") == "competitor"
                        else domain.markdown(
                            {
                                **row.steps[-1]["result"],
                                "analysis_only": row.payload.get("phase") == "request",
                            }
                        )
                    )
                if operation == "posts":
                    db.expunge(row)
            if operation == "download":
                await message.answer_document(
                    BufferedInputFile(document.encode(), filename="content-posts.md")
                )
                return
            if operation == "posts":
                await show_posts(bot, row)
            await render(
                message,
                state,
                bot,
                engine,
                config,
                token,
                callback.from_user.id,
                edit=operation == "period",
            )
        except (ValueError, ObjectUnavailable) as error:
            await message.answer("⚠️ " + (str(error) or "Подборка недоступна"), parse_mode=None)

    @router.message(
        StateFilter("agent_content_input", "agent_content_period", "agent_content_request"),
        ~F.text.startswith("/"),
        flags={"requires_access": True},
    )
    async def receive(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if (
            not message.from_user
            or message.chat.type != "private"
            or data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "content_agent"
        ):
            return
        try:
            raw = message.text or message.caption or ""
            if message.document:
                doc = message.document
                if (
                    not (doc.file_name or "").lower().endswith((".txt", ".md"))
                    or not doc.file_size
                    or doc.file_size > 80000
                ):
                    raise ValueError(
                        "Нужен .txt/.md в UTF-8, до 80 КБ; объём текста задачи — до 20 КБ."
                    )
                buffer = BytesIO()
                await bot.download(doc.file_id, destination=buffer, timeout=20)
                if buffer.getbuffer().nbytes > 80000:
                    raise ValueError("Файл слишком большой")
                try:
                    raw = buffer.getvalue().decode("utf-8-sig")
                except UnicodeDecodeError:
                    raise ValueError("Сохраните текстовый файл в UTF-8") from None
            token = data["content_token"]
            waiting = await state.get_state()
            with Session(engine) as db, db.begin():
                user = app.owner_id(db, message.from_user.id)
                ai_tokens.account(db, user, bot.id, agents.test_mode(config))
                row = app.owned(db, config, user, bot.id, token, locked=True)
                if waiting == "agent_content_request":
                    row = app.generation_task(db, config, row, raw, f"message:{message.message_id}")
                    token = row.token
                elif (
                    waiting == "agent_content_period"
                    and raw.strip().isascii()
                    and raw.strip().isdecimal()
                ):
                    if len(raw.strip()) > 3:
                        raise ValueError("Период: от 1 до 365 дней")
                    app.period_launch(
                        db, config, row, int(raw.strip()), data.get("content_period_digest", "")
                    )
                elif row.payload["mode"] == "manual":
                    app.add_part(
                        row,
                        message.message_id,
                        raw,
                        source_platform="telegram"
                        if getattr(message.forward_origin, "type", None) == "channel"
                        else None,
                    )
                else:
                    platform, url = domain.detect_source(raw)
                    app.change(
                        row,
                        url=url,
                        source_platform=platform,
                        phase="period",
                        period_selected=False,
                    )
                manual_upload = (
                    row.payload.get("mode") == "manual" and row.payload.get("phase") == "materials"
                )
            if manual_upload:
                # One acknowledgement below the forwarded batch, not a reply per post.
                previous = receipts.get(message.chat.id)
                if previous:
                    previous.cancel()
                task = asyncio.create_task(upload_receipt(message, state, bot, token))
                receipts[message.chat.id] = task

                def done(finished: asyncio.Task[None]) -> None:
                    if receipts.get(message.chat.id) is finished:
                        receipts.pop(message.chat.id, None)
                    if not finished.cancelled() and finished.exception() is not None:
                        logging.getLogger(__name__).error("content_receipt_error")

                task.add_done_callback(done)
            else:
                await render(message, state, bot, engine, config, token, message.from_user.id)
        except (ValueError, ObjectUnavailable) as error:
            await message.answer("⚠️ " + (str(error) or "Подборка недоступна"), parse_mode=None)

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
    if row.payload.get("kind") == "lead_magnet":
        from leedbox.telegram.lead_magnet_agent import notify as notify_lm

        await notify_lm(bot, engine, config, token)
        return
    storage = PostgresStorage(engine)

    async def active() -> bool:
        current = await storage.get_data(
            StorageKey(bot_id=bot.id, chat_id=row.chat_id, user_id=telegram)
        )
        return (
            current.get("mode") == "expert"
            and not current.get("paused")
            and current.get("active_tool") == "content_agent"
            and current.get("content_token") == token
        )

    state = await storage.get_data(StorageKey(bot_id=bot.id, chat_id=row.chat_id, user_id=telegram))
    if (
        state.get("mode") != "expert"
        or state.get("paused")
        or state.get("active_tool") != "content_agent"
        or state.get("content_token") != token
    ):
        return  # Never inject expert results into a gift-recipient or another tool's dialog.
    rows = [[("📄 Открыть подборку", f"ca:{token}:open")], BACK]
    with suppress(TelegramAPIError):
        await bot.edit_message_text(
            (
                "🔎 Анализ конкурентов\n\n"
                if row.payload.get("workflow") == "competitor"
                else "✍️ Генератор паттернов\n\n"
            )
            + app.state_text(row),
            chat_id=row.chat_id,
            message_id=row.screen_id,
            reply_markup=keyboard(rows),
        )
    if row.status == "completed" and not row.delivered:
        with Session(engine) as db, db.begin():
            current = db.get(ContentTask, token, with_for_update=True)
            if current is None or current.delivered:
                return
            current.delivered = (
                True  # Ambiguous sends are reopened through history, never blindly replayed.
            )
        with Session(engine) as db:
            op = db.get(AIOperation, row.operation_id)
            cost_text = receipt(db, row, op)
        if row.payload.get("workflow") == "competitor":
            await show_posts(
                bot,
                row,
                active,
                controls=keyboard(
                    [
                        [("🌐 Открыть и скопировать", f"ait:report:{row.operation_id}")],
                        [("📥 Скачать .md", f"ca:{token}:download")],
                        [("📒 Мои подборки", "ca:history")],
                        BACK,
                    ]
                ),
                footer=cost_text if op and op.billing_mode != "unlimited" else "",
            )
            return
        if row.payload.get("phase") == "request":
            if not await active():
                return
            text, buttons = request_screen(row)
            await storage.set_state(
                StorageKey(bot_id=bot.id, chat_id=row.chat_id, user_id=telegram),
                "agent_content_request",
            )
            await bot.send_message(
                row.chat_id,
                text + "\n\n" + cost_text,
                reply_markup=keyboard(buttons + [BACK]),
                parse_mode=None,
            )
            return
        if not await show_posts(bot, row, active) or not await active():
            return
        if row.payload.get("generation_only"):
            await storage.set_state(
                StorageKey(bot_id=bot.id, chat_id=row.chat_id, user_id=telegram),
                "agent_content_request",
            )
        await bot.send_message(
            row.chat_id,
            (
                "✅ Ссылки и метрики сохранены.\n"
                if row.payload.get("workflow") == "competitor"
                else f"✅ Посты готовы и сохранены: {len(row.steps[-1]['result']['posts'])}.\n"
            )
            + cost_text
            + (
                "\n\nНужны ещё посты? Напишите количество и темы."
                if row.payload.get("generation_only")
                else ""
            ),
            reply_markup=keyboard(
                [
                    [("🌐 Открыть и скопировать", f"ait:report:{row.operation_id}")],
                    [("📥 Скачать .md", f"ca:{token}:download")],
                    [("📒 Мои подборки", "ca:history")],
                    *(
                        [
                            [
                                (
                                    "✍️ Ещё посты по этому анализу",
                                    f"ca:{row.payload['analysis_token']}:open",
                                )
                            ]
                        ]
                        if row.payload.get("generation_only")
                        else []
                    ),
                    BACK,
                ]
            ),
        )


async def background(bot: Bot, engine: Engine, config: Settings) -> None:
    async with asyncio.TaskGroup() as group:
        for _ in range(config.ai_workers):
            group.create_task(_worker(bot, engine, config))


async def _worker(bot: Bot, engine: Engine, config: Settings) -> None:
    while True:
        token = None
        try:
            token = await asyncio.to_thread(app.claim, engine, config, bot.id)
            if token:
                # A Telegram edit failure must not strand an already claimed job.
                with suppress(TelegramAPIError):
                    await notify(bot, engine, config, token)
                await asyncio.to_thread(app.advance, engine, config, token)
                with suppress(TelegramAPIError):
                    await notify(bot, engine, config, token)
        except Exception:
            logging.getLogger(__name__).error(
                "content_worker_error"
            )  # No raw inputs, keys or provider body.
        if token is None:
            await asyncio.sleep(0.5)
