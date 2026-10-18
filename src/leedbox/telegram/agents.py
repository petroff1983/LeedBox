"""Agent catalogue, explicit paid confirmation, persistent draft and token receipt."""

import asyncio
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    Message,
)
from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session

from leedbox.application import agents, ai_tokens, ai_usage, credits, dzen_history, wallet
from leedbox.application.cabinet_access import issue_ticket
from leedbox.application.sections import visible_sections
from leedbox.application.settings import setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.dzen import channel_slug
from leedbox.infrastructure.agents import AgentError
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIOperation, User
from leedbox.telegram.content_agent import build_content_router
from leedbox.telegram.image_prompts import build_image_router, show_images
from leedbox.telegram.lead_magnet_agent import build_lead_magnet_router
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.writing_profile import build_writing_router


def agent_rows(visible: set[str]) -> list[list[tuple[str, str]]]:
    rows = [
        [("📰 Агент Дзен", "az:begin")],
        [("🖼 Промпты для картинок", "ip:begin")],
        [("✍️ Контент-аналитик", "ca:begin")],
        [("🎁 Агент лид-магнитов", "lm:begin")],
    ]
    if "agents" not in visible:
        return []
    if "youtube" in visible:
        rows.append([("📊 Анализ YouTube", "section:youtube")])
    return rows


