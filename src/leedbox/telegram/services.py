"""Expert-only catalog and durable request wizard; never used in recipient mode."""

import asyncio
from html import escape, unescape
from typing import Any
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.projects import ensure_project
from leedbox.application.services import (
    catalog,
    change_status,
    consent_document,
    get_request,
    owner_request,
    request_card,
    selected_services,
    service_snapshot,
    submit_request,
    withdraw_consent,
)
from leedbox.application.settings import setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.services import (
    STATUS_LABELS,
    owner_contact_url,
    request_contact,
    service_text,
    storefront_url,
)
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import Service, ServiceRequest, User
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.texts import ui_text


def menu_rows() -> list[list[tuple[str, str]]]:
    return [
        [("🤖 Услуги", "sn:list:0")],
        [("🛒 Товары", "store:products")],
        [("⬅️ Назад", "menu")],
    ]


def card_text(item: Service) -> str:
    return (
        f"{item.title}\n{item.price_label} · {item.duration}\n\n"
        f"Что получите:\n{item.description}\n\n{item.conditions}"
    )


def service_pages(items: list[Service], intro: str) -> list[str]:
    """One readable catalog, paginated only when Telegram's text limit requires it."""
    if not items:
        return ["🤖 Услуги пока не опубликованы. Попробуйте зайти позже."]
    pages: list[str] = []
    body = escape(intro)
    separator = "\n\n━━━━━━━━━━━━━━━━━━\n\n"
    for item in items:
        block = (
            f"<b>{escape(item.title)}</b>\n"
            f"{escape(item.price_label)} · {escape(item.duration)}\n\n"
            f"Что получите:\n{escape(item.description)}\n\n{escape(item.conditions)}"
        )
        if len(unescape(body + separator + block)) > 3500:
            if body:
                pages.append(body)
            body = block
        else:
            body += separator + block
    if body:
        pages.append(body)
    return pages


async def show(
    message: Message,
    text: str,
    rows: list[list[tuple[str, str]]],
    url: str | None = None,
    contact: bool = False,
) -> None:
    markup = keyboard(rows)
    if url:
        markup.inline_keyboard.insert(
            0, [InlineKeyboardButton(text="💬 Связаться", url=url, style="success")]
        )
    elif contact:
        markup.inline_keyboard.insert(
            0,
            [
                InlineKeyboardButton(
                    text="💬 Связаться", callback_data="sn:contact", style="success"
                )
            ],
        )
    await next_message(message, text, reply_markup=markup)


def owner_id(session: Session, telegram_id: int) -> int:
    value = session.scalar(
        select(User.id).where(User.telegram_id == telegram_id, User.expert_started_at.is_not(None))
    )
    if value is None:
        raise ObjectUnavailable
    return value


async def edit(
    message: Message,
    text: str,
    rows: list[list[tuple[str, str]]],
    url: str | None = None,
    url_label: str = "Написать лично",
) -> None:
    markup = keyboard(rows)
    if url:
        markup.inline_keyboard.insert(0, [InlineKeyboardButton(text=url_label, url=url)])
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode=None)
    except TelegramBadRequest as error:
        if "message is not modified" not in error.message.lower():
            raise


