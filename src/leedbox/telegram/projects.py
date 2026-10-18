"""Owner-scoped project selection and explicitly edited durable brief."""

import asyncio
from typing import Any
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.profile import update_profile
from leedbox.application.projects import (
    create_project,
    get_project,
    save_brief_answer,
    select_project,
    set_archived,
)
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.brief import BRIEF_FIELDS, missing_fields
from leedbox.infrastructure.models import Project, ProjectSelection, User
from leedbox.telegram.material_wizard import buttons
from leedbox.telegram.services import edit


def owner(session: Session, actor: int) -> int:
    id = session.scalar(
        select(User.id).where(User.telegram_id == actor, User.expert_started_at.is_not(None))
    )
    if id is None:
        raise ObjectUnavailable
    return id


def build_project_router(engine: Engine) -> Router:
    router = Router()

    async def card(message: Message, actor: int, id: int) -> None:
        def read() -> tuple[str, list[list[tuple[str, str]]]]:
            with Session(engine) as session:
                user_id = owner(session, actor)
                project = get_project(session, user_id, id)
                selected = session.get(ProjectSelection, user_id)
                active = bool(selected and selected.project_id == id and not project.archived)
                status = (
                    "Архив"
                    if project.archived
                    else "Выбран для новых мастеров"
                    if active
                    else "Активный проект"
                )
                completed = len(BRIEF_FIELDS) - len(missing_fields(project.brief, BRIEF_FIELDS))
                text = (
                    f"Проект: {project.name}\n{status}\nЧасовой пояс: {project.timezone}\n"
                    f"Контакт: {project.public_contact or 'не задан'}\n\n"
                    f"Бриф: {completed} из {len(BRIEF_FIELDS)} полей.\n"
                )
                text += "\n".join(
                    f"{BRIEF_FIELDS[key][0]}: {value[:120]}"
                    for key, value in project.brief.items()
                    if key in BRIEF_FIELDS and isinstance(value, str)
                )
                rows: list[list[tuple[str, str]]] = []
                if not project.archived:
                    if not active:
                        rows.append([("Выбрать проект", f"pr:select:{id}:{project.revision}")])
                    for field, label in (
                        ("name", "Название"),
                        ("contact", "Контакт автора"),
                        ("timezone", "Часовой пояс"),
                    ):
                        rows.append([(label, f"pr:edit:{id}:{project.revision}:{field}")])
                    rows += [
                        [(label, f"pr:edit:{id}:{project.revision}:{key}")]
                        for key, (label, _) in BRIEF_FIELDS.items()
                    ]
                rows.append(
                    [
                        (
                            "Восстановить" if project.archived else "Архивировать",
                            f"pr:archive:{id}:{project.revision}:{0 if project.archived else 1}",
                        )
                    ]
                )
                rows += [[("Назад", f"pr:list:{int(project.archived)}:0")], [("В меню", "menu")]]
                return text, rows

        text, rows = await asyncio.to_thread(read)
        await edit(message, text, rows)

    async def prompt(message: Message, state: FSMContext) -> None:
        draft = (await state.get_data())["profile_draft"]
        if draft["kind"] == "new":
            text = (
                "Введите название нового проекта (до 100 символов). "
                "Он станет выбранным для новых мастеров."
            )
        else:
            field = draft["field"]
            label, limit = (
                BRIEF_FIELDS[field]
                if field in BRIEF_FIELDS
                else (
                    {
                        "name": "Название",
                        "contact": "Telegram-имя автора",
                        "timezone": "Часовой пояс IANA",
                    }[field],
                    100,
                )
            )
            text = (
                f"{label}: до {limit} символов.\n"
                f"Текущее значение: {draft.get('current') or 'не задано'}"
            )
            if field == "links":
                text += "\nТолько HTTP/HTTPS, каждая ссылка с новой строки, до 10 ссылок."
            elif field == "contact":
                text += "\nTelegram username либо «-», чтобы удалить контакт."
            elif field == "timezone":
                text += "\nНапример Europe/Moscow."
            text += "\nСохраняется только ваш ответ; неизвестные факты не дополняются."
        rows = [
            [
                (
                    "Назад",
                    f"pr:card:{draft['project_id']}" if draft["kind"] == "edit" else "pr:list:0:0",
                )
            ],
            [("В меню", "menu")],
        ]
        if draft.get("field") in BRIEF_FIELDS:
            rows.insert(0, [("Очистить поле", "pr:clear:" + draft["token"])])
        await state.update_data(
            paused=False, active_tool="profile", screen_message_id=message.message_id
        )
        await state.set_state("profile_input")
        await edit(message, text, rows)

    @router.callback_query(F.data.startswith("pr:"))
    async def navigation(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Экран устарел. Откройте /start", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        await state.update_data(history_pending=None)
        if parts[1] != "confirm":
            await state.update_data(profile_pending=None)
        try:
            if parts[1] == "list" and len(parts) == 4:
                archived, page = int(parts[2]), int(parts[3])
                if archived not in {0, 1} or not 0 <= page <= 10000:
                    raise ValueError("Некорректная страница")

                def listing() -> list[list[tuple[str, str]]]:
                    with Session(engine) as session:
                        user_id = owner(session, callback.from_user.id)
                        entries = list(
                            session.scalars(
                                select(Project)
                                .where(
                                    Project.owner_user_id == user_id,
                                    Project.archived.is_(bool(archived)),
                                )
                                .order_by(Project.id)
                                .offset(page * 5)
                                .limit(6)
                            )
                        )
                        selection = session.get(ProjectSelection, user_id)
                        rows = [
                            [
                                (
                                    (
                                        "✓ "
                                        if selection
                                        and selection.project_id == row.id
                                        and not archived
                                        else ""
                                    )
                                    + row.name[:36],
                                    f"pr:card:{row.id}",
                                )
                            ]
                            for row in entries[:5]
                        ]
                        nav = []
                        if page:
                            nav.append(("←", f"pr:list:{archived}:{page - 1}"))
                        if len(entries) > 5:
                            nav.append(("→", f"pr:list:{archived}:{page + 1}"))
                        if nav:
                            rows.append(nav)
                        rows += [
                            [("Новый проект", "pr:new")],
                            [("Активные" if archived else "Архив", f"pr:list:{1 - archived}:0")],
                        ]
                        rows += [[("Назад", "section:profile")], [("В меню", "menu")]]
                        return rows

                rows = await asyncio.to_thread(listing)
                await state.update_data(paused=True)
                await edit(
                    callback.message,
                    "Архив проектов"
                    if archived
                    else (
                        "Проекты и общий бриф\nВыбранный проект используется в новых мастерах. "
                        "Старые черновики остаются в своём проекте."
                    ),
                    rows,
                )
            elif parts[1] == "card" and len(parts) == 3:
                await state.update_data(paused=True)
                await card(callback.message, callback.from_user.id, int(parts[2]))
            elif parts[1] == "new" and len(parts) == 2:
                if (data.get("profile_draft") or {}).get("kind") == "new":
                    await prompt(callback.message, state)
                    await callback.answer()
                    return
                await state.update_data(
                    profile_draft={
                        "kind": "new",
                        "action_key": uuid4().hex,
                        "token": uuid4().hex[:12],
                    }
                )
                await prompt(callback.message, state)
            elif parts[1] == "resume" and len(parts) == 2:
                draft = data.get("profile_draft")
                if not draft:
                    raise ValueError("Нет незавершённого заполнения")
                draft["token"] = uuid4().hex[:12]
                await state.update_data(profile_draft=draft)
                await prompt(callback.message, state)
            elif parts[1] == "edit" and len(parts) == 5:
                id, revision, field = int(parts[2]), int(parts[3]), parts[4]
                if field not in BRIEF_FIELDS and field not in {"name", "contact", "timezone"}:
                    raise ValueError("Неизвестное поле")

                def current() -> str:
                    with Session(engine) as session:
                        project = get_project(session, owner(session, callback.from_user.id), id)
                        if project.archived or project.revision != revision:
                            raise ValueError("Проект изменился. Откройте его заново")
                        return (
                            str(project.brief.get(field, ""))
                            if field in BRIEF_FIELDS
                            else str(
                                getattr(project, "public_contact" if field == "contact" else field)
                                or ""
                            )
                        )

                value = await asyncio.to_thread(current)
                await state.update_data(
                    profile_draft={
                        "kind": "edit",
                        "project_id": id,
                        "revision": revision,
                        "field": field,
                        "current": value,
                        "token": uuid4().hex[:12],
                    }
                )
                await prompt(callback.message, state)
            elif parts[1] == "select" and len(parts) == 4:

                def choose() -> None:
                    with Session(engine) as session, session.begin():
                        select_project(
                            session,
                            owner(session, callback.from_user.id),
                            int(parts[2]),
                            int(parts[3]),
                        )

                await asyncio.to_thread(choose)
                await card(callback.message, callback.from_user.id, int(parts[2]))
            elif parts[1] == "archive" and len(parts) == 5 and parts[4] in {"0", "1"}:

                def validate_archive() -> None:
                    with Session(engine) as session:
                        project = get_project(
                            session, owner(session, callback.from_user.id), int(parts[2])
                        )
                        if project.revision != int(parts[3]):
                            raise ValueError("Проект изменился")

                await asyncio.to_thread(validate_archive)
                token = uuid4().hex[:12]
                await state.update_data(
                    profile_pending={
                        "id": int(parts[2]),
                        "revision": int(parts[3]),
                        "archived": parts[4] == "1",
                        "token": token,
                        "message_id": callback.message.message_id,
                    },
                    paused=True,
                )
                await edit(
                    callback.message,
                    "Подтвердите "
                    + ("архивирование" if parts[4] == "1" else "восстановление")
                    + " проекта. Материалы и история не удаляются. "
                    "Если активных проектов не останется, следующий новый мастер "
                    "создаст «Мой проект».",
                    [[("Подтвердить", "pr:confirm:" + token)], [("Отмена", "pr:card:" + parts[2])]],
                )
            elif parts[1] == "confirm" and len(parts) == 3:
                pending = data.get("profile_pending")
                if (
                    not pending
                    or pending["token"] != parts[2]
                    or pending["message_id"] != callback.message.message_id
                ):
                    raise ValueError("Подтверждение устарело")

                def archive() -> None:
                    with Session(engine) as session, session.begin():
                        set_archived(
                            session,
                            owner(session, callback.from_user.id),
                            pending["id"],
                            pending["revision"],
                            pending["archived"],
                        )

                await asyncio.to_thread(archive)
                await state.update_data(profile_pending=None)
                await card(callback.message, callback.from_user.id, pending["id"])
            elif parts[1] == "clear" and len(parts) == 3:
                draft = data.get("profile_draft")
                if (
                    not draft
                    or draft["token"] != parts[2]
                    or draft.get("field") not in BRIEF_FIELDS
                ):
                    raise ValueError("Экран устарел")
                await save(callback.from_user.id, draft, None)
                await state.set_state(None)
                await state.update_data(profile_draft=None, paused=True)
                await card(callback.message, callback.from_user.id, draft["project_id"])
            else:
                raise ValueError("Экран устарел")
        except (ValueError, ObjectUnavailable, OverflowError):
            await callback.answer(
                "Проект недоступен или изменился. Откройте «Проекты и бриф» заново", show_alert=True
            )
            return
        await callback.answer()

    async def save(actor: int, draft: dict[str, Any], answer: str | None) -> int:
        def write() -> int:
            with Session(engine) as session, session.begin():
                user_id = owner(session, actor)
                if draft["kind"] == "new":
                    project = create_project(
                        session, user_id, answer or "", action_key=draft["action_key"]
                    )
                    select_project(session, user_id, project.id, project.revision)
                    return project.id
                if draft["field"] in BRIEF_FIELDS:
                    save_brief_answer(
                        session,
                        user_id,
                        draft["project_id"],
                        draft["revision"],
                        draft["field"],
                        answer,
                    )
                else:
                    update_profile(
                        session,
                        user_id,
                        draft["project_id"],
                        draft["field"],
                        answer or "",
                        draft["revision"],
                    )
                return int(draft["project_id"])

        return await asyncio.to_thread(write)

    @router.message(StateFilter("profile_input"), ~F.text.startswith("/"))
    async def answer(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        draft = data.get("profile_draft")
        if data.get("mode") != "expert" or data.get("paused") or not draft or not message.from_user:
            return
        try:
            id = await save(message.from_user.id, draft, message.text or "")
        except (ValueError, ObjectUnavailable) as error:
            await message.answer(
                str(error) if isinstance(error, ValueError) else "Проект недоступен"
            )
            return
        await state.set_state(None)
        await state.update_data(profile_draft=None, paused=True)
        if message.bot:
            await message.bot.edit_message_text(
                "Сохранено. Поля других проектов не изменились.",
                chat_id=message.chat.id,
                message_id=data["screen_message_id"],
                reply_markup=buttons([[("Открыть проект", f"pr:card:{id}")], [("В меню", "menu")]]),
            )

    return router
