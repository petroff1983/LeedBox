"""Current link tools plus read/export and revocation of saved legacy links."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.invites import join_summary
from leedbox.application.links import (
    get_link,
    search_links,
    set_enabled,
    stats,
    utm_csv,
)
from leedbox.application.results import get_result
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.links import text_field
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    ToolResult,
    TrackedLink,
    User,
)
from leedbox.telegram.invites import revoke_invite
from leedbox.telegram.library import keyboard
from leedbox.telegram.utm import build_utm_router


def menu_rows() -> list[list[tuple[str, str]]]:
    return [
        [("🔴 UTM Директ", "ut:new:yandex")],
        [("🔵 UTM ВК", "ut:new:vk")],
        [("🔗 Короткая ссылка", "sl:new:short")],
        [("🏠 В меню", "menu")],
    ]


async def edit(message: Message, text: str, rows: list[list[tuple[str, str]]]) -> None:
    try:
        await message.edit_text(text, reply_markup=keyboard(rows))
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.lower():
            raise


def invite_card(session: Session, link: TrackedLink) -> str:
    statuses = {
        "pending": "Не создано — повторите после проверки прав",
        "creating": "Запрос отправлен; результат уточняется. Автоматического повтора нет",
        "active": "Действует" if link.enabled else "Редирект отключён",
        "failed": "Telegram отклонил создание; можно повторить",
        "uncertain": (
            "Результат создания неизвестен. Проверьте приглашения в настройках канала; "
            "автоматического повтора нет"
        ),
        "revoking": "Редирект отключён, отзыв в Telegram ещё не подтверждён",
        "revoke_failed": (
            "Редирект отключён; Telegram отклонил отзыв. Прямая ссылка может действовать"
        ),
        "revoke_uncertain": (
            "Редирект отключён; отзыв не подтверждён. Прямая ссылка может действовать"
        ),
        "revoked": "Отозвано в Telegram",
    }
    status = statuses[link.status]
    if link.status == "active" and link.expires_at and link.expires_at <= datetime.now(UTC):
        status = "Срок действия истёк"
    end = link.expires_at.strftime("%d.%m.%Y %H:%M UTC") if link.expires_at else "без ограничения"
    return (
        f"{link.name}\n{link.destination or 'Адрес ещё не подтверждён'}\n\nСтатус: {status}\n"
        f"Источник: {link.source}\nКампания: {link.campaign}\nСрок: {end}\n"
        f"Лимит одновременно вступивших: {link.member_limit or 'без ограничения'}\n\n"
        + join_summary(session, link)
    )


def build_link_router(engine: Engine, settings: Settings) -> Router:
    router = Router()

    def owner(session: Session, telegram_id: int) -> int:
        value = session.scalar(select(User.id).where(User.telegram_id == telegram_id))
        if value is None:
            raise ObjectUnavailable
        return value

    @router.callback_query(F.data.startswith("ln:history:") | F.data.startswith("ln:registry:"))
    async def listing(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        parts = (callback.data or "").split(":")
        data = await state.get_data()
        if (
            data.get("mode") != "expert"
            or not isinstance(callback.message, Message)
            or (len(parts) != 3 or not parts[2].isdecimal() or len(parts[2]) > 5)
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        page = int(parts[2])
        if page > 10000:
            await callback.answer("Экран устарел", show_alert=True)
            return

        def read() -> list[list[tuple[str, str]]]:
            with Session(engine) as session:
                user_id = owner(session, callback.from_user.id)
                kind = parts[1]
                model: Any = ToolResult if kind == "history" else TrackedLink
                query = select(model).where(model.owner_user_id == user_id, model.bot_id == bot.id)
                if kind == "history":
                    query = query.where(ToolResult.tool == "utm")
                values = list(
                    session.scalars(query.order_by(model.id.desc()).offset(page * 5).limit(6))
                )
                if kind == "registry":
                    values = search_links(
                        session, user_id, bot.id, data.get("link_query") or "", page
                    )
                rows = []
                for value in values[:5]:
                    if isinstance(value, ToolResult):
                        rows.append([(value.inputs["name"], f"ur:open:{value.id}")])
                    else:
                        rows.append([(value.name, f"lr:open:{value.id}")])
                navigation = []
                if page:
                    navigation.append(("←", f"ln:{kind}:{page - 1}"))
                if len(values) > 5:
                    navigation.append(("→", f"ln:{kind}:{page + 1}"))
                if navigation:
                    rows.append(navigation)
                if kind == "registry":
                    rows.append([("Поиск", "ln:search"), ("Сбросить поиск", "ln:clear")])
                return rows + [[("Ссылки", "section:links")], [("В меню", "menu")]]

        try:
            rows = await asyncio.to_thread(read)
        except ObjectUnavailable:
            await callback.answer("Недоступно", show_alert=True)
            return
        await state.update_data(paused=True, link_disable=None, link_restart=None)
        await edit(callback.message, f"Ссылки · страница {page + 1}", rows)
        await callback.answer()

    @router.callback_query((F.data == "ln:search") | (F.data == "ln:clear"))
    async def search(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        await state.update_data(link_disable=None, link_restart=None)
        if callback.data == "ln:clear":
            await state.set_state(None)
            await state.update_data(link_query=None)
            await listing(callback.model_copy(update={"data": "ln:registry:0"}), state, bot)
            return
        await state.set_state("link_search")
        await state.update_data(
            paused=False, active_tool="links", screen_message_id=callback.message.message_id
        )
        await edit(
            callback.message,
            "Что найти? Название, источник или кампания (до 100 символов)",
            [[("В меню", "menu")]],
        )
        await callback.answer()

    @router.message(StateFilter("link_search"), ~F.text.startswith("/"))
    async def search_input(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or data.get("paused"):
            return
        try:
            query = text_field(message.text or "", limit=100)
        except ValueError as error:
            await message.answer(str(error))
            return
        await state.set_state(None)
        await state.update_data(link_query=query, paused=True)
        await message.answer(
            f"Поиск: {query}",
            reply_markup=keyboard(
                [[("Показать результаты", "ln:registry:0")], [("В меню", "menu")]]
            ),
        )

    @router.callback_query(
        F.data.startswith("ln:")
        | F.data.startswith("lw:")
        | F.data.startswith("li:")
        | F.data.startswith("ur:save:")
    )
    async def retired(callback: CallbackQuery, state: FSMContext) -> None:
        # Retired buttons must not create new legacy objects or modify a current draft.
        await callback.answer("Этот инструмент убран. Откройте /utm", show_alert=True)

    @router.callback_query(F.data.startswith("ur:") | F.data.startswith("lr:"))
    async def result(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        parts = (callback.data or "").split(":")
        data = await state.get_data()
        if (
            data.get("mode") != "expert"
            or not isinstance(callback.message, Message)
            or (len(parts) != 3 or not parts[2].isdecimal())
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        screen_message_id = callback.message.message_id

        if parts[0] == "lr" and parts[1] == "confirm":
            if data.get("link_disable") != {"id": int(parts[2]), "message": screen_message_id}:
                await callback.answer("Экран устарел", show_alert=True)
                return

            def context() -> tuple[int, str]:
                with Session(engine) as session:
                    user_id = owner(session, callback.from_user.id)
                    return user_id, get_link(session, user_id, bot.id, int(parts[2])).kind

            try:
                user_id, kind = await asyncio.to_thread(context)
                if kind == "invite":
                    await revoke_invite(bot, engine, user_id, int(parts[2]))
            except (ObjectUnavailable, ValueError) as error:
                await callback.answer(str(error) or "Недоступно", show_alert=True)
                return

        def read() -> tuple[str, list[list[tuple[str, str]]], bytes | None]:
            with Session(engine) as session, session.begin():
                user_id = owner(session, callback.from_user.id)
                id = int(parts[2])
                if parts[0] == "ur" and parts[1] in {"open", "md", "csv"}:
                    value = get_result(session, user_id, bot.id, id)
                    if value.tool != "utm":
                        raise ObjectUnavailable
                    binary = (
                        utm_csv(value)
                        if parts[1] == "csv"
                        else value.result.encode()
                        if parts[1] == "md"
                        else None
                    )
                    return (
                        value.result[:3000],
                        [
                            [("CSV", f"ur:csv:{id}"), (".md", f"ur:md:{id}")],
                            [("История", "ln:history:0")],
                            [("В меню", "menu")],
                        ],
                        binary,
                    )
                if parts[0] != "lr" or parts[1] not in {"open", "disable", "confirm"}:
                    raise ObjectUnavailable
                link = get_link(session, user_id, bot.id, id)
                if parts[1] == "confirm":
                    pending = data.get("link_disable")
                    if pending != {"id": id, "message": screen_message_id}:
                        raise ObjectUnavailable
                    if link.kind != "invite":
                        set_enabled(session, user_id, bot.id, id, False)
                clicks, starts, unique = stats(session, id)
                text = (
                    f"{link.name}\n{link.destination}\n\nИсточник: {link.source}\n"
                    f"Кампания: {link.campaign}\n"
                    f"Статус: {'активна' if link.enabled else 'отключена'}\n"
                    f"Переходы через /go: {clicks}\nЗапуски: {starts}\n"
                    f"Уникальные запуски: {unique}\n\n"
                    "Прямые переходы по t.me не измеряются. Отключение этой ссылки "
                    "не изменяет постоянную ссылку материала."
                )
                if link.kind == "invite":
                    text = invite_card(session, link) + f"\n\nПереходы через /go: {clicks}"
                if link.external_url:
                    text = (
                        f"🔗 Короткая ссылка\n{link.external_url}\n\n"
                        f"Исходный адрес: {link.destination}\n\n"
                        "Статистика и управление ссылкой доступны в кабинете clck.su. "
                        "Локальный счётчик /go не учитывает переходы по этой ссылке."
                    )
                elif settings.public_base_url and link.destination:
                    text += f"\n\nС учётом переходов: {settings.public_base_url}/go/{link.code}"
                else:
                    text += "\nАдрес редиректа пока не настроен владельцем."
                rows = (
                    [[("Отключить", f"lr:disable:{id}")]]
                    if link.enabled and not link.external_url
                    else []
                )
                if link.external_url and parts[1] in {"disable", "confirm"}:
                    raise ValueError("Управление этой ссылкой доступно в кабинете clck.su")
                if link.kind == "invite":
                    rows = []
                    if link.status in {"active", "revoking", "revoke_failed", "revoke_uncertain"}:
                        rows.append([("Отозвать в Telegram", f"lr:disable:{id}")])
                if parts[1] == "disable":
                    text = "Отключить отслеживаемую ссылку? Её запуск и редирект станут недоступны."
                    if link.kind == "invite":
                        text = (
                            "Отозвать дополнительное приглашение в Telegram? "
                            "Основная ссылка канала не изменится."
                        )
                    rows = [[("Подтверждаю", f"lr:confirm:{id}")], [("Назад", f"lr:open:{id}")]]
                return text, rows + [[("Реестр", "ln:registry:0")], [("В меню", "menu")]], None

        try:
            text, rows, binary = await asyncio.to_thread(read)
        except ObjectUnavailable:
            await callback.answer("Объект недоступен или экран устарел", show_alert=True)
            return
        await state.update_data(
            paused=not (
                binary is not None
                and (await state.get_data()).get("active_tool") == "utm"
                and (await state.get_data()).get("utm_draft", {}).get("phase") == "ready"
            ),
            link_disable={"id": int(parts[2]), "message": callback.message.message_id}
            if parts[1] == "disable"
            else None,
        )
        if binary is not None:
            await bot.send_document(
                callback.from_user.id,
                BufferedInputFile(binary, filename=f"utm-{parts[2]}.{parts[1]}"),
            )
        else:
            await edit(callback.message, text, rows)
        await callback.answer()

    from leedbox.telegram.smart_links import build_smart_link_router

    router.include_router(build_smart_link_router(engine, settings))
    router.include_router(build_utm_router(engine, settings))
    return router