def build_service_router(engine: Engine, config: Settings) -> Router:
    router = Router()

    async def render(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        draft = data["service_draft"]
        revision, step = draft["revision"], draft["step"]

        def code(action: str) -> str:
            return f"sf:{revision}:{action}"

        rows: list[list[tuple[str, str]]] = []
        if draft.get("view_item"):
            item = draft["view_item"]
            text = (
                f"{item['title']}\n\nДля кого: {item['audience']}\n"
                f"Что входит: {item['description']}\nСрок: {item['duration']}\n"
                f"Цена: {item['price_label']}\n\nИсполнитель: {item['executor_details']}\n"
                f"Условия: {item['conditions']}\n\nОплата вне бота."
            )
            await state.update_data(screen_message_id=message.message_id)
            await edit(
                message, text, [[("Назад к заявке", code("details_close"))], [("В меню", "menu")]]
            )
            return
        if step == 0:

            def read() -> list[tuple[int, str]]:
                with Session(engine) as session:
                    return [(row.id, row.title) for row in catalog(session, draft["page"])]

            entries = await asyncio.to_thread(read)
            text = "Выберите услуги (до 10). После выбора нажмите «Далее»."
            if not entries:
                text += "\nНа этой странице опубликованных услуг нет."
            for id, title in entries[:5]:
                mark = "☑" if id in draft["ids"] else "☐"
                rows.append(
                    [
                        (f"{mark} {title[:28]}", code(f"toggle:{id}")),
                        ("Подробнее", code(f"details:{id}")),
                    ]
                )
            nav = []
            if draft["page"]:
                nav.append(("←", code(f"page:{draft['page'] - 1}")))
            if len(entries) > 5:
                nav.append(("→", code(f"page:{draft['page'] + 1}")))
            if nav:
                rows.append(nav)
            rows.append([("Далее", code("next"))])
        elif step == 1:
            text = await asyncio.to_thread(ui_text, engine, "service_contact")
        elif step == 2:
            text = await asyncio.to_thread(ui_text, engine, "service_comment")
            rows.append([("Пропустить", code("skip"))])
        elif step == 3:
            names = "\n".join(
                f"• {item['title']} — {item['price_label']}" for item in draft["items"]
            )
            text = (
                f"Проверьте заявку\n\n{names}\n\nКонтакт: {draft['contact']}\n"
                f"Комментарий: {draft.get('comment') or '—'}\n\n"
                "Оплата вне бота, после согласования."
            )
            rows.append([("К согласию", code("next"))])
            rows += [
                [(f"Условия: {item['title'][:24]}", code(f"details:{item['id']}"))]
                for item in draft["items"]
            ]
        else:
            text = (
                f"Согласие на передачу данных\n\n{draft['consent_text']}\n\n"
                "Без согласия заявка не отправляется."
            )
            rows.append([("Согласен, отправить", code("submit"))])
            rows.append([("Не согласен", code("cancel"))])
        rows += [[("Назад", code("back"))], [("В меню", "menu")]]
        await state.update_data(screen_message_id=message.message_id)
        await edit(message, text, rows)

    @router.callback_query(F.data.startswith("sn:"))
    async def navigation(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Экран устарел. Откройте /start", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) < 2:
                raise ValueError("Экран устарел")
            if parts[1] in {"contact", "about"} and len(parts) == 2:
                await callback.answer(
                    "Ссылка пока не настроена. Попробуйте позже.", show_alert=True
                )
                return
            if parts[1] in {"new", "resume"}:
                draft = data.get("service_draft")
                if not draft:
                    if parts[1] == "resume":
                        raise ValueError("Нет незавершённой заявки")

                    def context() -> int:
                        with Session(engine) as session, session.begin():
                            id = owner_id(session, callback.from_user.id)
                            project = ensure_project(session, id)
                            consent_document(session, config)
                            return project.id

                    project_id = await asyncio.to_thread(context)
                    draft = {
                        "token": uuid4().hex,
                        "revision": 1,
                        "step": 0,
                        "page": 0,
                        "ids": [],
                        "project_id": project_id,
                    }
                else:
                    draft["revision"] += 1
                await state.update_data(
                    service_draft=draft, active_tool="services", paused=False, service_withdraw=None
                )
                await state.set_state("service_request")
                await render(callback.message, state)
            elif len(parts) == 3 and parts[2].isdecimal() and len(parts[2]) <= 16:
                id = int(parts[2])
                if parts[1] == "list":

                    def listing() -> tuple[
                        str, list[list[tuple[str, str]]], str | None, str | None
                    ]:
                        with Session(engine) as session:
                            entries = (
                                list(
                                    session.scalars(
                                        select(Service)
                                        .where(Service.enabled.is_(True))
                                        .order_by(Service.sort_order, Service.id)
                                    )
                                )
                                if setting(session, "services_enabled")
                                else []
                            )
                            pages = service_pages(entries, ui_text(engine, "service_catalog"))
                            if id >= len(pages):
                                raise ValueError("Страница устарела. Откройте услуги заново")
                            rows: list[list[tuple[str, str]]] = []
                            nav = []
                            if id:
                                nav.append(("←", f"sn:list:{id - 1}"))
                            if id + 1 < len(pages):
                                nav.append(("→", f"sn:list:{id + 1}"))
                            if nav:
                                rows.append(nav)
                            rows.append([("⬅️ Назад", "section:services")])
                            contact_url = storefront_url(
                                str(setting(session, "service_contact_url")), contact=True
                            ) or owner_contact_url(str(setting(session, "owner_contact")))
                            about_url = storefront_url(str(setting(session, "service_about_url")))
                            return (
                                pages[id],
                                rows,
                                contact_url,
                                about_url,
                            )

                    text, rows, contact_url, about_url = await asyncio.to_thread(listing)
                    await state.update_data(paused=True)
                    markup = keyboard(rows)
                    markup.inline_keyboard.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                text="💬 Связаться", url=contact_url, style="success"
                            )
                            if contact_url
                            else InlineKeyboardButton(
                                text="💬 Связаться", callback_data="sn:contact", style="success"
                            )
                        ],
                    )
                    markup.inline_keyboard.insert(
                        1,
                        [
                            InlineKeyboardButton(text="ℹ️ Обо мне", url=about_url)
                            if about_url
                            else InlineKeyboardButton(text="ℹ️ Обо мне", callback_data="sn:about")
                        ],
                    )
                    await next_message(
                        callback.message,
                        text,
                        reply_markup=markup,
                        parse_mode="HTML",
                    )
                elif parts[1] == "card":

                    def card() -> tuple[str, str | None]:
                        with Session(engine) as session:
                            row = session.get(Service, id)
                            if (
                                not row
                                or not row.enabled
                                or not setting(session, "services_enabled")
                            ):
                                raise ObjectUnavailable
                            return (
                                card_text(row) + f"\n\nИсполнитель: {row.executor_details}",
                                storefront_url(
                                    str(setting(session, "service_contact_url")), contact=True
                                )
                                or owner_contact_url(str(setting(session, "owner_contact"))),
                            )

                    text, url = await asyncio.to_thread(card)
                    await state.update_data(paused=True)
                    await show(
                        callback.message,
                        text,
                        [
                            [("Назад", "sn:list:0")],
                        ],
                        url,
                        contact=True,
                    )
                elif parts[1] in {"requests", "request", "withdraw"}:

                    def history() -> tuple[str, list[list[tuple[str, str]]]]:
                        with Session(engine) as session:
                            own = owner_id(session, callback.from_user.id)
                            if parts[1] in {"request", "withdraw"}:
                                row = get_request(session, bot.id, id, own)
                                if parts[1] == "withdraw":
                                    return (
                                        "Отозвать согласие на обработку этой заявки? "
                                        "Ожидающие уведомления будут отменены. "
                                        "Уже переданные владельцу сведения "
                                        "отозвать задним числом нельзя.",
                                        [
                                            [("Подтвердить отзыв", f"sw:confirm:{id}")],
                                            [("Отмена", f"sn:request:{id}")],
                                        ],
                                    )
                                rows = []
                                if row.consent_revoked_at is None:
                                    rows.append([("Отозвать согласие", f"sn:withdraw:{id}")])
                                return request_card(row), rows + [
                                    [("Назад", "sn:requests:0")],
                                    [("В меню", "menu")],
                                ]
                            if id > 10000:
                                raise ValueError("Некорректная страница")
                            records = list(
                                session.scalars(
                                    select(ServiceRequest)
                                    .where(
                                        ServiceRequest.owner_user_id == own,
                                        ServiceRequest.bot_id == bot.id,
                                    )
                                    .order_by(ServiceRequest.id.desc())
                                    .offset(id * 5)
                                    .limit(6)
                                )
                            )
                            rows = [
                                [
                                    (
                                        f"№{row.id} · {STATUS_LABELS[row.status]}",
                                        f"sn:request:{row.id}",
                                    )
                                ]
                                for row in records[:5]
                            ]
                            nav = []
                            if id:
                                nav.append(("←", f"sn:requests:{id - 1}"))
                            if len(records) > 5:
                                nav.append(("→", f"sn:requests:{id + 1}"))
                            if nav:
                                rows.append(nav)
                            return "Мои заявки" + (
                                "\nПока заявок нет." if not records else ""
                            ), rows + [[("Назад", "section:services")], [("В меню", "menu")]]

                    text, rows = await asyncio.to_thread(history)
                    await state.update_data(
                        paused=True,
                        service_withdraw=(
                            {"id": id, "message_id": callback.message.message_id}
                            if parts[1] == "withdraw"
                            else None
                        ),
                    )
                    await edit(callback.message, text, rows)
                else:
                    raise ValueError("Экран устарел")
            else:
                raise ValueError("Экран устарел")
        except (ValueError, ObjectUnavailable) as error:
            await callback.answer(str(error) or "Объект недоступен", show_alert=True)
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("sf:"))
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("service_draft")
        parts = (callback.data or "").split(":")
        if (
            data.get("mode") != "expert"
            or data.get("paused")
            or not draft
            or len(parts) < 3
            or parts[1] != str(draft["revision"])
            or not isinstance(callback.message, Message)
            or callback.message.message_id != data.get("screen_message_id")
        ):
            await callback.answer("Экран устарел. Откройте услуги заново", show_alert=True)
            return
        try:
            command = parts[2]
            if (
                command == "details"
                and len(parts) == 4
                and parts[3].isdecimal()
                and len(parts[3]) <= 16
                and draft["step"] in {0, 3}
            ):
                id = int(parts[3])

                def details() -> dict[str, Any]:
                    with Session(engine) as session:
                        if draft["step"] == 3:
                            for item in draft["items"]:
                                if item["id"] == id:
                                    return dict(item)
                            raise ObjectUnavailable
                        return service_snapshot(selected_services(session, [id])[0])

                draft["view_item"] = await asyncio.to_thread(details)
                draft["revision"] += 1
                await state.update_data(service_draft=draft)
                await render(callback.message, state)
                await callback.answer()
                return
            if command == "details_close" and draft.get("view_item"):
                draft.pop("view_item")
                draft["revision"] += 1
                await state.update_data(service_draft=draft)
                await render(callback.message, state)
                await callback.answer()
                return
            if draft.get("view_item"):
                raise ValueError("Вернитесь к заявке")
            if command == "cancel" or (command == "back" and draft["step"] == 0):
                await state.update_data(service_draft=None, paused=True)
                await state.set_state(None)
                await edit(callback.message, "Заявка не отправлена", menu_rows())
                await callback.answer()
                return
            if (
                command in {"toggle", "page"}
                and len(parts) == 4
                and parts[3].isdecimal()
                and len(parts[3]) <= 16
                and draft["step"] == 0
            ):
                id = int(parts[3])
                if command == "page":
                    if id > 10000:
                        raise ValueError("Некорректная страница")
                    draft["page"] = id
                elif id in draft["ids"]:
                    draft["ids"].remove(id)
                else:
                    if len(draft["ids"]) >= 10:
                        raise ValueError("Можно выбрать не более десяти услуг")
                    draft["ids"].append(id)
            elif command == "back":
                draft["step"] = max(0, draft["step"] - 1)
            elif command == "skip" and draft["step"] == 2:
                draft["comment"], draft["step"] = "", 3
            elif command == "next" and draft["step"] == 0:

                def preview() -> tuple[list[dict[str, Any]], str, str]:
                    with Session(engine) as session:
                        rows = selected_services(session, draft["ids"])
                        version, text = consent_document(session, config)
                        return [service_snapshot(row) for row in rows], version, text

                items, version, text = await asyncio.to_thread(preview)
                draft.update(
                    items=items,
                    revisions={str(item["id"]): item["revision"] for item in items},
                    consent_version=version,
                    consent_text=text,
                    step=1,
                )
            elif command == "next" and draft["step"] == 3:
                draft["step"] = 4
            elif command == "submit" and draft["step"] == 4:

                def submit() -> tuple[int, str]:
                    with Session(engine) as session, session.begin():
                        row = submit_request(
                            session,
                            config,
                            owner_id(session, callback.from_user.id),
                            draft["project_id"],
                            bot.id,
                            draft["ids"],
                            draft["revisions"],
                            draft["contact"],
                            draft.get("comment", ""),
                            True,
                            draft["consent_version"],
                            f"services:{draft['token']}",
                        )
                        return row.id, request_card(row)

                id, text = await asyncio.to_thread(submit)
                await state.update_data(service_draft=None, paused=True)
                await state.set_state(None)
                await edit(
                    callback.message,
                    "Заявка сохранена\n\n" + text,
                    [[("Мои заявки", "sn:requests:0")], [("В меню", "menu")]],
                )
                await callback.answer()
                return
            else:
                raise ValueError("Экран устарел")
            draft["revision"] += 1
            await state.update_data(service_draft=draft)
            await render(callback.message, state)
        except (ValueError, ObjectUnavailable) as error:
            await callback.answer(str(error) or "Объект недоступен", show_alert=True)
            return
        await callback.answer()

    @router.message(StateFilter("service_request"), ~F.text.startswith("/"))
    async def answer(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("service_draft")
        if not message.from_user or data.get("mode") != "expert" or data.get("paused") or not draft:
            return
        try:
            if draft["step"] == 1:
                draft["contact"] = request_contact(message.text or "")
            elif draft["step"] == 2:
                draft["comment"] = service_text(message.text or "", 1000)
            else:
                return
        except ValueError as error:
            await message.answer(str(error))
            return
        draft["step"] += 1
        draft["revision"] += 1
        await state.update_data(service_draft=draft)
        screen = Message(
            message_id=data["screen_message_id"], date=message.date, chat=message.chat
        ).as_(bot)
        await render(screen, state)

    @router.callback_query(F.data.startswith("ows:"))
    async def owner_actions(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (
            not config.is_owner(callback.from_user.id)
            or (await state.get_data()).get("mode") != "expert"
            or not isinstance(callback.message, Message)
        ):
            await callback.answer("Доступ закрыт", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        try:

            def read() -> tuple[str, list[list[tuple[str, str]]], str | None]:
                with Session(engine) as session, session.begin():
                    if len(parts) == 3 and parts[1] == "list" and parts[2].isdecimal():
                        page = int(parts[2])
                        if page > 10000:
                            raise ValueError("Некорректная страница")
                        records = list(
                            session.scalars(
                                select(ServiceRequest)
                                .where(ServiceRequest.bot_id == bot.id)
                                .order_by(ServiceRequest.id.desc())
                                .offset(page * 5)
                                .limit(6)
                            )
                        )
                        rows = [
                            [(f"№{row.id} · {STATUS_LABELS[row.status]}", f"ows:open:{row.id}")]
                            for row in records[:5]
                        ]
                        if page:
                            rows.append([("←", f"ows:list:{page - 1}")])
                        if len(records) > 5:
                            rows.append([("→", f"ows:list:{page + 1}")])
                        return (
                            "Заявки на услуги" + ("\nПока заявок нет." if not records else ""),
                            rows + [[("Назад", "owner:summary")]],
                            None,
                        )
                    if len(parts) < 3 or not parts[2].isdecimal() or len(parts[2]) > 16:
                        raise ValueError("Экран устарел")
                    id = int(parts[2])
                    row = owner_request(session, config, callback.from_user.id, bot.id, id)
                    if (
                        parts[1] in {"change", "confirm"}
                        and len(parts) == 5
                        and parts[3].isdecimal()
                        and parts[4] in {"working", "closed"}
                    ):
                        revision, target = int(parts[3]), parts[4]
                        if row.revision != revision:
                            raise ValueError("Заявка изменилась. Откройте карточку заново")
                        if parts[1] == "change":
                            return (
                                f"Изменить статус заявки №{id} на «{STATUS_LABELS[target]}»?",
                                [
                                    [("Подтвердить", f"ows:confirm:{id}:{revision}:{target}")],
                                    [("Отмена", f"ows:open:{id}")],
                                ],
                                None,
                            )
                        row = change_status(
                            session, config, callback.from_user.id, bot.id, id, revision, target
                        )
                    elif parts[1] != "open" or len(parts) != 3:
                        raise ValueError("Экран устарел")
                    rows = []
                    if row.status == "new":
                        rows.append([("Взять в работу", f"ows:change:{id}:{row.revision}:working")])
                    if row.status != "closed":
                        rows.append([("Закрыть", f"ows:change:{id}:{row.revision}:closed")])
                    rows += [[("Заявки", "ows:list:0")], [("В меню", "menu")]]
                    if row.consent_revoked_at is not None:
                        return (
                            f"Заявка №{row.id} · {STATUS_LABELS[row.status]}\n"
                            "Согласие отозвано. Контакт и комментарий больше не отображаются.",
                            rows,
                            None,
                        )
                    url = owner_contact_url(row.contact) if row.contact.startswith("@") else None
                    return request_card(row), rows, url

            text, rows, url = await asyncio.to_thread(read)
            await state.update_data(paused=True)
            await edit(callback.message, text, rows, url, "Ответить клиенту")
        except (ValueError, ObjectUnavailable) as error:
            await callback.answer(str(error) or "Объект недоступен", show_alert=True)
            return
        await callback.answer()

    @router.callback_query(F.data.startswith("sw:confirm:"))
    async def withdraw(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        pending = data.get("service_withdraw")
        if (
            data.get("mode") != "expert"
            or not pending
            or not isinstance(callback.message, Message)
            or callback.data != f"sw:confirm:{pending['id']}"
            or callback.message.message_id != pending["message_id"]
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return

        def save() -> None:
            with Session(engine) as session, session.begin():
                withdraw_consent(
                    session, bot.id, pending["id"], owner_id(session, callback.from_user.id)
                )

        try:
            await asyncio.to_thread(save)
        except ObjectUnavailable:
            await callback.answer("Заявка недоступна", show_alert=True)
            return
        await state.update_data(service_withdraw=None)
        await edit(callback.message, "Согласие отозвано", menu_rows())
        await callback.answer()

    return router
