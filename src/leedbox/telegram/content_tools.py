"""Durable text masters, frozen catalogs and explicit shared-brief updates."""

import asyncio
from copy import deepcopy
from html import escape
from typing import Any
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    LinkPreviewOptions,
    Message,
)
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leedbox.application.content import catalog_version, snapshot, suggest_niche, tool_catalog
from leedbox.application.projects import ensure_project, get_project, save_brief_answer
from leedbox.application.prompt_templates import current_template
from leedbox.application.results import get_result, save_result
from leedbox.application.settings import product_event, setting
from leedbox.domain import ai_prompts, funnel, offers
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.brief import BRIEF_FIELDS, validate_answer
from leedbox.domain.commercial_texts import commercial_proposal, package_case
from leedbox.domain.content import landing, offer, text
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import Channel, FunnelTopic, Niche, Objection, ToolResult
from leedbox.telegram.material_ui import next_message, retire
from leedbox.telegram.material_wizard import buttons
from leedbox.telegram.projects import owner
from leedbox.telegram.services import edit

QUESTIONS = {
    "sequence": funnel.FIELDS,
    "ideas": ("niche", "audience", "goal", "format"),
    **ai_prompts.FIELDS,
    "proposal": (
        "problem",
        "product",
        "scope",
        "process",
        "price_conditions",
        "conditions",
        "action",
    ),
}
PROMPTS = {
    **{key: label for key, (label, _) in BRIEF_FIELDS.items()},
    "goal": "Цель материала",
    "format": "Формат (необязательно)",
    "timeframe": "Реальный срок результата, если известен",
    "placement": "Где использовать оффер",
    "page_type": "Тип страницы",
    "price_band": "Диапазон цены",
    "temperature": "Температура аудитории",
    "action": "Целевое действие на странице",
    "task": "Задача клиента (без персональных данных)",
    "baseline": "Исходные данные кейса",
    "work": "Что сделали — фактические шаги",
    "result_number": "Результат — число и единица измерения из ваших данных",
    "testimonial": "Реальный отзыв клиента (можно пропустить)",
    "scope": "Состав работ / комплектация и границы",
    "process": "Этапы и реальные сроки",
    "conditions": "Условия предложения",
    **funnel.LABELS,
}
MULTI_CHOICES = {
    "product_format": {
        "course": "Курс",
        "consultation": "Консультация",
        "service": "Услуга",
        "product": "Продукт",
    },
    "sales_channel": {
        "telegram": "Telegram",
        "website": "Сайт",
        "messages": "Личные сообщения",
        "call": "Созвон",
    },
    "motivation": {
        "time": "Сэкономить время",
        "understand": "Разобраться",
        "task": "Решить задачу",
        "risk": "Снизить риски",
    },
}
CHOICES = {
    "goal": {"subscribers": "Подписчики", "requests": "Заявки", "warming": "Прогрев"},
    "format": {
        "checklist": "Чек-лист",
        "guide": "Гайд",
        "template": "Шаблон",
        "video": "Видео",
        "table": "Таблица",
    },
    "placement": {"page": "Страница", "post": "Пост", "ad": "Объявление", "button": "Кнопка"},
    "page_type": {
        "service": "Услуга",
        "consultation": "Консультация",
        "course": "Курс",
        "webinar": "Вебинар",
        "product": "Продукт",
        "channel": "Канал",
    },
    "price_band": {"low": "До 5 000 ₽", "medium": "5–50 000 ₽", "high": "Дороже 50 000 ₽"},
    "temperature": {"cold": "Холодная", "warm": "Тёплая"},
}
DEFAULTS = {
    "goal": "subscribers",
    "placement": "page",
    "page_type": "service",
    "price_band": "medium",
    "temperature": "warm",
}
LABELS = {
    "ca": "Анализ ЦА",
    "content_strategy": "Контент-стратегия",
    "sequence": "Конструктор воронки",
    "ideas": "Идеи лид-магнитов",
    "offer": "УТП",
    "landing": "Структура лендинга",
    "case": "Структура кейса",
    "proposal": "Коммерческое предложение",
}


