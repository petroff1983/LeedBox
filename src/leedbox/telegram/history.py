"""Unified, owner-scoped history. Reading and exports do not require paid access."""

import asyncio
import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    Message,
)
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application.entitlements import require_access
from leedbox.application.links import utm_csv
from leedbox.application.projects import ensure_project, get_project
from leedbox.application.results import delete_result, get_result, history_page
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.links import csv_export
from leedbox.telegram.calculators import EDITABLE_VERSIONS, LABELS, SCHEMAS
from leedbox.telegram.content_tools import LABELS as CONTENT_LABELS
from leedbox.telegram.content_tools import QUESTIONS
from leedbox.telegram.material_ui import next_message
from leedbox.telegram.projects import owner
from leedbox.telegram.services import edit


def build_history_router(engine: Engine) -> Router:
    router = Router()

    async def listing(message: Message, actor: int, bot_id: int, scope: int, page: int) -> None:
        def read() -> tuple[str, list[list[tuple[str, str]]]]:
            with Session(engine) as session, session.begin():
                user_id = owner(session, actor)
                project = ensure_project(session, user_id) if scope == 1 else None
                values = history_page(
                    session, user_id, bot_id, project.id if project else None, page
                )
                text = "История: " + (project.name if project else "все проекты")
                if not values:
                    text += "\nНа этой странице нет результатов."
                rows = [
                    [
                        (
                            f"{LABELS.get(row.tool, CONTENT_LABELS.get(row.tool, row.tool))} "
                            f"· {row.created_at:%d.%m}",
                            f"hr:open:{row.id}",
                        )
                    ]
                    for row in values[:5]
                ]
                nav = []
                if page:
                    nav.append(("←", f"hr:list:{scope}:{page - 1}"))
                if len(values) > 5:
                    nav.append(("→", f"hr:list:{scope}:{page + 1}"))
                if nav:
                    rows.append(nav)
                rows += [
                    [("Все проекты" if scope else "Выбранный проект", f"hr:list:{1 - scope}:0")],
                    [("Назад", "section:profile")],
                    [("В меню", "menu")],
                ]
                return text, rows

        text, rows = await asyncio.to_thread(read)
        await edit(message, text, rows)

    @router.callback_query(F.data.startswith("hr:"))
    async def navigation(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Экран устарел. Откройте /start", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        await state.update_data(idea_material=None)
        await state.update_data(profile_pending=None)
        if parts[1] != "confirm":
            await state.update_data(history_pending=None)
        try:
            if len(parts) == 4 and parts[1] == "list":
                scope, page = int(parts[2]), int(parts[3])
                if scope not in {0, 1}:
                    raise ObjectUnavailable
                if page < 0 or page > 100000:
                    raise ObjectUnavailable
                await state.update_data(history_location={"scope": scope, "page": page})
                await state.update_data(paused=True)
                await listing(callback.message, callback.from_user.id, bot.id, scope, page)
                await callback.answer()
                return
            if len(parts) != 3 or parts[1] not in {
                "open",
                "md",
                "csv",
                "delete",
                "edit",
                "confirm",
            }:
                raise ObjectUnavailable
            command = parts[1]
            pending = data.get("history_pending")
            if command == "confirm":
                if (
                    not pending
                    or pending["token"] != parts[2]
                    or pending["message_id"] != callback.message.message_id
                ):
                    raise ObjectUnavailable
                id = int(pending["id"])
            else:
                id = int(parts[2])

            def read() -> dict[str, Any]:
                with Session(engine) as session:
                    user_id = owner(session, callback.from_user.id)
                    value = get_result(session, user_id, bot.id, id)
                    project = get_project(session, user_id, value.project_id)
                    return {
                        "id": value.id,
                        "project_id": project.id,
                        "project": project.name,
                        "archived": project.archived,
                        "tool": value.tool,
                        "version": value.template_version,
                        "inputs": deepcopy(value.inputs),
                        "result": value.result,
                    }

            value = await asyncio.to_thread(read)
            if command in {"delete", "edit"}:
                if command == "edit" and (
                    value["archived"]
                    or value["tool"] == "sequence"
                    and value["version"].startswith("prompt-")
                    or not (
                        value["tool"] in SCHEMAS
                        and value["version"] in EDITABLE_VERSIONS
                        or value["tool"] in QUESTIONS
                        and value["version"].startswith(("content-", "prompt-"))
                    )
                ):
                    raise ValueError("Для этого результата повторный мастер пока недоступен")
                token = uuid4().hex[:12]
                await state.update_data(
                    history_pending={
                        "token": token,
                        "id": id,
                        "action": command,
                        "message_id": callback.message.message_id,
                    },
                    paused=True,
                )
                text = (
                    "Удалить только этот результат? Материалы, ссылки и шаблоны сохранятся."
                    if command == "delete"
                    else "Создать новый результат на основе сохранённых параметров? "
                    "Старый результат не изменится. Незавершённый мастер этого инструмента "
                    "будет заменён после подтверждения."
                )
                rows = [[("Подтвердить", "hr:confirm:" + token)], [("Назад", f"hr:open:{id}")]]
                if (
                    command == "edit"
                    and value["tool"] in SCHEMAS
                    and value["version"] in EDITABLE_VERSIONS
                ):
                    from leedbox.telegram.library import keyboard

                    sent = await next_message(callback.message, text, reply_markup=keyboard(rows))
                    await state.update_data(
                        history_pending={
                            "token": token,
                            "id": id,
                            "action": command,
                            "message_id": sent.message_id,
                        }
                    )
                else:
                    await edit(callback.message, text, rows)
            elif command == "confirm":
                assert pending is not None
                if pending["action"] == "delete":

                    def remove() -> None:
                        with Session(engine) as session, session.begin():
                            delete_result(
                                session, owner(session, callback.from_user.id), bot.id, id
                            )

                    await asyncio.to_thread(remove)
                    await state.update_data(history_pending=None, paused=True)
                    await listing(callback.message, callback.from_user.id, bot.id, 0, 0)
                else:

                    def access() -> None:
                        with Session(engine) as session:
                            require_access(session, owner(session, callback.from_user.id))

                    await asyncio.to_thread(access)
                    if value["archived"]:
                        raise ValueError("Сначала восстановите проект из архива")
                    inputs = value["inputs"]
                    if value["tool"] in SCHEMAS and value["version"] in EDITABLE_VERSIONS:
                        revision = (
                            max(
                                int(data.get("calc_revision", 0)),
                                int((data.get("calc_draft") or {}).get("revision", 0)),
                            )
                            + 1
                        )
                        await state.update_data(
                            calc_draft={
                                "step": 0,
                                "revision": revision,
                                "tool": value["tool"],
                                "values": inputs["values"],
                                "factors": inputs.get("factors", []),
                                "token": uuid4().hex,
                                "project_id": value["project_id"],
                            },
                            active_tool="calc",
                        )
                        resume = "calc:resume"
                    elif value["tool"] in QUESTIONS and value["version"].startswith(
                        ("content-", "prompt-")
                    ):
                        revision = (
                            max(
                                int(data.get("text_revision", 0)),
                                int((data.get("text_draft") or {}).get("revision", 0)),
                            )
                            + 1
                        )
                        restored = deepcopy(inputs)
                        restored.update(
                            step=0,
                            fields=list(
                                (
                                    "product",
                                    "audience",
                                    "desired_result",
                                    "problem",
                                    "timeframe",
                                    "price_conditions",
                                    "evidence",
                                )
                                if value["tool"] == "offer"
                                and inputs.get("offer_schema") != 2
                                and inputs.get("prompt_schema") != 1
                                else QUESTIONS[value["tool"]]
                            ),
                            revision=revision,
                            token=uuid4().hex,
                        )
                        for key in (
                            "result_id",
                            "exported_id",
                            "brief_pending",
                            "suggest_pending",
                            "topics",
                            "approved",
                            "multi",
                            "offer_detail",
                            "offer_edit",
                            "offer_brief_saved",
                            "offer_parts",
                        ):
                            restored.pop(key, None)
                        await state.update_data(text_draft=restored, active_tool="texts")
                        resume = "ct:resume"
                    else:
                        raise ObjectUnavailable
                    await state.set_state(None)
                    await state.update_data(
                        history_pending=None,
                        paused=True,
                        screen_message_id=callback.message.message_id,
                    )
                    text = (
                        "Параметры восстановлены в исходном проекте. "
                        "Их можно оставить или изменить на каждом шаге."
                    )
                    destination = (
                        "calc:new:" + value["tool"]
                        if resume == "calc:resume"
                        else "ct:new:"
                        + (
                            "proposal-" + restored["values"].get("proposal_kind", "service")
                            if value["tool"] == "proposal"
                            else value["tool"]
                        )
                        if resume == "ct:resume"
                        else "section:links"
                    )
                    rows = [[("✏️ Открыть инструмент", destination)], [("В меню", "menu")]]
                    if resume == "calc:resume" or value["version"].startswith("prompt-"):
                        from leedbox.telegram.library import keyboard

                        await next_message(callback.message, text, reply_markup=keyboard(rows))
                    else:
                        await edit(callback.message, text, rows)
            elif command in {"md", "csv"}:

                def export() -> bytes:
                    with Session(engine) as session:
                        row = get_result(session, owner(session, callback.from_user.id), bot.id, id)
                        if command == "md":
                            return row.result.encode()
                        if row.tool == "utm":
                            return utm_csv(row)
                        return csv_export(
                            [
                                ["tool", "project", "version", "inputs", "result"],
                                [
                                    row.tool,
                                    value["project"],
                                    row.template_version,
                                    json.dumps(row.inputs, ensure_ascii=False),
                                    row.result,
                                ],
                            ]
                        )

                binary = await asyncio.to_thread(export)
                await bot.send_document(
                    callback.from_user.id,
                    BufferedInputFile(binary, filename=f"result-{id}.{command}"),
                )
            else:
                await state.update_data(paused=True, history_pending=None)
                rows = [
                    [(".md", f"hr:md:{id}"), ("CSV", f"hr:csv:{id}")],
                    [("Изменить параметры", f"hr:edit:{id}")],
                    [("Удалить результат", f"hr:delete:{id}")],
                    [
                        (
                            "← К списку результатов",
                            f"hr:list:{data.get('history_location', {}).get('scope', 0)}:"
                            f"{data.get('history_location', {}).get('page', 0)}",
                        )
                    ],
                ]
                if value["tool"] == "utm" or (
                    value["tool"] == "sequence" and value["version"].startswith("prompt-")
                ):
                    rows.pop(1)
                text = f"{value['project']} · {value['version']}\n\n" + value["result"]
                if len(text) > 3500:
                    text = text[:3400] + "\n\nПолный результат — в файле .md."
                if value["tool"] in SCHEMAS and value["version"] in EDITABLE_VERSIONS:
                    from leedbox.telegram.library import keyboard

                    await next_message(callback.message, text, reply_markup=keyboard(rows))
                else:
                    await edit(callback.message, text, rows)
                if 1 <= len(value["result"]) <= 256:
                    from leedbox.telegram.material_wizard import buttons

                    markup = buttons(rows)
                    markup.inline_keyboard.insert(
                        0,
                        [
                            InlineKeyboardButton(
                                text="Копировать", copy_text=CopyTextButton(text=value["result"])
                            )
                        ],
                    )
                    await callback.message.edit_reply_markup(reply_markup=markup)
            await callback.answer()
        except (ObjectUnavailable, ValueError):
            await callback.answer(
                "Результат недоступен или действие устарело. "
                "Для нового расчёта проверьте «Мой доступ».",
                show_alert=True,
            )

    return router