def build_agent_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    def owner(db: Session, telegram: int) -> int:
        result = db.scalar(select(User.id).where(User.telegram_id == telegram))
        if result is None:
            raise ObjectUnavailable
        return result

    def funds(telegram: int, bot: int) -> int:
        with Session(engine) as db:
            return ai_tokens.available(db, owner(db, telegram), bot, agents.test_mode(config))

    def header(value: int) -> str:
        with Session(engine) as db:
            unit = "кредитов" if credits.enabled(db) else "токенов"
        return f"🪙 Остаток: {value:,} {unit}".replace(",", " ")

    async def show_posts(message: Message, state: FSMContext, draft: dict[str, Any]) -> None:
        await state.set_state(None)
        await state.update_data(
            agent_draft={**draft, "phase": "posts"}, active_tool="agents", paused=False
        )
        await next_message(
            message,
            "📚 Сохранённая подборка\n\n"
            + str(draft["report"].get("slug", "Канал Дзен"))
            + f"\nПубликации: {draft['report'].get('start', '—')} — "
            + f"{draft['report'].get('end', '—')}.\n\n"
            + "Выберите статью. Открытие подборки без запроса Zenstat и списаний.",
            reply_markup=keyboard(
                [
                    [(f"{i + 1}. {p['title'][:55]}", f"az:{draft['token']}:select:{i}")]
                    for i, p in enumerate(draft["report"]["posts"])
                ]
                + [[("📒 История", "ait:history")], [("← Агенты", "section:agents")]]
            ),
        )

    async def show_result(
        message: Message,
        state: FSMContext,
        draft: dict[str, Any],
        telegram: int,
        bot: int,
        number: int,
        cached: bool = False,
        from_history: bool = False,
    ) -> None:
        with Session(engine) as db:
            row = agents.operation(db, owner(db, telegram), bot, agents.test_mode(config), number)
            result = row.result or {}
            receipt = ai_usage.operation_receipt(db, row)
            if cached:
                receipt += "\nСохранённый результат. Новых списаний нет."
        await state.set_state(None)
        await state.update_data(
            agent_draft={**draft, "phase": "ready", "operation_id": number},
            active_tool="agents",
            paused=False,
        )
        text = "🎯 Ваши заголовки\n\n" + "\n\n".join(
            f"{v['id']}. {v['title']}" for v in result.get("variants", [])
        )
        text += "\n\n" + receipt
        markup = keyboard(
            [
                [("📝 Промпт для статьи", "wp:open")],
                [("← К подборке статей", f"az:{draft['token']}:posts:all")],
                [("📒 История", "ait:history"), ("🌐 Отчёты", "ait:web")],
                [("← К списку результатов", "ait:operations")]
                if from_history
                else [("← Агенты", "section:agents")],
            ]
        )
        if not draft.get("report", {}).get("posts"):
            markup.inline_keyboard.pop(1)
        copy_rows = [
            [
                InlineKeyboardButton(
                    text=f"📋 Заголовок {v['id']}", copy_text=CopyTextButton(text=v["title"])
                )
            ]
            for v in result.get("variants", [])
        ]
        markup.inline_keyboard[0:0] = copy_rows
        await message.answer(text[:3900], reply_markup=markup)

    async def select_article(
        message: Message,
        state: FSMContext,
        draft: dict[str, Any],
        telegram: int,
        bot: int,
        index: int,
    ) -> None:
        posts = draft["report"]["posts"]
        if not 0 <= index < len(posts):
            raise ValueError("Публикация недоступна")
        selected = posts[index]
        with Session(engine) as db:
            user = owner(db, telegram)
            cached = dzen_history.headlines(
                db, user, bot, agents.test_mode(config), draft["token"], selected
            )
            cached_id = cached.id if cached else None
            offer = (
                agents.quote_generation(
                    db,
                    selected,
                    "Самостоятельные заголовки по теме выбранной статьи; "
                    "без выдуманного опыта автора и без привязки к неизвестной аудитории.",
                    [p["title"] for p in posts if p != selected],
                    draft["token"],
                )
                if cached_id is None
                else {}
            )
            enough, payment = (
                agents.payment_preview(db, config, user, bot, offer) if offer else (True, "")
            )
        draft = {**draft, "selected": selected}
        if cached_id is not None:
            await show_result(message, state, draft, telegram, bot, cached_id, cached=True)
            return
        generation = uuid4().hex[:12]
        draft = {**draft, "offer": offer, "phase": "confirm", "generation_token": generation}
        await state.set_state(None)
        await state.update_data(agent_draft=draft, active_tool="agents", paused=False)
        buttons = (
            [[("✨ Создать 5 заголовков", f"az:{draft['token']}:generate:{generation}")]]
            if enough
            else [[("🪙 Пополнить AI-баланс", "ait:packages")]]
        )
        await next_message(
            message,
            "✨ Пять оригинальных заголовков\n\n" + selected["title"] + "\n\n" + payment + "\n"
            "Опрос нужен позже — только для промпта вашего канала.",
            reply_markup=keyboard(
                buttons
                + [
                    [("← К подборке статей", f"az:{draft['token']}:posts:all")],
                    [("← Агенты", "section:agents")],
                ]
            ),
        )

    @router.callback_query(F.data == "section:agents")
    async def catalogue(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (
            not isinstance(callback.message, Message)
            or callback.message.chat.type != "private"
            or (await state.get_data()).get("mode") != "expert"
        ):
            await callback.answer("Откройте /start", show_alert=True)
            return
        balance = await asyncio.to_thread(funds, callback.from_user.id, bot.id)
        with Session(engine) as db:
            visible = visible_sections(db)
            credit_mode = credits.enabled(db)
            user = owner(db, callback.from_user.id)
            mode = ai_tokens.funding(db, config, user, bot.id, agents.test_mode(config), {})
            account_header = "♾ Безлимитный доступ" if mode == "unlimited" else header(balance)
            trials = (
                "\n".join(
                    f"🎁 {label}: "
                    f"{ai_tokens.free_remaining(db, user, bot.id, agents.test_mode(config), task)} "
                    "бесплатных попыток"
                    for task, label in ai_tokens.TRIAL_LABELS.items()
                )
                if mode != "unlimited"
                else ""
            )
        draft = (await state.get_data()).get("agent_draft", {})
        resume = (
            [[("↩ Мой незавершённый анализ", "ait:return")]]
            if draft.get("phase") in {"channel", "expert", "period", "posts", "confirm", "ready"}
            or (draft.get("phase") == "error" and draft.get("report"))
            else []
        )
        if (await state.get_data()).get("image_draft", {}).get("phase") in {"article", "confirm"}:
            resume.append([("↩ Промпты для картинок: вернуться", "ip:resume")])
        await state.set_state(None)
        await state.update_data(active_tool="agents", paused=False)
        await next_message(
            callback.message,
            account_header + "\n" + trials + "\n\n🤖 Агенты\n\n"
            "📰 Дзен — топ-5 статей конкурента и пять собственных заголовков.\n"
            "🖼 Картинки — готовая статья → до пяти промптов для изображений.\n"
            "✍️ Контент-аналитик: анализ ваших постов → темы и количество на ваш выбор; "
            "ссылка конкурента → ссылки публикаций и метрики, без генерации.\n"
            "🎁 Лид-магниты — 5 тем с описанием и форматом; критик по вашему выбору.\n"
            + (
                "📊 YouTube — топ-5 или топ-10 видео конкурента по просмотрам, без ИИ.\n"
                if "youtube" in visible
                else ""
            )
            + "\n"
            + (
                "Цена действия в кредитах показывается до запуска. "
                "Расход токенов ИИ — отдельная статистика. Дневного лимита нет.\n"
                if credit_mode
                else "Генерации оплачиваются по фактическому расходу. Дневного лимита нет.\n"
            )
            + "Бесплатные попытки выдаются один раз, не ежедневно. "
            "Цена поиска Дзен показывается перед запуском.\n\n"
            "Тексты и ответы опроса передаются внешнему ИИ для генерации. "
            "Не отправляйте пароли, платёжные данные и чужие персональные данные. "
            "Вводные и результаты сохраняются для повторного открытия; "
            "удалить свои данные можно в профиле. При остатке баланса — через поддержку.",
            reply_markup=keyboard(
                resume
                + agent_rows(visible)
                + [
                    [("📂 Мои промпты", "wp:open")],
                    [("🪙 Пополнить AI-баланс", "ait:packages"), ("📒 История", "ait:history")],
                    [("🌐 Подробные отчёты", "ait:web")],
                    [("🏠 В меню", "menu")],
                ]
            ),
        )
        await callback.answer()

    @router.callback_query(F.data == "az:begin", flags={"requires_access": True})
    async def begin(callback: CallbackQuery, state: FSMContext) -> None:
        if (
            not isinstance(callback.message, Message)
            or callback.message.chat.type != "private"
            or (await state.get_data()).get("mode") != "expert"
        ):
            await callback.answer("Откройте /start", show_alert=True)
            return
        with Session(engine) as db:
            enabled = setting(db, "ai_dzen_enabled")
        if (
            not enabled
            or not config.zenstat_api_key.get_secret_value()
            or not config.deepseek_api_key.get_secret_value()
        ):
            await callback.answer("Агент пока выключен или не настроен владельцем", show_alert=True)
            return
        draft = {"token": uuid4().hex[:12], "phase": "channel"}
        await state.set_state("agent_dzen_channel")
        await state.update_data(active_tool="agents", paused=False, agent_draft=draft)
        await next_message(
            callback.message,
            "📰 Агент Дзен\n\nПришлите ссылку канала конкурента, например https://dzen.ru/имя.\n"
            "На этом шаге ИИ не запускается.",
            reply_markup=keyboard([[("← Агенты", "section:agents")]]),
        )
        await callback.answer()

    @router.message(
        StateFilter("agent_dzen_channel", "agent_dzen_expert"),
        ~F.text.startswith("/"),
        flags={"requires_access": True},
    )
    async def answer(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        if (
            data.get("mode") != "expert"
            or message.chat.type != "private"
            or data.get("paused")
            or data.get("active_tool") != "agents"
        ):
            return
        draft = data.get("agent_draft", {})
        raw = (message.text or "").strip()
        try:
            if draft.get("phase") == "channel":
                slug = channel_slug(raw)
                assert message.from_user is not None and message.bot is not None
                with Session(engine) as db:
                    search_price = agents.search_price(db, "dzen_search")
                    search_offer = credits.quoted_value(
                        db, {"purpose": "dzen_search", "maximum": search_price}
                    )
                    _, search_payment = agents.payment_preview(
                        db,
                        config,
                        owner(db, message.from_user.id),
                        message.bot.id,
                        search_offer,
                    )
                await state.set_state(None)
                await state.update_data(
                    agent_draft={
                        **draft,
                        "slug": slug,
                        "phase": "period",
                        "search_price": search_price,
                        "search_mode": search_offer["confirmed_billing"],
                        "search_payment": search_payment,
                    }
                )
                await message.answer(
                    "📅 За какой период искать статьи?\n"
                    "Период относится к дате публикации, метрики накопленные.\n\n"
                    + search_payment
                    + "\nНажатие периода запускает поиск.",
                    reply_markup=keyboard(
                        [
                            [
                                ("Неделя", f"az:{draft['token']}:period:week"),
                                ("Месяц", f"az:{draft['token']}:period:1"),
                            ],
                            [
                                ("3 месяца", f"az:{draft['token']}:period:3"),
                                ("6 месяцев", f"az:{draft['token']}:period:6"),
                            ],
                            [("Год", f"az:{draft['token']}:period:12")],
                            [("← Изменить канал", f"az:{draft['token']}:channel:change")],
                        ]
                    ),
                )
            elif draft.get("phase") == "expert":
                # Compatibility for an old persisted survey; this question is no longer asked.
                if message.from_user and message.bot:
                    await select_article(
                        message,
                        state,
                        draft,
                        message.from_user.id,
                        message.bot.id,
                        draft["report"]["posts"].index(draft["selected"]),
                    )
        except ValueError as error:
            await message.answer("⚠️ " + str(error))

    @router.callback_query(F.data.startswith("az:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("agent_draft", {})
        parts = (callback.data or "").split(":")
        message = callback.message
        if (
            len(parts) != 4
            or not isinstance(message, Message)
            or message.chat.type != "private"
            or data.get("mode") != "expert"
        ):
            await callback.answer("Экран устарел. Откройте «Агенты».", show_alert=True)
            return
        _, token, operation, value = parts
        try:
            if operation in {"select", "posts"}:
                with Session(engine) as db, db.begin():
                    user = owner(db, callback.from_user.id)
                    if token == draft.get("token") and draft.get("report", {}).get("posts"):
                        dzen_history.save(
                            db, user, bot.id, agents.test_mode(config), token, draft["report"]
                        )
                    saved = dzen_history.get(db, user, bot.id, agents.test_mode(config), token)
                    report = dict(saved.report)
                draft = {"token": token, "phase": "posts", "report": report}
                if operation == "posts":
                    await show_posts(message, state, draft)
                elif value.isdigit():
                    await select_article(
                        message, state, draft, callback.from_user.id, bot.id, int(value)
                    )
                else:
                    raise ValueError("Публикация недоступна")
                await callback.answer()
                return
            if (
                data.get("paused")
                or data.get("active_tool") != "agents"
                or token != draft.get("token")
            ):
                await callback.answer(
                    "Откройте сохранённую подборку через «История».", show_alert=True
                )
                return
            if operation == "channel" and value == "change" and draft.get("phase") == "period":
                await state.update_data(agent_draft={**draft, "phase": "channel"})
                await state.set_state("agent_dzen_channel")
                await next_message(
                    message,
                    f"📰 Текущий канал: {draft['slug']}\n\n"
                    "Пришлите другую ссылку или эту же, чтобы вернуться к выбору периода. "
                    "Поиск ещё не запущен.",
                    reply_markup=keyboard([[("← Агенты", "section:agents")]]),
                )
                await callback.answer()
                return
            elif operation == "period" and draft.get("phase") == "period":
                if value not in {"week", "1", "3", "6", "12"}:
                    raise ValueError("Неизвестный период")
                await state.update_data(agent_draft={**draft, "phase": "working"})
                await callback.answer("Получаю статистику…")

                def run_stats() -> dict[str, Any]:
                    with Session(engine) as db:
                        id = owner(db, callback.from_user.id)
                    if "search_price" not in draft or "search_mode" not in draft:
                        raise ValueError("Условия поиска обновились. Укажите канал заново")
                    return agents.stats(
                        engine,
                        config,
                        id,
                        bot.id,
                        draft["slug"],
                        value,
                        token,
                        draft["search_price"],
                        draft["search_mode"],
                    )

                report = await asyncio.to_thread(run_stats)
                await state.update_data(agent_draft={**draft, "phase": "posts", "report": report})
                if not report["posts"]:
                    await message.answer(
                        "За период нет доступных статей. Возможно, канал ещё не индексируется.",
                        reply_markup=keyboard([[("← Агенты", "section:agents")]]),
                    )
                    return
                for index, post in enumerate(report["posts"], 1):
                    markup = keyboard([[("✨ Мои заголовки", f"az:{token}:select:{index - 1}")]])
                    markup.inline_keyboard.insert(
                        0, [InlineKeyboardButton(text="↗ Оригинал", url=post["url"])]
                    )
                    await message.answer(
                        f"{index}. {post['title']}\n\n"
                        f"📅 {post['date_publish'][:10]}\n"
                        f"📖 Дочитывания: {post['views_till_end'] if post['views_till_end'] is not None else 'нет данных'}\n"  # noqa: E501
                        f"👁 Просмотры: {post['views'] if post['views'] is not None else 'нет данных'}\n"  # noqa: E501
                        "По данным Zenstat; не гарантия полного охвата канала.",
                        reply_markup=markup,
                    )
                return
            if (
                operation == "generate"
                and draft.get("phase") == "confirm"
                and value == draft.get("generation_token")
            ):
                await state.update_data(agent_draft={**draft, "phase": "working"})
                await callback.answer("Создаю заголовки…")

                def run_generation() -> int:
                    with Session(engine) as db:
                        id = owner(db, callback.from_user.id)
                    return agents.generate(
                        engine,
                        config,
                        id,
                        bot.id,
                        "dzen:" + draft.get("generation_token", token),
                        draft["offer"],
                        require_confirmation=True,
                    )

                number = await asyncio.to_thread(run_generation)
                await show_result(message, state, draft, callback.from_user.id, bot.id, number)
                return
            if operation == "prompt" and draft.get("phase") == "ready" and value in {"A", "B", "C"}:
                with Session(engine) as db:
                    agents.operation(
                        db,
                        owner(db, callback.from_user.id),
                        bot.id,
                        agents.test_mode(config),
                        draft["operation_id"],
                    )
                    saved_prompt = agents.writing_profile(
                        db, owner(db, callback.from_user.id), bot.id, agents.test_mode(config)
                    )
                    if saved_prompt is None:
                        await message.answer(
                            "Настройте один промпт для своего канала.",
                            reply_markup=keyboard([[("📝 Мой промпт", "wp:open")]]),
                        )
                        await callback.answer()
                        return
                    prompt = (saved_prompt.result or {})["prompt"]
                await callback.answer()
                await message.answer_document(
                    BufferedInputFile(prompt.encode(), filename=f"dzen-prompt-{value}.md"),
                    caption="Промпт для внешней нейросети. Дополнительного списания нет.",
                )
                return
            raise ValueError("Экран устарел. Откройте «Агенты».")
        except (ValueError, AgentError, ObjectUnavailable) as error:
            await message.answer(
                "⚠️ " + (str(error) or "Доступ недоступен"),
                reply_markup=keyboard(
                    ([[("↩ К найденным статьям", "ait:return")]] if draft.get("report") else [])
                    + [[("← Агенты", "section:agents")]]
                ),
            )
            # A fresh input can start again; paid uncertain calls are never silently retried.
            await state.update_data(agent_draft={**draft, "phase": "error"})
            await callback.answer()

    @router.callback_query(F.data.startswith("ait:"))
    async def token_actions(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        message = callback.message
        if (
            not isinstance(message, Message)
            or message.chat.type != "private"
            or (await state.get_data()).get("mode") != "expert"
        ):
            await callback.answer("Откройте /start", show_alert=True)
            return
        code = callback.data or ""
        try:
            balance = await asyncio.to_thread(funds, callback.from_user.id, bot.id)
            if code == "ait:return":
                draft = (await state.get_data()).get("agent_draft", {})
                if draft.get("report") and draft.get("phase") in {"ready", "expert", "error"}:
                    await show_posts(message, state, draft)
                    await callback.answer()
                    return
                if draft.get("phase") == "error" and draft.get("report"):
                    draft = {**draft, "phase": "posts"}
                    await state.update_data(agent_draft=draft)
                phase, token = draft.get("phase"), draft.get("token")
                if not token or phase not in {
                    "channel",
                    "expert",
                    "period",
                    "posts",
                    "confirm",
                    "ready",
                }:
                    raise ValueError("Нет незавершённой задачи. Откройте агента заново")
                await state.update_data(active_tool="agents", paused=False)
                rows_back: list[list[tuple[str, str]]] = []
                if phase in {"channel", "expert"}:
                    await state.set_state("agent_dzen_" + phase)
                    text_back = (
                        "Пришлите ссылку канала конкурента."
                        if phase == "channel"
                        else "О чём ваш канал и для кого? Укажите тон."
                    )
                elif phase == "period":
                    await state.set_state(None)
                    text_back = "Выберите период публикации статей.\n\n" + draft.get(
                        "search_payment", "Укажите канал заново, чтобы увидеть цену поиска."
                    )
                    rows_back = [
                        [(label, f"az:{token}:period:{period}")]
                        for label, period in [
                            ("Неделя", "week"),
                            ("Месяц", "1"),
                            ("3 месяца", "3"),
                            ("6 месяцев", "6"),
                            ("Год", "12"),
                        ]
                    ]
                    rows_back.append([("← Изменить канал", f"az:{token}:channel:change")])
                elif phase == "posts":
                    text_back = (
                        "Выберите статью из сохранённого топа. Новый запрос Zenstat не нужен."
                    )
                    rows_back = [
                        [(f"{index + 1}. {post['title'][:45]}", f"az:{token}:select:{index}")]
                        for index, post in enumerate(draft["report"]["posts"])
                    ]
                elif phase == "confirm":
                    maximum = draft["offer"]["maximum"]
                    text_back = (
                        f"На создание потребуется до {maximum} токенов. "
                        "Спишем только фактически использованные токены."
                    )
                    with Session(engine) as db:
                        enough, payment = agents.payment_preview(
                            db, config, owner(db, callback.from_user.id), bot.id, draft["offer"]
                        )
                    await state.update_data(agent_draft=draft)
                    text_back = payment
                    if enough:
                        rows_back = [
                            [
                                (
                                    "✨ Создать 5 заголовков",
                                    f"az:{token}:generate:"
                                    f"{draft.get('generation_token', 'expired')}",
                                )
                            ]
                        ]
                    else:
                        text_back += "\nНе хватает токенов. Пополните баланс и вернитесь к задаче."
                    rows_back.append([("🪙 Пополнить AI-баланс", "ait:packages")])
                else:
                    text_back = "Заголовки сохранены. Откройте отчёт или свой промпт для статей."
                    rows_back = [
                        [("📝 Мой промпт для статей", "wp:open")],
                        [("🌐 Отчёт", "ait:web")],
                    ]
                await next_message(
                    message,
                    header(balance) + "\n\n" + text_back,
                    reply_markup=keyboard(rows_back + [[("← Агенты", "section:agents")]]),
                )
            elif code == "ait:history":
                await next_message(
                    message,
                    "📒 История агентов\n\n"
                    "Подборки статей и результаты ИИ сохраняются отдельно.\n"
                    "Открытие истории без запросов к Zenstat и ИИ.",
                    reply_markup=keyboard(
                        [
                            [("📚 Подборки статей", "ait:searches")],
                            [("📄 Все результаты", "ait:operations")],
                            [("🌐 История в кабинете", "ait:web")],
                            [("← Агенты", "section:agents")],
                        ]
                    ),
                )
            elif code == "ait:searches":
                draft = (await state.get_data()).get("agent_draft", {})
                with Session(engine) as db, db.begin():
                    user = owner(db, callback.from_user.id)
                    if draft.get("report", {}).get("posts") and draft.get("token"):
                        dzen_history.save(
                            db,
                            user,
                            bot.id,
                            agents.test_mode(config),
                            draft["token"],
                            draft["report"],
                        )
                    searches = dzen_history.latest(db, user, bot.id, agents.test_mode(config))
                    buttons = [
                        [
                            (
                                f"{s.created_at:%d.%m} · {s.report.get('slug', 'Канал')} "
                                f"· {len(s.report['posts'])} статей",
                                f"az:{s.token}:posts:all",
                            )
                        ]
                        for s in searches
                    ]
                await next_message(
                    message,
                    "📚 Подборки статей\n\n"
                    + (
                        "Выберите сохранённую подборку. Нового запроса Zenstat нет."
                        if buttons
                        else "Подборок пока нет. Запустите анализ канала."
                    ),
                    reply_markup=keyboard(buttons + [[("← История", "ait:history")]]),
                )
            elif code.startswith("ait:result:"):
                number = int(code.split(":")[-1])
                with Session(engine) as db:
                    user = owner(db, callback.from_user.id)
                    row = agents.operation(db, user, bot.id, agents.test_mode(config), number)
                    if row.status != "completed" or not row.result:
                        raise ValueError("У операции нет готового результата")
                    if row.result.get("prompt"):
                        prompt = row.result["prompt"]
                        report = None
                    else:
                        prompt = None
                        token = row.snapshot.get("search_token")
                        report = (
                            dzen_history.get(
                                db, user, bot.id, agents.test_mode(config), token
                            ).report
                            if token
                            else None
                        )
                        snapshot = dict(row.snapshot)
                if row.snapshot.get("workflow") == "competitor":
                    from leedbox.domain.content_agent import competitor_markdown

                    await message.answer_document(
                        BufferedInputFile(
                            competitor_markdown(row.result or {}).encode(),
                            filename="competitor-links.md",
                        ),
                        caption="Сохранённые ссылки и метрики. Новых списаний нет.",
                    )
                    await next_message(
                        message,
                        "Анализ конкурента сохранён.",
                        reply_markup=keyboard(
                            [
                                [("🌐 Открыть отчёт", f"ait:report:{number}")],
                                [("← История", "ait:operations")],
                            ]
                        ),
                    )
                elif row.snapshot.get("purpose") in {
                    "content",
                    "lead_magnet",
                    "lead_magnet_revision",
                }:
                    from leedbox.domain.content_agent import markdown
                    from leedbox.domain.lead_magnet_agent import markdown as lm_markdown

                    is_lm = row.snapshot.get("purpose") != "content"
                    await message.answer_document(
                        BufferedInputFile(
                            (
                                lm_markdown(row.result or {})
                                if is_lm
                                else markdown(row.result or {})
                            ).encode(),
                            filename="lead-magnets.md" if is_lm else "content-posts.md",
                        ),
                        caption="Сохранённые материалы. Открытие бесплатно.",
                    )
                    await next_message(
                        message,
                        "Сохранённые материалы доступны для копирования.",
                        reply_markup=keyboard(
                            [
                                [("🌐 Открыть и скопировать", f"ait:report:{number}")],
                                [("← История", "ait:operations")],
                            ]
                        ),
                    )
                elif row.snapshot.get("purpose") == "image_prompts":
                    await show_images(
                        message,
                        number,
                        row.result or {},
                        ai_tokens.receipt(row) + "\nСохранённый результат. Новых списаний нет.",
                        from_history=True,
                    )
                elif prompt:
                    await message.answer_document(
                        BufferedInputFile(prompt.encode(), filename=f"dzen-prompt-{number}.md"),
                        caption="Сохранённая версия. Новых списаний нет.",
                    )
                    await next_message(
                        message,
                        "Сохранённый промпт. Открытие бесплатно.",
                        reply_markup=keyboard([[("← К списку результатов", "ait:operations")]]),
                    )
                else:
                    draft = {
                        "token": snapshot.get("search_token") or uuid4().hex[:12],
                        "report": report or {"posts": []},
                    }
                    await show_result(
                        message,
                        state,
                        draft,
                        callback.from_user.id,
                        bot.id,
                        number,
                        cached=True,
                        from_history=True,
                    )
            elif code == "ait:operations" or code.startswith("ait:operations:"):
                raw_page = code.removeprefix("ait:operations:") if code != "ait:operations" else "0"
                if not raw_page.isascii() or not raw_page.isdecimal() or len(raw_page) > 6:
                    raise ValueError("Неверная страница результатов")
                page = int(raw_page)
                if page > 100000:
                    raise ValueError("Неверная страница результатов")
                with Session(engine) as db:
                    rows = list(
                        db.scalars(
                            select(AIOperation)
                            .where(
                                AIOperation.owner_user_id == owner(db, callback.from_user.id),
                                AIOperation.bot_id == bot.id,
                                AIOperation.test_mode == agents.test_mode(config),
                                or_(
                                    AIOperation.kind == "generation",
                                    AIOperation.snapshot["workflow"].astext == "competitor",
                                ),
                                AIOperation.status == "completed",
                                AIOperation.result.is_not(None),
                            )
                            .order_by(AIOperation.id.desc())
                            .offset(page * 10)
                            .limit(11)
                        )
                    )
                    buttons = [
                        [
                            (
                                f"{r.created_at:%d.%m} · "
                                + (
                                    "Ссылки и метрики конкурента"
                                    if r.snapshot.get("workflow") == "competitor"
                                    else "Темы для лид-магнитов"
                                    if r.snapshot.get("purpose")
                                    in {"lead_magnet", "lead_magnet_revision"}
                                    else "Анализ постов"
                                    if r.snapshot.get("content_phase") == "analysis"
                                    else "Готовые посты"
                                    if r.snapshot.get("purpose") == "content"
                                    else "Промпт канала"
                                    if r.snapshot.get("purpose") == "writing_profile"
                                    else "Промпты для картинок"
                                    if r.snapshot.get("purpose") == "image_prompts"
                                    else str(
                                        r.snapshot.get("inputs", {})
                                        .get("selected", {})
                                        .get("title", "Заголовки")
                                    )[:45]
                                ),
                                f"ait:result:{r.id}",
                            )
                        ]
                        for r in rows[:10]
                        if r.status == "completed"
                    ]
                pages = []
                if page:
                    pages.append(("← Новее", f"ait:operations:{page - 1}"))
                if len(rows) > 10:
                    pages.append(("Старее →", f"ait:operations:{page + 1}"))
                if pages:
                    buttons.append(pages)
                await next_message(
                    message,
                    f"📒 Результаты агентов · страница {page + 1}\n\n"
                    + (
                        "Выберите сохранённый результат — открытие бесплатно."
                        if rows
                        else "На этой странице результатов нет."
                    ),
                    reply_markup=keyboard(buttons + [[("← История", "ait:history")]]),
                )
            elif code == "ait:packages":
                # Returning to the catalogue cancels the previous purchase confirmation.
                await state.update_data(ai_package_offer={})
                with Session(engine) as db:
                    items = agents.validate_packages(setting(db, "ai_token_packages"))
                package_buttons = [
                    [(f"{p['tokens']:,} токенов — {p['price_rub']} ₽", f"ait:offer:{p['id']}")]
                    for p in items
                    if p["enabled"]
                ]
                await next_message(
                    message,
                    header(balance) + "\n\n🪙 Пакеты AI-токенов\n"
                    "Генерации списывают фактический вход + выход ИИ.\n"
                    "Анализ источников имеет отдельную фиксированную цену в токенах.\n"
                    "Пакет покупается с рублёвого баланса и не продлевает подписку.\n"
                    + (
                        "Владелец ещё не опубликовал пакеты."
                        if not package_buttons
                        else "Выберите пакет; перед списанием будет подтверждение."
                    ),
                    reply_markup=keyboard(
                        package_buttons
                        + [
                            [("💳 Рублёвый баланс", "section:wallet")],
                            [("👤 Мой аккаунт", "account:open")],
                            [("← Агенты", "section:agents")],
                        ]
                    ),
                )
            elif code.startswith("ait:offer:"):
                with Session(engine) as db:
                    offer = agents.package_offer(db, config, code.split(":", 2)[2])
                    rubles = wallet.balance(
                        db, owner(db, callback.from_user.id), bot.id, offer["test"]
                    )
                token = uuid4().hex[:12]
                await state.update_data(ai_package_offer={"token": token, "offer": offer})
                await next_message(
                    message,
                    f"Купить {offer['tokens']:,} токенов за {offer['price_rub']} ₽?\n"
                    f"Баланс: {rubles:.2f} ₽\n"
                    f"После покупки: {rubles - Decimal(offer['price_rub']):.2f} ₽\n"
                    "Подписка и её срок не изменятся.\n"
                    f"\nУсловия: {offer['terms']}\nПоддержка: {offer['support']}\n\n"
                    + (
                        "🧪 Тестовый баланс; реальные средства не списываются."
                        if offer["test"]
                        else "Средства спишутся с рублёвого баланса."
                    ),
                    reply_markup=keyboard(
                        [
                            [("✅ Купить", "ait:buy:" + token)],
                            [("← Выбрать другой пакет", "ait:packages")],
                        ]
                    ),
                )
            elif code.startswith("ait:buy:"):
                draft = (await state.get_data()).get("ai_package_offer", {})
                if code.split(":", 2)[2] != draft.get("token"):
                    raise ValueError("Подтверждение устарело")
                with Session(engine) as db, db.begin():
                    row = agents.buy_package(
                        db,
                        config,
                        owner(db, callback.from_user.id),
                        bot.id,
                        "package:" + draft["token"],
                        draft["offer"],
                    )
                    after = row.balance_after
                    rubles = wallet.balance(
                        db, owner(db, callback.from_user.id), bot.id, draft["offer"]["test"]
                    )
                await next_message(
                    message,
                    f"✅ Пакет зачислен: +{draft['offer']['tokens']:,} AI-токенов.\n"
                    f"Списано: {draft['offer']['price_rub']} ₽\n"
                    f"Рублёвый баланс: {rubles:.2f} ₽\n"
                    f"Остаток: {after:,} токенов.\nПодписка не изменилась.",
                    reply_markup=keyboard(
                        [[("👤 Мой аккаунт", "account:open")], [("← Агенты", "section:agents")]]
                    ),
                )
            elif code == "ait:web" or code.startswith("ait:report:"):
                destination = "next=agents"
                with Session(engine) as db, db.begin():
                    if code.startswith("ait:report:"):
                        number = int(code.split(":")[2])
                        agents.operation(
                            db,
                            owner(db, callback.from_user.id),
                            bot.id,
                            agents.test_mode(config),
                            number,
                        )
                        destination += f"&report={number}"
                    url = issue_ticket(db, config, callback.from_user.id).replace(
                        "/login#", f"/login?{destination}#"
                    )
                button = (
                    InlineKeyboardButton(
                        text="📋 Скопировать личную ссылку", copy_text=CopyTextButton(text=url)
                    )
                    if urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
                    else InlineKeyboardButton(text="🌐 Открыть отчёты", url=url)
                )
                markup = keyboard([[("← Агенты", "section:agents")]])
                markup.inline_keyboard.insert(0, [button])
                await next_message(
                    message,
                    "🔐 Личный кабинет: ссылка действует 5 минут, не пересылайте её.\n"
                    "При локальном тестировании скопируйте ссылку в браузер этого компьютера.",
                    reply_markup=markup,
                )
            else:
                raise ValueError("Неизвестное действие")
        except (ValueError, ObjectUnavailable) as error:
            await callback.answer(str(error) or "Недоступно", show_alert=True)
            return
        await callback.answer()

    router.include_router(build_writing_router(engine, config))
    router.include_router(build_image_router(engine, config))
    router.include_router(build_content_router(engine, config))
    router.include_router(build_lead_magnet_router(engine, config))
    return router