def render_result(draft: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    values, catalog, tool = draft["values"], draft["catalog"], draft["tool"]
    if tool == "sequence":
        return funnel.result(draft), []
    if tool == "case":
        return "\n\n---\n\n".join(
            package_case(values, placement, catalog[0]) for placement in ("post", "page")
        ), []
    if tool == "proposal":
        kind = values.get("proposal_kind", "service")
        template = next((row for row in catalog if row["payload"]["model"] == kind), None)
        if template is None:
            raise ValueError("Для выбранного типа КП нет опубликованного шаблона")
        return commercial_proposal(values, kind, template), []
    if tool == "offer":
        if draft.get("offer_schema") == 2:
            return offers.report(values, catalog), []
        return offer(values, catalog[:5], values["placement"], draft["limits"]), []
    if tool == "landing":
        result = landing(values, catalog[0])
        return result + "\n\nОффер (черновик):\n" + offer(
            values, draft["offers"][:5], "page", {}
        ), []
    niche = values.get("niche")
    known = {item["slug"] for item in draft["niches"]}
    pool = [
        item
        for item in catalog
        if item["payload"]["niche"] in ({niche, None} if niche in known else {None})
        and values.get("goal") in item["payload"]["goals"]
        and (not values.get("format") or values["format"] == item["payload"]["format"])
    ]
    pool.sort(key=lambda item: item["payload"]["niche"] is None)
    fallback = False
    if values.get("format") and len(pool) < 5:
        additional = [
            item
            for item in catalog
            if item not in pool
            and item["payload"]["niche"] is None
            and values.get("goal") in item["payload"]["goals"]
        ]
        pool += additional
        fallback = bool(additional)
    if not pool:
        return "Для выбранного формата идей пока нет. Измените формат или выберите другой.", []
    offset = int(draft.get("offset", 0)) % len(pool)
    selected = (pool[offset:] + pool[:offset])[: min(5, len(pool))]
    parts = [
        "Идеи из каталога; это структура, содержимое заполняет автор.",
        "Аудитория из ваших данных: «" + str(values.get("audience") or "[впишите]") + "».",
    ]
    if fallback:
        parts += [
            "Совпадений по формату мало: добавлены универсальные идеи других форматов. "
            "Формат каждой идеи указан рядом с названием."
        ]
    if niche not in known:
        parts += [
            "Для неизвестной ниши показаны универсальные форматы; "
            "свободная генерация не используется."
        ]
    for index, item in enumerate(selected, 1):
        data = item["payload"]
        parts += [
            f"\n{index}. {item['title']} · {CHOICES['format'][data['format']]}\n"
            + ("Универсальная идея\n" if data["niche"] is None else "Нишевая идея\n")
            + "\n".join("• " + line for line in data["structure"])
            + "\nАвтор заполняет: "
            + "; ".join(data["fields"])
        ]
    return "\n".join(parts), selected


def prompt_menu_rows() -> list[list[tuple[str, str]]]:
    return [
        [("🎯 Анализ ЦА", "ct:new:ca"), ("📅 Контент-стратегия", "ct:new:content_strategy")],
        [("🎯 УТП", "ct:new:offer"), ("🧱 Лендинг", "ct:new:landing")],
        [("📖 Кейс", "ct:new:case"), ("🔥 Воронка", "ct:new:sequence")],
        [("💬 Ответы на возражения", "ob:list:0")],
        [("🏠 В главное меню", "menu")],
    ]


def build_content_router(engine: Engine, config: Settings | None = None) -> Router:
    router = Router()

    async def render_prompt(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data["text_draft"]
        prefix = f"tw:{draft['revision']}:"
        rows: list[list[tuple[str, str]]] = []
        if draft["step"] < len(draft["fields"]):
            field = draft["fields"][draft["step"]]
            body = (
                f"✍️ {ai_prompts.LABELS[draft['tool']]} · вопрос "
                f"{draft['step'] + 1}/{len(draft['fields'])}\n\n" + ai_prompts.QUESTIONS[field]
            )
            if draft["step"] == 0:
                body = "Соберём промпт для вашей нейросети. Отвечайте своими словами.\n\n" + body
            if draft["values"].get(field):
                previous_answer = str(draft["values"][field])
                body += "\n\nВаш ответ: " + previous_answer[:500]
                if len(previous_answer) > 500:
                    body += (
                        "…\nПоказан фрагмент; полный ответ сохранён. "
                        "Можно оставить его кнопкой ниже."
                    )
                rows.append([("Оставить ответ", prefix + "keep")])
            if field in ai_prompts.OPTIONAL:
                rows.append(
                    [
                        (
                            "Нет доказательств"
                            if field in {"evidence", "proof_difference"}
                            else "Пропустить",
                            prefix + "skip",
                        )
                    ]
                )
        else:
            template = draft.setdefault(
                "prompt_template", ai_prompts.default_template(draft["tool"])
            )
            prompt = ai_prompts.build_prompt(draft["tool"], draft["values"], template)

            def save() -> tuple[int, str]:
                with Session(engine) as session, session.begin():
                    result = save_result(
                        session,
                        owner(session, message.chat.id),
                        draft["project_id"],
                        bot.id,
                        draft["tool"],
                        deepcopy(draft),
                        prompt,
                        f"text:{draft['token']}:{draft['revision']}",
                        ai_prompts.version(draft["tool"], template),
                    )
                    return result.id, result.result

            result_id, prompt = await asyncio.to_thread(save)
            draft["result_id"] = result_id
            if draft.get("exported_id") != result_id:
                await bot.send_document(
                    message.chat.id,
                    BufferedInputFile(prompt.encode(), filename=f"prompt-{result_id}.md"),
                    caption="Готовый промпт: скопируйте текст из файла в свою нейросеть.",
                )
                draft["exported_id"] = result_id
            body = (
                f"✅ Промпт «{ai_prompts.LABELS[draft['tool']]}» готов!\n\n"
                "Откройте .md-файл и скопируйте его содержимое в ChatGPT или другую нейросеть. "
                "Можно также посмотреть промпт ниже.\n\n"
                "Бот подготовил задание с вашими вводными; ответ создаст выбранная вами нейросеть."
            )
            rows += [
                [("📄 Посмотреть промпт", f"ap:{result_id}:0")],
                [("✏️ Изменить вводные", f"hr:edit:{result_id}")],
                [("История / экспорт", f"hr:open:{result_id}")],
                [("Создать новый", "ct:restart:" + draft["tool"])],
            ]
            if draft["tool"] == "ca":
                body += (
                    "\n\n📅 Для контент-плана получите анализ в ИИ и нажмите кнопку ниже. "
                    "Ваши вводные перенесутся автоматически; "
                    "ответ ИИ можно будет добавить отдельно."
                )
                rows.insert(0, [("📅 Контент-план на основе ЦА", f"cs:from:{result_id}")])
        if draft["step"]:
            rows.append([("← Предыдущий вопрос", prefix + "back")])
        rows.append([("🏠 В меню", "menu")])
        await retire(bot, message.chat.id, data.get("screen_message_id"))
        sent = await message.answer(body, reply_markup=buttons(rows))
        await state.update_data(
            text_draft=draft,
            active_tool="texts",
            paused=False,
            screen_message_id=sent.message_id,
        )
        await state.set_state("text_input" if draft["step"] < len(draft["fields"]) else None)
        if config and draft["step"] == len(draft["fields"]) and draft.get("result_id"):
            from leedbox.telegram.monetization import recommend

            await recommend(engine, config, bot, message.chat.id, state, draft["tool"])

    @router.callback_query(
        F.data.startswith("cs:from:") | F.data.startswith("cs:replace:"),
        flags={"requires_access": True},
    )
    async def strategy_from_ca(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Откройте /start для инструментов", show_alert=True)
            return
        try:
            result_id = int((callback.data or "").split(":")[2])
            previous = data.get("text_draft") or {}
            if previous and previous.get("step", 0) < len(previous.get("fields", [])):
                if previous.get("source_ca_result_id") == result_id:
                    await render_prompt(callback.message, state, bot)
                    await callback.answer()
                    return
                if (callback.data or "").startswith("cs:from:"):
                    await next_message(
                        callback.message,
                        "Есть незавершённый опрос. Начать контент-стратегию "
                        "с выбранным анализом ЦА? "
                        "Ответы текущего незавершённого опроса будут заменены; "
                        "сохранённые результаты останутся в истории.",
                        reply_markup=buttons(
                            [
                                [("Создать контент-стратегию", f"cs:replace:{result_id}")],
                                [("Вернуться к опросу", "ct:resume")],
                            ]
                        ),
                    )
                    await callback.answer()
                    return

            def context() -> dict[str, Any]:
                with Session(engine) as db, db.begin():
                    user_id = owner(db, callback.from_user.id)
                    result = get_result(db, user_id, bot.id, result_id)
                    if result.tool != "ca" or not result.template_version.startswith("prompt-"):
                        raise ObjectUnavailable
                    get_project(db, user_id, result.project_id)
                    product_event(
                        db, bot.id, callback.from_user.id, "tool_started", tool="content_strategy"
                    )
                    return {
                        "tool": "content_strategy",
                        "source_ca_result_id": result_id,
                        "prompt_schema": 1,
                        "prompt_template": current_template(db, "content_strategy"),
                        "fields": list(ai_prompts.FIELDS["content_strategy"]),
                        "step": 3,
                        "values": {
                            field: result.inputs["values"].get(field)
                            for field in ai_prompts.FIELDS["ca"]
                        },
                        "project_id": result.project_id,
                        "revision": int((data.get("text_draft") or {}).get("revision", 0)) + 1,
                        "token": uuid4().hex,
                    }

            await state.update_data(text_draft=await asyncio.to_thread(context))
            await render_prompt(callback.message, state, bot)
            await callback.answer()
        except (ValueError, ObjectUnavailable):
            await callback.answer(
                "Анализ недоступен. Откройте его из своей истории.", show_alert=True
            )

    @router.callback_query(F.data.startswith("ap:"))
    async def prompt_preview(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Промпт недоступен", show_alert=True)
            return
        try:
            parts = (callback.data or "").split(":")
            if len(parts) != 3:
                raise ValueError
            result_id, page = int(parts[1]), int(parts[2])

            def read() -> str:
                with Session(engine) as session:
                    result = get_result(
                        session, owner(session, callback.from_user.id), bot.id, result_id
                    )
                    if not result.template_version.startswith("prompt-"):
                        raise ObjectUnavailable
                    return result.result

            prompt = await asyncio.to_thread(read)
            pages = (len(prompt) + 1599) // 1600
            if not 0 <= page < pages:
                raise ValueError
            rows = []
            nav = []
            if page:
                nav.append(("←", f"ap:{result_id}:{page - 1}"))
            if page + 1 < pages:
                nav.append(("→", f"ap:{result_id}:{page + 1}"))
            if nav:
                rows.append(nav)
            rows += [[("📎 Полный .md", f"hr:md:{result_id}")], [("В меню", "menu")]]
            await bot.send_message(
                callback.message.chat.id,
                f"Промпт · часть {page + 1}/{pages}\n"
                "Скопируйте все части по порядку или текст из .md.\n\n<pre>"
                + escape(prompt[page * 1600 : (page + 1) * 1600])
                + "</pre>",
                parse_mode="HTML",
                reply_markup=buttons(rows),
            )
            await retire(bot, callback.message.chat.id, callback.message.message_id)
            await callback.answer()
        except (ValueError, ObjectUnavailable):
            await callback.answer("Промпт недоступен или экран устарел", show_alert=True)

    async def render(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data["text_draft"]
        if draft["tool"] in ai_prompts.FIELDS and draft.get("prompt_schema") != 1:
            # Preserve historical answers, but never resume retired text generators.
            draft = {
                "tool": draft["tool"],
                "prompt_schema": 1,
                "fields": list(ai_prompts.FIELDS[draft["tool"]]),
                "step": 0,
                "values": deepcopy(draft["values"]),
                "project_id": draft["project_id"],
                "revision": int(draft["revision"]) + 1,
                "token": uuid4().hex,
            }
            await state.update_data(text_draft=draft)
        if draft.get("prompt_schema") == 1:
            await render_prompt(message, state, bot)
            return
        offer_v2 = draft["tool"] == "offer" and draft.get("offer_schema") == 2
        draft.pop("suggest_pending", None)
        await state.update_data(idea_material=None)
        prefix = f"tw:{draft['revision']}:"
        rows: list[list[tuple[str, str]]] = []
        pending = draft.get("brief_pending")
        if pending:
            body = (
                "Ответ сохранён в этом мастере. Сохранить его также в общем брифе проекта?\n"
                + pending["answer"]
            )
            rows += [
                [("Да, в бриф", prefix + "brief:save"), ("Только здесь", prefix + "brief:skip")]
            ]
        elif draft["step"] < len(draft["fields"]):
            field = draft["fields"][draft["step"]]
            body = (
                f"{LABELS[draft['tool']]} · {draft['step'] + 1}/{len(draft['fields'])}\n\n"
                f"{offers.PROMPTS[field] if offer_v2 else PROMPTS[field]}"
            )
            if offer_v2:
                block = (
                    "Продукт и аудитория"
                    if field in offers.FIELDS[:4]
                    else "Ценность и отличие"
                    if field in offers.FIELDS[4:8]
                    else "Условия и действие"
                )
                body = (
                    f"✍️ Оффер и УТП · {draft['step'] + 1}/{len(draft['fields'])}\n"
                    f"{block}\n\n"
                    + offers.DETAILS.get(draft.get("offer_detail", ""), offers.PROMPTS[field])
                )
                if draft["step"] == 0:
                    body = "Соберём три варианта оффера и ваше УТП из реальных фактов.\n\n" + body
            if draft["values"].get(field):
                body += "\nСохранено: " + str(draft["values"][field])
                rows.append([("Оставить", prefix + "keep")])
            choices = CHOICES.get(field, {})
            if draft["tool"] == "sequence" and (field in MULTI_CHOICES or field == "objections"):
                choices = MULTI_CHOICES.get(field, {})
                if field == "objections":
                    choices = {str(row["id"]): row["title"] for row in draft["objection_pool"]}
                selected_codes = draft.get("multi", {}).get(field, [])
                rows += [
                    [(("✓ " if code in selected_codes else "") + label, prefix + "toggle:" + code)]
                    for code, label in choices.items()
                ]
                rows.append([("Готово", prefix + "multi_done")])
                choices = {}
                body += "\nМожно выбрать несколько или написать своими словами."
            if field == "niche":
                choices = {str(item["id"]): item["title"] for item in draft["niches"]}
                body += "\nМожно написать другую нишу своими словами."
            if field == "audience" and not offer_v2:
                choices = {"a0": "Начинающие", "a1": "Практикующие", "a2": "Заказчики"}
                body += "\nМожно написать своими словами."
            if offer_v2:
                choices = {}
                if not draft.get("offer_detail"):
                    rows += [
                        [(label, prefix + "offer_choice:" + code)]
                        for code, (label, _) in offers.CHOICES.get(field, {}).items()
                    ]
                    if field in offers.CHOICES:
                        rows.append([("✍️ Свой вариант", prefix + "offer_custom")])
            rows += [[(label, prefix + "choose:" + code)] for code, label in choices.items()]
            if not offer_v2 or field not in offers.REQUIRED:
                rows.append([("Пропустить", prefix + "skip")])
            if draft["tool"] == "sequence":
                rows.append([("Результат из заполненного", prefix + "finish")])
        else:
            if draft["tool"] == "sequence" and "topics" not in draft:
                draft["topics"] = funnel.select_topics(draft["values"], draft["topic_pool"])
                # Only catalog titles, never user prose, become objection topic headings.
                selected_objections = [
                    row
                    for row in draft["objection_pool"]
                    if row["title"] in str(draft["values"].get("objections") or "").split("; ")
                ][:3]
                closing = [row for row in draft["topics"] if row["payload"]["stage"] == "closing"]
                for topic, objection in zip(closing, selected_objections, strict=False):
                    topic["title"] = "Разобрать возражение: " + objection["title"]
                closing[-1]["title"] = "Финальный призыв: подтвердить условия и следующий шаг"
            body, selected = render_result(draft)
            if offer_v2:
                draft["offer_parts"] = offers.variants(draft["values"], draft["catalog"])
            if not selected and draft["tool"] == "ideas":
                rows.append([("Изменить формат", prefix + "back")])
            else:

                def save() -> int:
                    with Session(engine) as session, session.begin():
                        return save_result(
                            session,
                            owner(session, message.chat.id),
                            draft["project_id"],
                            bot.id,
                            draft["tool"],
                            deepcopy(draft),
                            body,
                            f"text:{draft['token']}:{draft['revision']}",
                            catalog_version(
                                draft["catalog"]
                                + draft.get("offers", [])
                                + draft.get("topic_pool", [])
                                + draft.get("objection_pool", [])
                            ),
                        ).id

                result_id = await asyncio.to_thread(save)
                draft["result_id"] = result_id
                rows.append([("История / экспорт", f"hr:open:{result_id}")])
                if draft["tool"] == "sequence":
                    rows += [
                        [("Один / три промта", prefix + "split")],
                        [("Утвердить темы", prefix + "approve")],
                    ]
                    if not draft.get("approved"):
                        rows += [
                            [
                                (f"Заменить {index + 1}", prefix + f"replace:{index}"),
                                (f"Убрать {index + 1}", prefix + f"remove:{index}"),
                            ]
                            for index in range(len(draft["topics"]))
                        ]
                    rows.append([("Заказать посты у нас", "sn:list:0")])
                    if draft.get("approved"):
                        rows.append([("Копировать промт по частям", prefix + "prompt_page:0")])
                if draft["tool"] == "offer":
                    if offer_v2:
                        rows += [
                            [("⚡ Сделать прямее", prefix + "offer_direct")],
                            [("🎯 Уточнить результат и цифры", prefix + "offer_numbers")],
                        ]
                        rows += [
                            [(f"📋 Копировать вариант {i + 1}", f"oc:{result_id}:{i}:0")]
                            for i in range(3)
                        ]
                        if not draft.get("offer_brief_saved"):
                            rows.append([("💾 Сохранить основу в бриф", prefix + "offer_brief")])
                    else:
                        rows += [
                            [(label, prefix + "placement:" + code)]
                            for code, label in CHOICES["placement"].items()
                            if code != draft["values"]["placement"]
                        ]
                if draft["tool"] == "proposal":
                    rows += [
                        [(label, prefix + "proposal_kind:" + code)]
                        for code, label in (
                            ("service", "Услуга"),
                            ("consultation", "Консультация"),
                            ("product", "Продукт"),
                        )
                        if code != draft["values"].get("proposal_kind", "service")
                        and any(row["payload"]["model"] == code for row in draft["catalog"])
                    ]
                if selected:
                    rows.append([("Показать другие", prefix + "more")])
                    rows += [
                        [(f"Материал по идее {index + 1}", f"ci:prepare:{result_id}:{index}")]
                        for index in range(len(selected))
                    ]
                    if draft["values"].get("niche") not in {row["slug"] for row in draft["niches"]}:
                        rows.append([("Предложить нишу", prefix + "suggest")])
                if (
                    len(body) > 3500 or draft["tool"] in {"case", "proposal", "sequence"}
                ) and draft.get("exported_id") != result_id:
                    await bot.send_document(
                        message.chat.id,
                        BufferedInputFile(
                            body.encode(), filename=f"{draft['tool']}-{result_id}.md"
                        ),
                    )
                    draft["exported_id"] = result_id
        rows += [[("Назад", prefix + "back")], [("В меню", "menu")]]
        if offer_v2:
            if (
                not message.from_user
                or not message.from_user.is_bot
                or message.message_id != data.get("screen_message_id")
            ):
                await retire(bot, message.chat.id, data.get("screen_message_id"))
            sent = await next_message(
                message,
                body if len(body) <= 3500 else body[:3300] + "\n\nПолный результат — в .md.",
                reply_markup=buttons(rows),
            )
            screen_id = sent.message_id
        else:
            screen_id = message.message_id
        await state.update_data(
            text_draft=draft,
            active_tool="texts",
            paused=False,
            screen_message_id=screen_id,
        )
        await state.set_state(
            "text_input" if draft["step"] < len(draft["fields"]) and not pending else None
        )
        if not offer_v2:
            await edit(
                message,
                body if len(body) <= 3500 else body[:3300] + "\n\nПолный результат — в .md.",
                rows,
            )
        if config and draft["step"] == len(draft["fields"]) and draft.get("result_id"):
            from leedbox.telegram.monetization import recommend

            await recommend(engine, config, bot, message.chat.id, state, draft["tool"])

    @router.message(Command("offer", "utp"))
    async def shortcut(message: Message, state: FSMContext) -> None:
        if (await state.get_data()).get("mode") != "expert":
            await message.answer("Для инструментов откройте /start без ссылки материала")
            return
        await message.answer(
            "УТП · готовый промпт для вашей нейросети",
            reply_markup=buttons([[("Начать", "ct:new:offer")], [("В меню", "menu")]]),
        )

    @router.callback_query(
        F.data.in_({"ct:new:sequence", "ct:restart:sequence", "ct:example"}),
        flags={"requires_access": True},
    )
    async def sequence_prompt(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Для инструментов откройте /start", show_alert=True)
            return

        def save() -> tuple[int, str]:
            with Session(engine) as session, session.begin():
                user_id = owner(session, callback.from_user.id)
                project = ensure_project(session, user_id)
                action_key = f"sequence-prompt:{bot.id}:{callback.from_user.id}:{callback.id}"
                previous = session.scalar(
                    select(ToolResult).where(ToolResult.action_key == action_key)
                )
                if previous:
                    previous = get_result(session, user_id, bot.id, previous.id)
                    return previous.id, previous.result
                product_event(
                    session, bot.id, callback.from_user.id, "tool_started", tool="sequence"
                )
                template = current_template(session, "sequence")
                result = save_result(
                    session,
                    user_id,
                    project.id,
                    bot.id,
                    "sequence",
                    {
                        "tool": "sequence",
                        "prompt_schema": 1,
                        "values": {},
                        "fields": [],
                        "step": 0,
                        "prompt_template": template,
                    },
                    ai_prompts.build_prompt("sequence", {}, template),
                    action_key,
                    ai_prompts.version("sequence", template),
                )
                return result.id, result.result

        try:
            result_id, prompt = await asyncio.to_thread(save)
            await bot.send_document(
                callback.message.chat.id,
                BufferedInputFile(prompt.encode(), filename=f"voronka-{result_id}.md"),
                caption="Промпт для воронки из 16 постов. Скопируйте текст в свою нейросеть.",
            )
            await next_message(
                callback.message,
                "✅ Промпт для воронки готов!\n\n"
                "Вставьте его в свою нейросеть. Она начнёт с анкеты, затем поможет согласовать "
                "структуру и написать 16 постов по одному.\n\n"
                "В боте отвечать на вопросы не нужно. "
                "Ваш незавершённый опрос другого инструмента сохранён.",
                reply_markup=buttons(
                    [
                        [("📄 Посмотреть промпт", f"ap:{result_id}:0")],
                        [("История / экспорт", f"hr:open:{result_id}")],
                        [("🏠 В меню", "menu")],
                    ]
                ),
            )
            await state.update_data(paused=True)
            if config:
                from leedbox.telegram.monetization import recommend

                await recommend(engine, config, bot, callback.from_user.id, state, "sequence")
            await callback.answer()
        except (ValueError, ObjectUnavailable) as error:
            await callback.answer(str(error) or "Проект недоступен", show_alert=True)

    @router.callback_query(F.data.startswith("ct:"), flags={"requires_access": True})
    async def start(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        parts = (callback.data or "").split(":")
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Экран устарел", show_alert=True)
            return
        if callback.data == "ct:example":

            def example() -> str:
                with Session(engine) as session:
                    entries = tool_catalog(session, "funnel_example")
                    if not entries:
                        return "Учебный пример пока не опубликован"
                    entry = entries[0]
                    return (
                        str(entry["title"])
                        + "\nДемонстрационные данные, не реальный оффер.\n\n"
                        + "\n\n".join(
                            funnel.LABELS[key] + ": " + entry["payload"]["facts"][key]
                            for key in funnel.FIELDS
                        )
                    )

            await state.update_data(paused=True)
            await edit(
                callback.message,
                await asyncio.to_thread(example),
                [[("Начать свою воронку", "ct:new:sequence")], [("Назад", "section:texts")]],
            )
            await callback.answer()
            return
        if callback.data == "ct:proposal_types":
            await state.update_data(paused=True, idea_material=None)
            await edit(
                callback.message,
                "Тип коммерческого предложения",
                [
                    [("Услуга", "ct:new:proposal-service")],
                    [("Консультация", "ct:new:proposal-consultation")],
                    [("Продукт", "ct:new:proposal-product")],
                    [("Назад", "section:texts")],
                ],
            )
            await callback.answer()
            return
        if callback.data != "ct:resume":
            if (
                len(parts) != 3
                or parts[1] not in {"new", "restart"}
                or (
                    parts[2] not in QUESTIONS
                    and parts[2]
                    not in {"proposal-service", "proposal-consultation", "proposal-product"}
                )
            ):
                await callback.answer("Экран устарел", show_alert=True)
                return
            previous = data.get("text_draft")
            if parts[1] == "new" and previous and previous["step"] < len(previous["fields"]):
                expected = "proposal" if parts[2].startswith("proposal-") else parts[2]
                same = previous["tool"] == expected and (
                    expected != "proposal"
                    or previous["values"].get("proposal_kind") == parts[2].removeprefix("proposal-")
                )
                if same and (
                    expected not in ai_prompts.FIELDS or previous.get("prompt_schema") == 1
                ):
                    await render(callback.message, state, bot)
                    await callback.answer()
                    return
                if not (same and expected in ai_prompts.FIELDS):
                    await state.update_data(paused=True)
                    choice = await next_message(
                        callback.message,
                        f"📝 Сохранён незавершённый опрос «{LABELS[previous['tool']]}».\n\n"
                        "Вернитесь к нему с сохранёнными ответами или начните выбранный "
                        "инструмент заново. При новом старте прежний опрос будет заменён.",
                        reply_markup=buttons(
                            [
                                [("↩️ Вернуться к опросу", "ct:resume")],
                                [("Начать заново", "ct:restart:" + parts[2])],
                                [("В меню", "menu")],
                            ]
                        ),
                    )
                    await state.update_data(screen_message_id=choice.message_id)
                    await callback.answer()
                    return

            def context() -> dict[str, Any]:
                with Session(engine) as session, session.begin():
                    user_id = owner(session, callback.from_user.id)
                    project = ensure_project(session, user_id)
                    tool = "proposal" if parts[2].startswith("proposal-") else parts[2]
                    if tool in ai_prompts.FIELDS:
                        values = deepcopy(project.brief)
                        previous = data.get("text_draft") or {}
                        if parts[1] == "new" and previous.get("tool") == tool:
                            values.update(previous.get("values", {}))
                        # Only existing brief facts are reused; no invented proof or CTA.
                        product_event(
                            session, bot.id, callback.from_user.id, "tool_started", tool=tool
                        )
                        return {
                            "tool": tool,
                            "prompt_schema": 1,
                            "prompt_template": current_template(session, tool),
                            "fields": list(ai_prompts.FIELDS[tool]),
                            "step": 0,
                            "values": values,
                            "project_id": project.id,
                            "revision": int((data.get("text_draft") or {}).get("revision", 0)) + 1,
                            "token": uuid4().hex,
                        }
                    catalog = tool_catalog(session, tool)
                    if tool == "offer":
                        catalog = [row for row in catalog if row["slug"].startswith("offer-v2-")]
                    offers = tool_catalog(session, "offer") if tool == "landing" else []
                    if (
                        not catalog
                        or (tool == "landing" and len(offers) < 3)
                        or (tool == "offer" and len(catalog) < 3)
                    ):
                        raise ValueError("Каталог пока не опубликован")
                    niches = [
                        snapshot(row)
                        for row in session.scalars(
                            select(Niche).where(Niche.enabled.is_(True)).order_by(Niche.id)
                        )
                    ]
                    values = deepcopy(project.brief)
                    old_offer = data.get("text_draft") or {}
                    if (
                        tool == "offer"
                        and old_offer.get("tool") == "offer"
                        and old_offer.get("offer_schema") != 2
                    ):
                        values.update(old_offer.get("values", {}))
                    if tool == "offer":
                        values["placement"] = "page"
                    if tool == "proposal":
                        values["proposal_kind"] = (
                            parts[2].removeprefix("proposal-") if "-" in parts[2] else "service"
                        )
                    topic_pool = (
                        [
                            snapshot(row)
                            for row in session.scalars(
                                select(FunnelTopic)
                                .where(FunnelTopic.enabled.is_(True))
                                .order_by(FunnelTopic.id)
                            )
                        ]
                        if tool == "sequence"
                        else []
                    )
                    objection_pool = (
                        [
                            snapshot(row)
                            for row in session.scalars(
                                select(Objection)
                                .where(Objection.enabled.is_(True))
                                .order_by(Objection.id)
                            )
                        ]
                        if tool == "sequence"
                        else []
                    )
                    if tool == "ideas":
                        for niche in niches:
                            if values.get("niche") in {niche["title"], niche["slug"]}:
                                values["niche"] = niche["slug"]
                    fields = [key for key in QUESTIONS[tool] if not values.get(key)]
                    product_event(session, bot.id, callback.from_user.id, "tool_started", tool=tool)
                    return {
                        "tool": tool,
                        "offer_schema": 2 if tool == "offer" else None,
                        "fields": fields,
                        "step": 0,
                        "values": values,
                        "catalog": catalog,
                        "offers": offers,
                        "topic_pool": topic_pool,
                        "objection_pool": objection_pool,
                        "niches": niches,
                        "project_id": project.id,
                        "project_revision": project.revision,
                        "revision": max(
                            int(data.get("text_revision", 0)),
                            int((data.get("text_draft") or {}).get("revision", 0)),
                        )
                        + 1,
                        "token": uuid4().hex,
                        "limits": {
                            "ad": setting(session, "offer_ad_limit"),
                            "button": setting(session, "offer_button_limit"),
                        },
                    }

            try:
                await state.update_data(text_draft=await asyncio.to_thread(context))
            except (ObjectUnavailable, ValueError) as error:
                await callback.answer(str(error) or "Проект недоступен", show_alert=True)
                return
        elif not data.get("text_draft"):
            await callback.answer("Черновик недоступен", show_alert=True)
            return
        await render(callback.message, state, bot)
        await callback.answer()

    async def consume(state: FSMContext, draft: dict[str, Any], value: str | None) -> None:
        field = draft["fields"][draft["step"]]
        if draft.get("prompt_schema") == 1:
            if value is None and field not in ai_prompts.OPTIONAL:
                raise ValueError("Напишите ответ: он нужен для промпта.")
            if value is not None:
                value = (
                    validate_answer(field, value)
                    if field in BRIEF_FIELDS
                    else text(value, 4000 if field == "ca_report" else 1500)
                )
                if field == "audience" and value.casefold() in {"все", "для всех", "все люди"}:
                    raise ValueError("Уточните профессию, ситуацию или задачу клиента.")
            draft["values"][field] = (
                value
                if value is not None
                else None
                if field in {"case_destination", "ca_focus", "ca_report"}
                else "нет"
            )
            draft["step"] += 1
            draft["revision"] += 1
            draft.pop("result_id", None)
            await state.update_data(text_draft=draft, text_revision=draft["revision"])
            return
        offer_v2 = draft["tool"] == "offer" and draft.get("offer_schema") == 2
        if offer_v2 and field in offers.REQUIRED and value is None:
            raise ValueError("Это основа оффера. Напишите ответ своими словами.")
        if (
            offer_v2
            and field == "audience"
            and value
            and value.strip().casefold()
            in {
                "все",
                "для всех",
                "все подряд",
                "любой",
                "все люди",
            }
        ):
            raise ValueError(
                "Уточните, кому нужен продукт: профессия, ситуация или задача клиента."
            )
        if value is not None:
            if field in CHOICES and value not in CHOICES[field]:
                raise ValueError("Используйте кнопки выбора")
            value = validate_answer(field, value) if field in BRIEF_FIELDS else text(value, 1000)
        draft["values"][field] = value if value is not None else DEFAULTS.get(field)
        draft["step"] += 1
        draft.pop("offer_detail", None)
        if draft.pop("offer_edit", False):
            draft["step"] = len(draft["fields"])
        draft["revision"] += 1
        draft.pop("result_id", None)
        if offer_v2:
            draft.pop("offer_brief_saved", None)
        if value is not None and field in BRIEF_FIELDS and not offer_v2:
            brief_answer = value
            if field == "niche":
                brief_answer = next(
                    (row["title"] for row in draft["niches"] if row["slug"] == value), value
                )
            draft["brief_pending"] = {"field": field, "answer": brief_answer}
        await state.update_data(text_draft=draft, text_revision=draft["revision"])

    @router.callback_query(F.data.startswith("tw:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("text_draft")
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
            await callback.answer("Экран устарел", show_alert=True)
            return
        try:
            if draft.get("prompt_schema") == 1 and parts[2] not in {"back", "keep", "skip"}:
                raise ValueError("Экран устарел. Откройте инструмент заново.")
            if (
                draft["tool"] == "offer"
                and draft.get("offer_schema") == 2
                and parts[2].startswith("offer_")
            ):
                completed = draft["step"] == len(draft["fields"])
                if parts[2] == "offer_direct" and completed:
                    draft["values"]["offer_tone"] = "direct"
                    draft["revision"] += 1
                elif parts[2] == "offer_numbers" and completed:
                    if "desired_result" not in draft["fields"]:
                        draft["fields"].append("desired_result")
                    draft["step"] = draft["fields"].index("desired_result")
                    draft["offer_edit"] = True
                    draft["revision"] += 1
                elif parts[2] == "offer_brief" and completed:

                    def save_offer_brief() -> int:
                        with Session(engine) as session, session.begin():
                            user_id = owner(session, callback.from_user.id)
                            revision = int(draft["project_revision"])
                            for field, answer in draft["values"].items():
                                if field in BRIEF_FIELDS and answer:
                                    save_brief_answer(
                                        session,
                                        user_id,
                                        draft["project_id"],
                                        revision,
                                        field,
                                        answer,
                                    )
                                    revision = get_project(
                                        session, user_id, draft["project_id"]
                                    ).revision
                            return revision

                    draft["project_revision"] = await asyncio.to_thread(save_offer_brief)
                    draft["offer_brief_saved"] = True
                    draft["revision"] += 1
                elif parts[2] in {"offer_choice", "offer_custom"} and not completed:
                    field = draft["fields"][draft["step"]]
                    if parts[2] == "offer_custom":
                        draft["offer_detail"] = "custom"
                        draft["revision"] += 1
                    elif len(parts) == 4 and parts[3] in offers.CHOICES.get(field, {}):
                        _, value = offers.CHOICES[field][parts[3]]
                        if value is None:
                            draft["offer_detail"] = parts[3]
                            draft["revision"] += 1
                        else:
                            await consume(state, draft, value or None)
                    else:
                        raise ValueError("Экран устарел")
                else:
                    raise ValueError("Экран устарел")
                await state.update_data(text_draft=draft, text_revision=draft["revision"])
                await render(callback.message, state, bot)
                await callback.answer()
                return
            if parts[2] == "prompt_page":
                if (
                    len(parts) != 4
                    or draft["tool"] != "sequence"
                    or not draft.get("approved")
                    or draft["step"] != len(draft["fields"])
                ):
                    raise ValueError("Сначала утвердите темы")
                prompt_text = "\n\n---\n\n".join(
                    funnel.prompts(
                        draft["values"],
                        draft["topics"],
                        draft["catalog"][0]["payload"]["rules"],
                        draft.get("split", False),
                    )
                )
                page = int(parts[3])
                pages = (len(prompt_text) + 499) // 500
                if not 0 <= page < pages:
                    raise ValueError("Экран устарел")
                prefix = f"tw:{draft['revision']}:prompt_page:"
                rows = []
                if page:
                    rows.append([("Предыдущая часть", prefix + str(page - 1))])
                if page + 1 < pages:
                    rows.append([("Следующая часть", prefix + str(page + 1))])
                rows += [[("Результат", "ct:resume")], [("В меню", "menu")]]
                await callback.message.edit_text(
                    f"Промт · часть {page + 1}/{pages}\n\n<pre>"
                    + escape(prompt_text[page * 500 : (page + 1) * 500])
                    + "</pre>",
                    parse_mode="HTML",
                    reply_markup=buttons(rows),
                )
                await callback.answer()
                return
            if (
                parts[2] == "brief"
                and len(parts) == 4
                and draft.get("brief_pending")
                and parts[3] in {"save", "skip"}
            ):
                if parts[3] == "save":
                    pending = draft["brief_pending"]

                    def save() -> int:
                        with Session(engine) as session, session.begin():
                            user_id = owner(session, callback.from_user.id)
                            save_brief_answer(
                                session,
                                user_id,
                                draft["project_id"],
                                draft["project_revision"],
                                pending["field"],
                                pending["answer"],
                            )
                            return get_project(session, user_id, draft["project_id"]).revision

                    draft["project_revision"] = await asyncio.to_thread(save)
                draft.pop("brief_pending")
                draft["revision"] += 1
            elif (
                draft["tool"] == "sequence"
                and parts[2] in {"split", "approve", "replace", "remove"}
                and draft["step"] == len(draft["fields"])
            ):
                if parts[2] == "split":
                    draft["split"] = not draft.get("split", False)
                elif parts[2] == "approve":
                    draft["approved"] = True
                else:
                    if draft.get("approved") or len(parts) != 4:
                        raise ValueError("Темы уже утверждены; измените параметры через историю")
                    index = int(parts[3])
                    if not 0 <= index < len(draft["topics"]):
                        raise ValueError("Экран устарел")
                    if parts[2] == "remove":
                        if len(draft["topics"]) <= 1:
                            raise ValueError("Оставьте хотя бы одну тему")
                        draft["topics"].pop(index)
                    else:
                        current = draft["topics"][index]
                        alternatives = [
                            row
                            for row in draft["topic_pool"]
                            if row["payload"]["stage"] == current["payload"]["stage"]
                            and funnel.eligible(row, draft["values"])
                            and row["id"] not in {item["id"] for item in draft["topics"]}
                        ]
                        if not alternatives:
                            raise ValueError("Других подходящих тем пока нет")
                        draft["topics"][index] = deepcopy(alternatives[0])
                draft["revision"] += 1
            elif parts[2] == "back":
                draft.pop("brief_pending", None)
                if draft["step"] == 0:
                    await state.update_data(paused=True)
                    await edit(
                        callback.message,
                        "Промпты",
                        prompt_menu_rows(),
                    )
                    await callback.answer()
                    return
                draft["step"] -= 1
                draft["revision"] += 1
                draft.pop("topics", None)
                draft.pop("approved", None)
            elif (
                parts[2] == "proposal_kind"
                and len(parts) == 4
                and draft["tool"] == "proposal"
                and draft["step"] == len(draft["fields"])
                and any(row["payload"]["model"] == parts[3] for row in draft["catalog"])
            ):
                draft["values"]["proposal_kind"] = parts[3]
                draft["revision"] += 1
            elif (
                parts[2] == "placement"
                and len(parts) == 4
                and parts[3] in CHOICES["placement"]
                and draft["tool"] == "offer"
                and draft["step"] == len(draft["fields"])
            ):
                draft["values"]["placement"] = parts[3]
                draft["revision"] += 1
            elif (
                parts[2] == "more"
                and draft["tool"] == "ideas"
                and draft["step"] == len(draft["fields"])
            ):
                draft["offset"] = int(draft.get("offset", 0)) + 5
                draft["revision"] += 1
            elif parts[2] == "suggest" and draft["tool"] == "ideas":
                await edit(
                    callback.message,
                    "Передать владельцу введённое название ниши для расширения каталога? "
                    "Без контактов и других полей брифа.",
                    [
                        [("Передать нишу", f"tw:{draft['revision']}:suggest_confirm")],
                        [("Назад", "ct:resume")],
                    ],
                )
                draft["suggest_pending"] = True
                await state.update_data(text_draft=draft)
                await callback.answer()
                return
            elif parts[2] == "suggest_confirm" and draft.get("suggest_pending"):

                def suggest() -> None:
                    with Session(engine) as session, session.begin():
                        suggest_niche(
                            session,
                            owner(session, callback.from_user.id),
                            draft["values"].get("niche") or "Другая",
                        )

                await asyncio.to_thread(suggest)
                draft.pop("suggest_pending")
                draft["revision"] += 1
            elif not draft.get("brief_pending") and draft["step"] < len(draft["fields"]):
                field = draft["fields"][draft["step"]]
                if parts[2] == "skip":
                    await consume(state, draft, None)
                elif draft["tool"] == "sequence" and parts[2] == "finish":
                    draft["step"] = len(draft["fields"])
                    draft["revision"] += 1
                elif (
                    draft["tool"] == "sequence"
                    and (field in MULTI_CHOICES or field == "objections")
                    and parts[2] in {"toggle", "multi_done"}
                ):
                    options = MULTI_CHOICES.get(field, {})
                    if field == "objections":
                        options = {str(row["id"]): row["title"] for row in draft["objection_pool"]}
                    chosen = draft.setdefault("multi", {}).setdefault(field, [])
                    if parts[2] == "toggle" and len(parts) == 4 and parts[3] in options:
                        if parts[3] in chosen:
                            chosen.remove(parts[3])
                        else:
                            chosen.append(parts[3])
                        draft["revision"] += 1
                    elif parts[2] == "multi_done":
                        await consume(
                            state, draft, "; ".join(options[code] for code in chosen) or None
                        )
                    else:
                        raise ValueError("Экран устарел")
                elif parts[2] == "keep" and draft["values"].get(field):
                    await consume(state, draft, str(draft["values"][field]))
                elif parts[2] == "choose" and len(parts) == 4:
                    if field == "niche":
                        value = next(
                            (row["slug"] for row in draft["niches"] if str(row["id"]) == parts[3]),
                            None,
                        )
                    elif field == "audience":
                        value = {"a0": "Начинающие", "a1": "Практикующие", "a2": "Заказчики"}.get(
                            parts[3]
                        )
                    else:
                        value = parts[3] if parts[3] in CHOICES.get(field, {}) else None
                    if value is None:
                        raise ValueError("Экран устарел")
                    await consume(state, draft, value)
                else:
                    raise ValueError("Экран устарел")
            else:
                raise ValueError("Экран устарел")
            await state.update_data(text_draft=draft, text_revision=draft["revision"])
            await render(callback.message, state, bot)
            await callback.answer()
        except (ValueError, ObjectUnavailable) as error:
            await callback.answer(str(error) or "Проект недоступен", show_alert=True)

    @router.message(
        StateFilter("text_input"), ~F.text.startswith("/"), flags={"requires_access": True}
    )
    async def answer(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("text_draft")
        if data.get("mode") != "expert" or data.get("paused") or not draft:
            return
        try:
            await consume(state, draft, message.text or "")
        except ValueError as error:
            await message.answer(str(error))
            return
        screen = message.model_copy(update={"message_id": data["screen_message_id"]})
        await render(screen, state, bot)

    @router.callback_query(F.data.startswith("oc:"))
    async def copy_offer(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        parts = (callback.data or "").split(":")
        if (
            (await state.get_data()).get("mode") != "expert"
            or not isinstance(callback.message, Message)
            or len(parts) != 4
            or not all(part.isdecimal() for part in parts[1:])
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        try:
            result_id, index, page = map(int, parts[1:])

            def read() -> str:
                with Session(engine) as session:
                    result = get_result(
                        session, owner(session, callback.from_user.id), bot.id, result_id
                    )
                    if (
                        result.tool != "offer"
                        or result.inputs.get("offer_schema") != 2
                        or not 0 <= index < 3
                    ):
                        raise ObjectUnavailable
                    saved_parts = result.inputs.get("offer_parts")
                    if isinstance(saved_parts, list) and len(saved_parts) == 3:
                        return str(saved_parts[index])
                    return offers.variants(result.inputs["values"], result.inputs["catalog"])[index]

            content = await asyncio.to_thread(read)
            count = (len(content) + 255) // 256
            if not 0 <= page < count:
                raise ObjectUnavailable
            chunk = content[page * 256 : (page + 1) * 256]
            rows = []
            if page:
                rows.append([("← Часть", f"oc:{result_id}:{index}:{page - 1}")])
            if page + 1 < count:
                rows.append([("Следующая часть →", f"oc:{result_id}:{index}:{page + 1}")])
            rows += [[("История / полный результат", f"hr:open:{result_id}")], [("В меню", "menu")]]
            markup = buttons(rows)
            markup.inline_keyboard.insert(
                0,
                [
                    InlineKeyboardButton(
                        text="📋 Копировать" if count == 1 else f"📋 Копировать часть {page + 1}",
                        copy_text=CopyTextButton(text=chunk),
                    )
                ],
            )
            await retire(bot, callback.from_user.id, callback.message.message_id)
            await callback.message.answer(
                f"Вариант {index + 1}"
                + (f" · часть {page + 1}/{count}" if count > 1 else "")
                + "\n\n<pre>"
                + escape(chunk)
                + "</pre>"
                + (
                    "\n\nСкопируйте части по порядку без дополнительных разделителей."
                    if count > 1
                    else ""
                ),
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            await callback.answer()
        except (ValueError, ObjectUnavailable):
            await callback.answer("Результат недоступен. Откройте свою историю.", show_alert=True)

    @router.callback_query(F.data.startswith("ci:"), flags={"requires_access": True})
    async def material(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Экран устарел", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        try:
            if len(parts) == 4 and parts[1] == "prepare":
                token = uuid4().hex[:12]

                def validate() -> None:
                    with Session(engine) as session:
                        result = get_result(
                            session,
                            owner(session, callback.from_user.id),
                            bot.id,
                            int(parts[2]),
                        )
                        if result.tool != "ideas" or not 0 <= int(parts[3]) < len(
                            render_result(result.inputs)[1]
                        ):
                            raise ObjectUnavailable

                await asyncio.to_thread(validate)
                await state.update_data(
                    idea_material={
                        "token": token,
                        "id": int(parts[2]),
                        "index": int(parts[3]),
                        "message_id": callback.message.message_id,
                    }
                )
                await edit(
                    callback.message,
                    "Создать новый материал по идее? Незавершённый мастер материала "
                    "будет заменён; идея останется в истории.",
                    [[("Подтверждаю", "ci:confirm:" + token)], [("Назад", "hr:open:" + parts[2])]],
                )
            elif len(parts) == 3 and parts[1] == "confirm":
                pending = data.get("idea_material")
                if (
                    not pending
                    or pending["token"] != parts[2]
                    or pending["message_id"] != callback.message.message_id
                ):
                    raise ObjectUnavailable

                def prepare() -> dict[str, Any]:
                    with Session(engine) as session:
                        user_id = owner(session, callback.from_user.id)
                        result = get_result(session, user_id, bot.id, pending["id"])
                        project = get_project(session, user_id, result.project_id)
                        if project.archived:
                            raise ObjectUnavailable
                        idea = render_result(result.inputs)[1][pending["index"]]
                        channels = [
                            (row.id, row.title)
                            for row in session.scalars(
                                select(Channel).where(Channel.owner_user_id == user_id)
                            )
                        ]
                        return {
                            "step": 0,
                            "revision": int(data.get("wizard_revision", 0)) + 1,
                            "owner_id": user_id,
                            "project_id": project.id,
                            "channels": channels,
                            "channel_id": None,
                            "publication_key": uuid4().hex,
                            "suggested_title": idea["title"],
                        }

                await state.update_data(
                    material_draft=await asyncio.to_thread(prepare),
                    active_tool="material",
                    idea_material=None,
                    paused=True,
                )
                await edit(
                    callback.message,
                    "Название идеи подготовлено. Содержимое пришлёт автор.",
                    [[("➕ Создать лид-магнит", "m:new")], [("В меню", "menu")]],
                )
            else:
                raise ObjectUnavailable
            await callback.answer()
        except (ObjectUnavailable, ValueError, IndexError):
            await callback.answer("Идея недоступна или экран устарел", show_alert=True)

    return router
