"""Expert-owned notification choices, separate from gift-author consents."""

import asyncio

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application import legal
from leedbox.application.broadcasts import (
    CONSENT_TEXT,
    advertising_version,
    change_preference,
    preferences,
)
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.config import Settings
from leedbox.telegram.legal import document_button
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.material_wizard import buttons


def build_notification_router(engine: Engine, config: Settings | None = None) -> Router:
    router = Router()

    @router.callback_query(F.data.startswith("notify:"))
    async def notifications(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        # Always allow revocation from a delivered broadcast, even if the expert's
        # current screen is a gift. Recipient-only users fail the server-side check.
        parts = (callback.data or "").split(":")
        if not isinstance(callback.message, Message):
            return
        try:
            if (
                len(parts) == 4
                and parts[1] == "set"
                and parts[2] in {"notice", "advertising"}
                and parts[3] in {"0", "1"}
            ):
                enabled = parts[3] == "1"
                if enabled and (await state.get_data()).get("mode") != "expert":
                    raise ObjectUnavailable
                if enabled and parts[2] == "advertising":
                    with Session(engine) as db:
                        doc = legal.current(db, "advertising")
                        version = advertising_version(db)
                    await state.update_data(advertising_offer=version)
                    document_rows = (
                        [
                            [
                                document_button(
                                    "Согласие на рекламу",
                                    config or Settings(),
                                    "advertising",
                                    doc.number,
                                )
                            ]
                        ]
                        if doc
                        else []
                    )
                    await state.set_state(None)
                    await state.update_data(paused=True, active_tool=None)
                    await next_message(
                        callback.message,
                        "📣 Рекламные сообщения владельца\n\n"
                        + CONSENT_TEXT
                        + (
                            "\n\nОзнакомьтесь с полным текстом согласия по кнопке ниже."
                            if doc
                            else ""
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=document_rows
                            + buttons(
                                [
                                    [("Согласен получать рекламу", "notify:consent:1")],
                                    [("⬅️ Назад", "notify:open")],
                                ]
                            ).inline_keyboard
                        ),
                    )
                    await callback.answer()
                    return

                def update() -> None:
                    with Session(engine) as db, db.begin():
                        change_preference(db, callback.from_user.id, bot.id, parts[2], enabled)

                await asyncio.to_thread(update)
                if (await state.get_data()).get("mode") != "expert":
                    await callback.answer("Уведомления отключены", show_alert=True)
                    return
            elif parts == ["notify", "consent", "1"]:
                if (await state.get_data()).get("mode") != "expert":
                    raise ObjectUnavailable

                offer = (await state.get_data()).get("advertising_offer")

                def consent() -> None:
                    with Session(engine) as db, db.begin():
                        change_preference(
                            db, callback.from_user.id, bot.id, "advertising", True, offer
                        )

                await asyncio.to_thread(consent)
                await state.update_data(advertising_offer=None)
            elif parts != ["notify", "open"]:
                raise ValueError
            if (await state.get_data()).get("mode") != "expert":
                raise ObjectUnavailable

            def read() -> tuple[bool, bool]:
                with Session(engine) as db, db.begin():
                    item = preferences(db, callback.from_user.id, bot.id)
                    return (
                        item.notices,
                        item.advertising and item.consent_version == advertising_version(db),
                    )

            notices, ads = await asyncio.to_thread(read)
            await state.set_state(None)
            await state.update_data(paused=True, active_tool=None)
            await next_message(
                callback.message,
                "🔔 Уведомления владельца\n\n"
                + f"🛠 Новости сервиса: {'включены' if notices else 'отключены'}\n"
                "Новые функции, промпты и изменения в работе бота.\n\n"
                + f"📣 Реклама: {'разрешена' if ads else 'не разрешена'}\n"
                "Включается только с вашим отдельным согласием.\n\n"
                "Выбор не влияет на доступ к инструментам.",
                reply_markup=buttons(
                    [
                        [
                            (
                                "Отключить новости" if notices else "Включить новости",
                                f"notify:set:notice:{int(not notices)}",
                            )
                        ],
                        [
                            (
                                "Отключить рекламу" if ads else "Разрешить рекламу",
                                f"notify:set:advertising:{int(not ads)}",
                            )
                        ],
                        [("⬅️ Профиль", "section:profile")],
                    ]
                ),
            )
            await callback.answer()
        except (ValueError, ObjectUnavailable):
            await callback.answer(
                "Настройки недоступны. Для инструментов откройте /start", show_alert=True
            )

    return router
