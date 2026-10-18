"""YouTube extension of the native Telegram flow: one question per new message.

Operate: preserve incumbent keyboards, bottom-of-chat navigation and semantic colors.
Channel → publication period → top size → raw YouTube statistics and CSV export.
Never present partial coverage as a complete channel ranking, or AI as part of this tool.
"""

import asyncio
from typing import Any
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    LinkPreviewOptions,
    Message,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application import agents, youtube_billing
from leedbox.application.youtube import analyze, cached_snapshot
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.youtube import (
    ChannelSnapshot,
    channel_reference,
    report_csv,
    report_text,
    top_videos,
)
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import User
from leedbox.infrastructure.youtube import YouTubeError
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message, retire


def period_rows(token: str) -> list[list[tuple[str, str]]]:
    return [
        [("📅 30 дней", f"yt:{token}:period:30"), ("📅 90 дней", f"yt:{token}:period:90")],
        [("📅 Год", f"yt:{token}:period:365"), ("🗂 Вся выборка", f"yt:{token}:period:0")],
        [("← Другой канал", "section:youtube")],
        [("🏠 В меню", "menu")],
    ]


def build_youtube_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    @router.callback_query(F.data == "section:youtube", flags={"requires_access": True})
    @router.message(Command("youtube"), flags={"requires_access": True})
    async def begin(event: CallbackQuery | Message, state: FSMContext, bot: Bot) -> None:
        message = event.message if isinstance(event, CallbackQuery) else event
        data = await state.get_data()
        if (
            data.get("mode") != "expert"
            or not isinstance(message, Message)
            or message.chat.type != "private"
        ):
            if isinstance(event, CallbackQuery):
                await event.answer("Откройте /start для инструментов", show_alert=True)
            else:
                await event.answer("Откройте /start для инструментов.")
            return
        if not config.youtube_api_key.get_secret_value():
            text = "YouTube пока не настроен. Владелец должен добавить LEEDBOX_YOUTUBE_API_KEY."
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            else:
                await event.answer(text)
            return
        token = uuid4().hex[:12]
        sent = await next_message(
            message,
            "📊 Анализ YouTube\n\n"
            "Найдите самые просматриваемые видео конкурента — без подключения его аккаунта.\n\n"
            "Пришлите одну ссылку на канал:\nhttps://www.youtube.com/@GoogleDevelopers\n\n"
            f"Проверяем до {config.youtube_scan_limit} записей загрузок. "
            "После ссылки выберите период публикации и топ-5 или топ-10.\n"
            "Это подборка по просмотрам, не оценка продаж или качества контента.",
            reply_markup=keyboard([[("← Агенты", "section:agents")], [("🏠 В меню", "menu")]]),
        )
        await state.set_state("youtube_channel")
        await state.update_data(
            active_tool="youtube",
            paused=False,
            youtube_draft={"token": token, "phase": "channel", "screen": sent.message_id},
        )
        if isinstance(event, CallbackQuery):
            await event.answer()

    @router.message(
        StateFilter("youtube_channel"), ~F.text.startswith("/"), flags={"requires_access": True}
    )
    async def channel(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("youtube_draft", {})
        if (
            data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "youtube"
            or draft.get("phase") != "channel"
        ):
            return
        raw = (message.text or "").strip()
        try:
            channel_reference(raw)
        except ValueError as error:
            await message.answer(
                "⚠️ " + str(error), reply_markup=keyboard([[("🏠 В меню", "menu")]])
            )
            return
        await retire(bot, message.chat.id, draft.get("screen"))
        sent = await message.answer(
            "📅 За какой период искать видео?\n\n"
            "Выбираем дату публикации. Просмотры показываются за всё время жизни ролика.\n\n"
            "«Вся выборка» — все видео в пределах лимита проверки.",
            reply_markup=keyboard(period_rows(draft["token"])),
        )
        await state.set_state(None)
        await state.update_data(
            youtube_draft={**draft, "raw": raw, "phase": "period", "screen": sent.message_id}
        )

    @router.callback_query(F.data.startswith("yt:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("youtube_draft", {})
        parts = (callback.data or "").split(":")
        message = callback.message
        if (
            len(parts) != 4
            or not isinstance(message, Message)
            or data.get("mode") != "expert"
            or data.get("paused")
            or data.get("active_tool") != "youtube"
            or parts[1] != draft.get("token")
            or message.message_id != draft.get("screen")
        ):
            await callback.answer(
                "Экран устарел. Откройте «Агенты» → «Анализ YouTube».", show_alert=True
            )
            return
        _, token, operation, value = parts
        if operation == "period" and value in {"0", "30", "90", "365"}:
            if draft.get("phase") not in {"period", "count"}:
                await callback.answer("Откройте проверку заново", show_alert=True)
                return

            def preview() -> tuple[dict[str, Any], bool, str]:
                with Session(engine) as db:
                    owner = db.scalar(
                        select(User.id).where(User.telegram_id == callback.from_user.id)
                    )
                    if owner is None:
                        raise ObjectUnavailable
                    offer = youtube_billing.quote(db, config, owner, bot.id, draft["raw"])
                    if offer["reuse_id"]:
                        return offer, True, "♻️ Сохранённый анализ. Повторных списаний нет."
                    enough, payment = agents.payment_preview(db, config, owner, bot.id, offer)
                    return offer, enough, payment

            offer, enough, payment = await asyncio.to_thread(preview)
            sent = await next_message(
                message,
                "🎬 Сколько видео показать?\n\n"
                "Отсортируем доступные видео по общему числу просмотров.\n\n"
                + payment
                + ("\nНажмите «Топ», чтобы запустить анализ на этих условиях." if enough else ""),
                reply_markup=keyboard(
                    [
                        [
                            ("🎬 Топ-5", f"yt:{token}:count:5"),
                            ("🎬 Топ-10", f"yt:{token}:count:10"),
                        ]
                        if enough
                        else [("🪙 Купить токены", "ait:packages")],
                        [("← Изменить период", f"yt:{token}:back:period")],
                        [("🏠 В меню", "menu")],
                    ]
                ),
            )
            await state.update_data(
                youtube_draft={
                    **draft,
                    "days": int(value),
                    "offer": offer,
                    "phase": "count",
                    "screen": sent.message_id,
                }
            )
            await callback.answer()
            return
        if operation == "back" and value == "period" and draft.get("phase") == "count":
            sent = await next_message(
                message, "📅 Выберите период публикации.", reply_markup=keyboard(period_rows(token))
            )
            await state.update_data(
                youtube_draft={**draft, "phase": "period", "screen": sent.message_id}
            )
            await callback.answer()
            return
        if operation == "csv" and value == "download" and draft.get("phase") == "ready":
            try:
                if draft.get("operation_id"):

                    def saved_snapshot() -> ChannelSnapshot:
                        with Session(engine) as db:
                            owner = db.scalar(
                                select(User.id).where(User.telegram_id == callback.from_user.id)
                            )
                            if owner is None:
                                raise ObjectUnavailable
                            return youtube_billing.result(
                                db, owner, bot.id, agents.test_mode(config), draft["operation_id"]
                            )[0]

                    snapshot = await asyncio.to_thread(saved_snapshot)
                else:
                    snapshot = await asyncio.to_thread(
                        cached_snapshot, engine, config, draft["raw"]
                    )
            except (YouTubeError, ValueError, ObjectUnavailable) as error:
                await callback.answer(str(error), show_alert=True)
                return
            current = await state.get_data()
            current_draft = current.get("youtube_draft", {})
            if (
                current.get("mode") != "expert"
                or current.get("active_tool") != "youtube"
                or current.get("paused")
                or current_draft.get("token") != token
                or current_draft.get("phase") != "ready"
                or current_draft.get("screen") != message.message_id
            ):
                await callback.answer("Экран устарел. Откройте проверку заново.", show_alert=True)
                return
            await callback.answer()
            await message.answer_document(
                BufferedInputFile(
                    report_csv(snapshot, draft["days"], draft["count"]), filename="youtube-top.csv"
                )
            )
            return
        if operation != "count" or value not in {"5", "10"} or draft.get("phase") != "count":
            await callback.answer("Экран устарел. Начните проверку заново.", show_alert=True)
            return
        if not draft.get("offer"):
            await callback.answer(
                "Выберите период заново, чтобы увидеть условия анализа.", show_alert=True
            )
            return
        await state.update_data(youtube_draft={**draft, "phase": "working"})
        await callback.answer("Проверяю публичные данные YouTube…")
        progress = await next_message(
            message,
            "⏳ Проверяю канал и статистику видео…\nЭто может занять до полутора минут.",
            reply_markup=keyboard([[("🏠 В меню", "menu")]]),
        )
        try:

            def run() -> tuple[ChannelSnapshot, bool, int, str]:
                with Session(engine) as db:
                    owner = db.scalar(
                        select(User.id).where(User.telegram_id == callback.from_user.id)
                    )
                    if owner is None:
                        raise ObjectUnavailable

                def fetch(
                    scan_engine: Engine,
                    scan_config: Settings,
                    scan_owner: int,
                    scan_bot: int,
                    raw: str,
                ) -> tuple[ChannelSnapshot, bool]:
                    return analyze(
                        scan_engine, scan_config, scan_owner, scan_bot, raw, metered=True
                    )

                number = youtube_billing.execute(
                    engine, config, owner, bot.id, "youtube:" + token, draft["offer"], fetch
                )
                with Session(engine) as db:
                    result, cached = youtube_billing.result(
                        db, owner, bot.id, agents.test_mode(config), number
                    )
                    payment = youtube_billing.receipt(
                        db, owner, bot.id, agents.test_mode(config), number
                    )
                return result, cached, number, payment

            snapshot, cached, number, payment = await asyncio.to_thread(run)
            draft = {**draft, "operation_id": number}
        except (YouTubeError, ObjectUnavailable, ValueError) as error:
            current = await state.get_data()
            if (
                current.get("mode") == "expert"
                and current.get("active_tool") == "youtube"
                and not current.get("paused")
                and current.get("youtube_draft", {}).get("token") == token
            ):
                await retire(bot, message.chat.id, progress.message_id)
                await message.answer(
                    "⚠️ " + (str(error) or "Доступ закончился. Откройте /start."),
                    reply_markup=keyboard(
                        [
                            [("🔄 Новая проверка", "section:youtube")],
                            [("🪙 Купить токены", "ait:packages")],
                            [("← Агенты", "section:agents")],
                            [("🏠 В меню", "menu")],
                        ]
                    ),
                )
                await state.update_data(paused=True)
            return
        current = await state.get_data()
        if (
            current.get("mode") != "expert"
            or current.get("active_tool") != "youtube"
            or current.get("paused")
            or current.get("youtube_draft", {}).get("token") != token
        ):
            return
        await retire(bot, message.chat.id, progress.message_id)
        count = int(value)
        text = report_text(snapshot, draft["days"], count)
        text += "\n\n" + payment
        if cached:
            text += "\n♻️ Сохранённая проверка: кэш действует 6 часов. Дата статистики указана выше."
        markup = keyboard(
            [
                [("📥 Скачать CSV", f"yt:{token}:csv:download")],
                [("🔄 Другой канал", "section:youtube")],
                [("← Агенты", "section:agents")],
                [("🏠 В меню", "menu")],
            ]
        )
        videos = top_videos(snapshot, draft["days"], count)
        links = [
            InlineKeyboardButton(text=f"🎬 Видео {i}", url=v.url) for i, v in enumerate(videos, 1)
        ]
        markup.inline_keyboard[:0] = [links[i : i + 2] for i in range(0, len(links), 2)]
        # Telegram's 4096 limit: split at line boundaries, keep actions at the bottom.
        chunks: list[str] = [""]
        for line in text.splitlines(keepends=True):
            if len(chunks[-1]) + len(line) > 3500:
                chunks.append("")
            chunks[-1] += line
        for index, chunk in enumerate(chunks):
            sent = await message.answer(
                chunk,
                reply_markup=markup if index == len(chunks) - 1 else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        await state.update_data(
            youtube_draft={**draft, "count": count, "phase": "ready", "screen": sent.message_id}
        )

    return router
