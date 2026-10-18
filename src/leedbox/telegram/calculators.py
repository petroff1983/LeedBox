"""Native planning: two choices, explained inputs, chronological messages.

Direction: inherit lead-magnet/UTM presentation; bold headings and native buttons.
One question per message; obsolete buttons retired, earlier text never rewritten.
Money flow and assumptions are explicit, forecasts never promised as earnings.
Live appearance requires owner QA; mock Bot API verifies behavior, not rendering.
"""

import asyncio
from decimal import Decimal
from html import escape
from typing import Any
from urllib.parse import urlencode
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

from leedbox.application.projects import ensure_project, register_user
from leedbox.application.results import get_result, save_result
from leedbox.application.settings import product_event
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.numbers import parse_number
from leedbox.domain.profit_planning import (
    Forecast,
    advertising_plan,
    display_number,
    romi,
    telegram_plan,
)
from leedbox.domain.wizard import Step, advance, back
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import ToolResult, User
from leedbox.telegram.library import keyboard
from leedbox.telegram.material_ui import next_message, retire

VERSION = "profit-planning-3"
EDITABLE_VERSIONS = {"profit-planning-2", VERSION}
INTRO = (
    "📊 Медиаплан для заказчика\n\n"
    "Рассчитайте воронку, рекламный бюджет и финансовый результат "
    "по данным клиента. Готовый отчёт можно отправить ему в Telegram.\n\n"
    "📣 Директ / таргет — план продвижения товара или услуги.\n"
    "📱 Telegram-канал — монетизация базы или план запуска.\n\n"
    "Выберите источник. Все показатели — прогноз, не гарантия заработка."
)
LABELS = {
    # Saved historical results are displayed/exported, never recalculated.
    "funnel": "Сколько нужно подписчиков",
    "campaign": "Итоги кампании",
    "breakeven": "Безубыточность",
    "budget_cpl": "Бюджет по CPL",
    "budget_cpc": "Бюджет по CPC",
    "ads": "📣 Заработок: Директ / таргет",
    "tg_subscribers": "📱 Telegram: от подписчиков",
    "tg_budget": "📱 Telegram: от бюджета",
    "tg_goal": "📱 План запуска Telegram-канала",
}
TG_COMMON = (
    Step(
        "subscriber_cost",
        "💸 Стоимость привлечения одного подписчика, ₽\n\n"
        "Сколько в среднем обходится новый подписчик? Для органического "
        "(бесплатного) привлечения введите 0.\n\n💡 Пример: 150 или 0 для органики.",
    ),
    Step(
        "lead_rate",
        "💬 Конверсия подписчика в заявку, %\n\n"
        "Какой процент аудитории оставит заявку или напишет по поводу продукта? "
        "Простой переход по ссылке сам по себе не считаем заявкой.\n\n"
        "💡 Из 100 подписчиков 5 оставляют заявку → введите 5. Это пример, не норма.",
    ),
    Step(
        "sale_rate",
        "🤝 Конверсия заявки в оплату, %\n\n"
        "Какой процент обратившихся покупает продукт или услугу?\n\n"
        "💡 Из 10 диалогов 3 заканчиваются оплатой → введите 30.",
    ),
    Step(
        "price",
        "💎 Стоимость продукта / средний чек, ₽\n\n"
        "Введите цену одной продажи, больше 0. В этой модели себестоимость "
        "и прочие расходы не вычитаются — учитываем только привлечение и налог.\n\n"
        "💡 Пример: 50 000.",
    ),
    Step(
        "tax_rate",
        "🏛 Ставка налога с выручки, %\n\n"
        "Введите свой процент или 0, если налог не учитываем. "
        "Бот не определяет налоговый режим.\n\n💡 Пример ввода: 6.",
    ),
)
SCHEMAS = {
    "ads": (
        Step(
            "target",
            "💼 Финансовая цель заказчика\n\n"
            "Какую прибыль с продаж хочет получить клиент после вычета "
            "себестоимости товара, но до расходов на рекламу?\n\n"
            "💡 Введите желаемую сумму в рублях. Пример: 30 000.",
        ),
        Step(
            "margin",
            "📦 Маржа с одной продажи\n\n"
            "Сколько рублей остаётся у заказчика с продажи до рекламы "
            "(цена товара минус себестоимость)? Введите сумму, не процент.\n\n"
            "💡 Цена 5 000 ₽ − себестоимость 4 000 ₽ → маржа 1 000 ₽.",
        ),
        Step(
            "sale_rate",
            "📞 Конверсия отдела продаж, %\n\n"
            "Какой процент обработанных заявок закрывается в оплату? "
            "Введите свой процент, больше 0.\n\n"
            "💡 Из 10 заявок 3 продажи → 30%. Из 100 заявок 33 продажи → 33%.",
        ),
        Step(
            "lead_rate",
            "🌐 Конверсия лендинга в заявку, %\n\n"
            "Какой процент посетителей сайта или формы оставляет заявку? "
            "Введите свой процент, больше 0.\n\n"
            "💡 Из 100 посетителей 10 заявок → введите 10.",
        ),
        Step(
            "click_cost",
            "🎯 Ожидаемая цена клика, ₽\n\n"
            "Сколько в среднем стоит переход по рекламе в нише заказчика?\n\n"
            "💡 Введите сумму в рублях. Пример: 15.",
        ),
    ),
    "tg_subscribers": (
        Step(
            "amount",
            "👥 Текущее или плановое количество подписчиков\n\n"
            "Сколько человек будет в канале на момент прогрева или продаж? "
            "Введите целое число.\n\n💡 Пример: 5 000. "
            "Если канала пока нет, введите 0 — перейдём к планированию запуска.",
        ),
        *TG_COMMON,
    ),
    "tg_budget": (
        Step(
            "amount",
            "Какой бюджет готовы потратить на подписчиков, ₽?\n\n"
            "Посчитаем аудиторию, продажи и остаток после рекламы и указанного налога.",
        ),
        *TG_COMMON,
    ),
    "tg_goal": (
        Step(
            "amount",
            "🎯 Финансовая цель запуска\n\n"
            "Какую сумму хотите получить после привлечения подписчиков и указанного налога? "
            "Посчитаем нужную аудиторию и бюджет. Прочие расходы не учитываем.\n\n"
            "💡 Пример: 100 000.",
        ),
        *TG_COMMON,
    ),
}
INPUT_LABELS = {
    "target": "Цель по марже до рекламы, ₽",
    "margin": "Маржа одной продажи, ₽",
    "sale_rate": "Заявка → продажа, %",
    "lead_rate": "Аудитория → заявка, %",
    "click_cost": "Цена клика, ₽",
    "subscriber_cost": "Цена подписчика, ₽",
    "price": "Цена продукта, ₽",
    "tax_rate": "Налог с выручки, %",
}


def menu_rows() -> list[list[tuple[str, str]]]:
    return [
        [("📣 Заработок: Директ / таргет", "calc:new:ads")],
        [("📱 Заработок в Telegram-канале", "calc:telegram")],
        [("📚 Сохранённые расчёты", "calc:history")],
        [("🏠 Главное меню", "menu")],
    ]


def resumable(draft: dict[str, Any] | None) -> bool:
    return bool(draft and draft.get("tool") in SCHEMAS)


def sharing_url(username: str, report: str) -> str | None:
    """Native chat picker; never sends on behalf of the expert or stores public reports."""
    if not username:
        return None
    url = "https://t.me/share/url?" + urlencode(
        {
            "url": f"https://t.me/{username}",
            "text": report,
        }
    )
    # Keep a conservative client URL size; unusually long reports can be forwarded manually.
    return url if len(url) <= 8192 else None


def calculate(tool: str, values: dict[str, str | None]) -> str:
    if tool not in SCHEMAS:
        raise ValueError("Этот расчёт больше не поддерживается. Откройте /calc")

    def number(key: str) -> Decimal:
        return Decimal(values[key] or "0")

    def money(value: Decimal) -> str:
        return display_number(value) + " ₽"

    def count(value: Decimal) -> str:
        return display_number(value).removesuffix(",00")

    amount_label = {
        "tg_subscribers": "Подписчики",
        "tg_budget": "Бюджет, ₽",
        "tg_goal": "Цель после рекламы и налога, ₽",
    }.get(tool, "Значение")
    inputs = "\n".join(
        f"{INPUT_LABELS.get(step.key, amount_label)}: {values[step.key]}" for step in SCHEMAS[tool]
    )
    result: Forecast | None
    if tool == "ads":
        result = advertising_plan(
            number("target"),
            number("margin"),
            number("sale_rate"),
            number("lead_rate"),
            number("click_cost"),
        )
        source = {"yandex": "Яндекс Директ", "target": "Таргетированная реклама"}.get(
            values.get("platform") or "", "Реклама"
        )
        return_rate = romi(result)
        rate = (
            display_number(return_rate, places=0) + "%"
            if return_rate is not None
            else ("не рассчитывается — рекламный бюджет равен нулю")
        )
        return (
            f"📊 МЕДИАПЛАН ДЛЯ ЗАКАЗЧИКА\n📣 Источник: {source}\n\n"
            f"🎯 Финансовая цель до рекламы: {money(result.income)}\n"
            f"📦 Маржа с одной продажи: {money(number('margin'))}\n\n"
            "Требуемая воронка продаж\n"
            f"1️⃣ Продажи: ≈ {display_number(result.sales, places=0)}\n"
            f"2️⃣ Заявки: ≈ {display_number(result.leads, places=0)} "
            f"(конверсия в продажу {values['sale_rate']}%)\n"
            f"3️⃣ Клики: ≈ {display_number(result.audience, places=0)} "
            f"(конверсия в заявку {values['lead_rate']}%)\n\n"
            f"💸 Рекламный бюджет: {money(result.spend)}\n\n"
            "💰 Финансовый прогноз\n"
            f"Маржа с продаж до рекламы: {money(result.income)}\n"
            f"Расходы на рекламу: − {money(result.spend)}\n"
            f"🟰 После рекламы: {money(result.profit)}\n"
            f"📈 ROMI (окупаемость рекламы): {rate}\n\n"
            + (
                "⚠️ Реклама съедает всю маржу: "
                "при таких показателях масштабировать её невыгодно.\n\n"
                if result.profit <= 0
                else ""
            )
            + "Прогноз, не гарантия. Налоги и прочие расходы учтены только "
            "если вы уже вычли их из маржи одной продажи. Объёмы округлены для показа; "
            "бюджет рассчитан без промежуточного округления.\n\n" + f"Ваши параметры\n{inputs}\n\n"
            "Как считаем\nПродажи = цель ÷ маржа одной продажи.\n"
            "Заявки = продажи ÷ (конверсия в продажу / 100).\n"
            "Переходы = заявки ÷ (конверсия в заявку / 100).\n"
            "Бюджет = переходы × цена клика. Остаток = цель − бюджет.\n"
            "ROMI = остаток после рекламы ÷ рекламный бюджет × 100%.\n"
            "Промежуточные значения не округляем: дроби — ожидаемые значения, "
            "не гарантированные люди."
        )
    mode = tool.removeprefix("tg_")
    result = telegram_plan(
        mode,
        number("amount"),
        number("subscriber_cost"),
        number("lead_rate"),
        number("sale_rate"),
        number("price"),
        number("tax_rate"),
    )
    if result is None:
        return (
            "📱 Telegram: цель пока недостижима\n\n"
            "При этих конверсиях, цене и налоге один привлечённый подписчик "
            "не даёт положительной прибыли после рекламы. "
            "Увеличение бюджета не поможет достичь цели. "
            "Измените стоимость привлечения, конверсии или цену продукта.\n\n"
            f"Ваши параметры\n{inputs}\n\n"
            "Как считаем\nПрибыль на подписчика = конверсия в заявку / 100 × "
            "конверсия в продажу / 100 × цена × (1 − налог / 100) − цена подписчика."
        )
    extra = ""
    if mode == "goal":
        extra = f"Цель после рекламы и налога: {money(number('amount'))}\n"
    elif mode == "budget":
        extra = (
            f"Заданный бюджет: {money(number('amount'))}\n"
            f"Неиспользованный остаток бюджета: {money(result.unused_budget)}\n"
        )
    return_rate = romi(result)
    roi = (
        display_number(return_rate, places=0) + "%"
        if return_rate is not None
        else "не рассчитывается — нет расходов на привлечение"
    )
    heading = (
        "📊 ПЛАН ЗАПУСКА TELEGRAM-КАНАЛА"
        if mode == "goal"
        else ("📊 МЕДИАПЛАН МОНЕТИЗАЦИИ TELEGRAM-КАНАЛА")
    )
    return (
        f"{heading}\n\n{extra}"
        f"👥 База аудитории: {count(result.audience)} подписчиков\n"
        f"💎 Средний чек: {money(number('price'))}\n"
        f"💸 Привлечение подписчиков: {money(result.spend)}"
        + (" (органический трафик)" if not number("subscriber_cost") else "")
        + "\n\nПрогноз воронки продаж\n"
        f"1️⃣ Заявки: ≈ {count(result.leads)} (конверсия {values['lead_rate']}%)\n"
        f"2️⃣ Продажи: ≈ {count(result.sales)} (конверсия {values['sale_rate']}%)\n\n"
        "💰 Финансовый прогноз\n"
        f"Выручка (оборот): {money(result.income)}\n"
        f"Налог ({values['tax_rate']}% выручки): − {money(result.tax)}\n"
        f"Расходы на привлечение: − {money(result.spend)}\n"
        f"🟰 После рекламы и налога: {money(result.profit)}\n"
        f"📈 ROMI (остаток / привлечение): {roi}\n\n"
        + (
            "⚠️ Прогноз убыточен. Измените параметры до вложений в рекламу.\n\n"
            if result.profit < 0
            else ""
        )
        + "Один цикл продаж. Без себестоимости продукта и других расходов; "
        "это прогноз, не гарантия чистой прибыли.\n\n"
        f"Ваши параметры\n{inputs}\n\n"
        "Как считаем\nЗаявки = подписчики × конверсия / 100. Продажи = заявки × конверсия / 100.\n"
        "Выручка = продажи × цена. Налог = выручка × ставка / 100.\n"
        "Остаток = выручка − налог − привлечение. Окупаемость = остаток / привлечение × 100%.\n"
        + (
            "От бюджета покупаем целое число подписчиков вниз, не превышая бюджет.\n"
            if mode == "budget"
            else ""
        )
        + (
            "Под цель: подписчики = цель / прибыль на подписчика, округляем вверх. "
            "Здесь уже включены реклама и налог.\n"
            if mode == "goal"
            else ""
        )
        + "Заявки и продажи — ожидаемые значения; промежуточные дроби не округляем."
    )


def formatted(text: str) -> str:
    return "\n".join(
        f"<b>{escape(line)}</b>"
        if index == 0
        or line.startswith(("💰", "🟰", "💸 Рекламный бюджет"))
        or line
        in {"Требуемая воронка продаж", "Прогноз воронки продаж", "Ваши параметры", "Как считаем"}
        or line in {step.prompt.split("\n")[0] for steps in SCHEMAS.values() for step in steps}
        else escape(line)
        for index, line in enumerate(text.splitlines())
    )


def build_calculator_router(engine: Engine, config: Settings | None = None) -> Router:
    router = Router()

    async def render(bot: Bot, chat_id: int, state: FSMContext, error: str = "") -> None:
        data = await state.get_data()
        draft = data["calc_draft"]
        if draft.get("version") != VERSION:
            # Preserve saved v2 text; resumed completed drafts get one separate v3 result.
            draft = {**draft, "revision": draft["revision"] + 1, "version": VERSION}
        steps = SCHEMAS[draft["tool"]]
        prefix = f"cw:{draft['revision']}:"
        rows: list[list[tuple[str, str]]] = []
        complete = draft["step"] == len(steps)
        if draft.get("phase") == "platform":
            text = (
                "📣 Где планируете рекламу?\n\nФормулы одинаковые. "
                "Стоимость перехода и конверсии вы введёте для своего источника."
            )
            rows = [
                [("Яндекс Директ", prefix + "platform:yandex")],
                [("Таргетированная реклама", prefix + "platform:target")],
            ]
            await state.set_state(None)
        elif draft.get("phase") == "zero":
            text = (
                "📱 Подписчиков пока нет\n\nНачнём с рекламного бюджета "
                "или с суммы, которую хотите заработать?"
            )
            rows = [
                [("💵 От бюджета", "calc:restart:tg_budget")],
                [("🎯 От желаемой прибыли", "calc:restart:tg_goal")],
            ]
            await state.set_state(None)
        elif complete:
            text = calculate(draft["tool"], draft["values"])

            def save() -> int:
                with Session(engine) as session, session.begin():
                    user = register_user(session, chat_id)
                    return save_result(
                        session,
                        user.id,
                        draft["project_id"],
                        bot.id,
                        draft["tool"],
                        {"values": draft["values"], "factors": []},
                        text,
                        f"calc:{draft['token']}:{draft['revision']}",
                        VERSION,
                    ).id

            draft["result_id"] = await asyncio.to_thread(save)
            text = text.split("\n\nВаши параметры", 1)[0]
            rows = [
                [("📁 В историю", "calc:history")],
                [("✏️ Изменить параметры", prefix + "edit")],
                [("🔎 Параметры и формулы", f"cr:open:{draft['result_id']}")],
                [("📄 Скачать расчёт .md", f"cr:export:{draft['result_id']}")],
                [("🧮 Другой расчёт", "section:calc")],
            ]
            await state.set_state(None)
        else:
            step = steps[draft["step"]]
            text = f"{LABELS[draft['tool']]} · {draft['step'] + 1}/{len(steps)}\n\n{step.prompt}"
            if draft["tool"] == "tg_budget" and step.key == "subscriber_cost":
                text = text.replace(
                    "Для органического (бесплатного) привлечения введите 0.",
                    "Введите цену больше 0. Бесплатное привлечение считают от числа подписчиков.",
                )
            if step.key in draft["values"]:
                text += f"\n\nВаше значение: {draft['values'][step.key]}"
                rows.append([("✅ Оставить значение", prefix + "keep")])
            await state.set_state("calc_input")
        if error:
            text += f"\n\n⚠️ {error}"
        if draft["step"] and not complete:
            rows.append([("← Предыдущий вопрос", prefix + "back")])
        if not complete:
            rows.append([("✖️ Отменить", prefix + "cancel")])
        rows.append([("🏠 Главное меню", "menu")])
        await retire(bot, chat_id, draft.get("screen", data.get("screen_message_id")))
        markup = keyboard(rows)
        if complete and not draft.get("phase"):
            me = await bot.me()
            share = sharing_url(me.username or "", text)
            markup.inline_keyboard.insert(
                0,
                [
                    InlineKeyboardButton(text="📤 Отправить клиенту", url=share)
                    if share
                    else InlineKeyboardButton(
                        text="📤 Отправить клиенту", callback_data=prefix + "share"
                    )
                ],
            )
        sent = await bot.send_message(
            chat_id,
            formatted(text),
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        draft["screen"] = sent.message_id
        await state.update_data(
            calc_draft=draft,
            calc_revision=draft["revision"],
            screen_message_id=sent.message_id,
            paused=False,
            active_tool="calc",
        )
        if config and complete and not draft.get("phase"):
            from leedbox.telegram.monetization import recommend

            await recommend(engine, config, bot, chat_id, state, draft["tool"])

    @router.message(Command("calc"))
    async def command(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert":
            await message.answer("Для инструментов откройте /start")
            return
        rows = menu_rows()
        await retire(bot, message.chat.id, (data.get("calc_draft") or {}).get("screen"))
        await retire(bot, message.chat.id, data.get("calc_menu_screen"))
        await state.update_data(paused=True)
        sent = await message.answer(INTRO, reply_markup=keyboard(rows))
        await state.update_data(calc_menu_screen=sent.message_id)

    @router.callback_query(F.data == "calc:telegram")
    async def telegram_modes(callback: CallbackQuery, state: FSMContext) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        await state.update_data(paused=True)
        await next_message(
            callback.message,
            "📱 Заработок в Telegram-канале\n\nС чего начнём расчёт?",
            reply_markup=keyboard(
                [
                    [("👥 Есть подписчики", "calc:new:tg_subscribers")],
                    [("💵 Есть бюджет", "calc:new:tg_budget")],
                    [("🎯 Есть цель по прибыли", "calc:new:tg_goal")],
                    [("🏠 Главное меню", "menu")],
                ]
            ),
        )
        await callback.answer()

    @router.callback_query(
        F.data.startswith("calc:new:")
        | F.data.startswith("calc:restart:")
        | (F.data == "calc:resume"),
        flags={"requires_access": True},
    )
    async def start(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        if data.get("mode") != "expert" or not isinstance(callback.message, Message):
            await callback.answer("Экран устарел", show_alert=True)
            return
        if callback.data == "calc:resume":
            if not resumable(data.get("calc_draft")):
                await next_message(
                    callback.message,
                    "Старый расчёт больше не используется. Выберите один из двух новых "
                    "инструментов; сохранённые результаты доступны в истории.",
                    reply_markup=keyboard(menu_rows()),
                )
                await callback.answer()
                return
        else:
            tool = (callback.data or "").split(":")[-1]
            if tool not in SCHEMAS:
                await next_message(
                    callback.message,
                    "Этот калькулятор заменён. Выберите новый расчёт.",
                    reply_markup=keyboard(menu_rows()),
                )
                await callback.answer()
                return
            previous = data.get("calc_draft")
            if (
                (callback.data or "").startswith("calc:new:")
                and resumable(previous)
                and previous
                and previous["step"] < len(SCHEMAS[previous["tool"]])
            ):
                if previous["tool"] == tool:
                    await retire(bot, callback.from_user.id, callback.message.message_id)
                    await render(bot, callback.from_user.id, state)
                    await callback.answer()
                    return
                await state.update_data(paused=True)
                await next_message(
                    callback.message,
                    f"📝 Сохранён незавершённый расчёт «{LABELS[previous['tool']]}».\n\n"
                    "Вернитесь к нему с сохранёнными ответами или начните выбранный "
                    "расчёт заново. При новом старте прежний расчёт будет заменён.",
                    reply_markup=keyboard(
                        [
                            [("↩️ Вернуться к расчёту", "calc:resume")],
                            [("Начать заново", f"calc:restart:{tool}")],
                            [("🏠 Главное меню", "menu")],
                        ]
                    ),
                )
                await callback.answer()
                return

            def context() -> int:
                with Session(engine) as session, session.begin():
                    user = register_user(session, callback.from_user.id)
                    project = ensure_project(session, user.id)
                    product_event(
                        session,
                        bot.id,
                        callback.from_user.id,
                        "tool_started",
                        tool=tool,
                        version=VERSION,
                    )
                    return project.id

            await state.update_data(
                calc_draft={
                    "step": 0,
                    "revision": int(data.get("calc_revision", 0)) + 1,
                    "tool": tool,
                    "values": {},
                    "token": uuid4().hex,
                    "project_id": await asyncio.to_thread(context),
                    "screen": callback.message.message_id,
                    "phase": "platform" if tool == "ads" else None,
                }
            )
        draft = (await state.get_data())["calc_draft"]
        if callback.message.message_id != draft.get("screen"):
            await retire(bot, callback.from_user.id, callback.message.message_id)
        await render(bot, callback.from_user.id, state)
        await callback.answer()

    async def accept(
        bot: Bot, state: FSMContext, chat_id: int, draft: dict[str, Any], raw: str
    ) -> None:
        steps = SCHEMAS[draft["tool"]]
        step = steps[draft["step"]]
        value = parse_number(
            raw,
            percentage=step.key in {"lead_rate", "sale_rate", "tax_rate"},
            integer=step.key == "amount" and draft["tool"] == "tg_subscribers",
        )
        positive = (
            (draft["tool"] == "ads" and step.key != "click_cost")
            or step.key == "price"
            or (draft["tool"] == "tg_goal" and step.key == "amount")
            or (draft["tool"] == "tg_budget" and step.key == "subscriber_cost")
        )
        if positive and value == 0:
            raise ValueError("Для этого поля нужно число больше 0")
        updated = advance(draft, steps, str(value))
        if draft["tool"] == "tg_subscribers" and step.key == "amount" and value == 0:
            updated.update(tool="tg_goal", step=0, values={}, phase=None)
        if updated["step"] == len(steps):
            calculate(updated["tool"], updated["values"])
        await state.update_data(calc_draft=updated)
        await render(bot, chat_id, state)

    @router.callback_query(F.data.startswith("cw:"), flags={"requires_access": True})
    async def action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("calc_draft")
        parts = (callback.data or "").split(":")
        if (
            data.get("mode") != "expert"
            or data.get("paused")
            or not resumable(draft)
            or not draft
            or len(parts) < 3
            or parts[1] != str(draft["revision"])
            or not isinstance(callback.message, Message)
            or callback.message.message_id != draft.get("screen")
        ):
            await callback.answer("Экран устарел. Откройте /calc", show_alert=True)
            return
        command = parts[2]
        if command == "cancel":
            await state.set_state(None)
            await state.update_data(
                calc_draft=None, active_tool=None, paused=True, calc_revision=draft["revision"] + 1
            )
            await next_message(
                callback.message,
                "Расчёт отменён. Сохранённые результаты остались в истории.",
                reply_markup=keyboard(menu_rows()),
            )
        elif (
            command == "platform"
            and draft.get("phase") == "platform"
            and len(parts) == 4
            and parts[3] in {"yandex", "target"}
        ):
            draft["values"]["platform"] = parts[3]
            draft["phase"] = None
            draft["revision"] += 1
            await state.update_data(calc_draft=draft)
            await render(bot, callback.from_user.id, state)
        elif (
            command == "back" and draft["step"] > 0 and draft["step"] < len(SCHEMAS[draft["tool"]])
        ):
            draft = back(draft)
            draft["phase"] = None
            await state.update_data(calc_draft=draft)
            await render(bot, callback.from_user.id, state)
        elif command == "share" and draft["step"] == len(SCHEMAS[draft["tool"]]):
            report = calculate(draft["tool"], draft["values"]).split("\n\nВаши параметры", 1)[0]
            await bot.send_message(
                callback.from_user.id,
                formatted(report),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            await callback.answer(
                "Выберите «Переслать» в меню сообщения с медиапланом и укажите клиента. "
                "Бот не отправляет отчёт клиенту автоматически.",
                show_alert=True,
            )
            return
        elif command == "edit" and draft["step"] == len(SCHEMAS[draft["tool"]]):
            draft.update(step=0, phase=None, revision=draft["revision"] + 1)
            await state.update_data(calc_draft=draft)
            await render(bot, callback.from_user.id, state)
        elif (
            command == "keep"
            and not draft.get("phase")
            and draft["step"] < len(SCHEMAS[draft["tool"]])
            and SCHEMAS[draft["tool"]][draft["step"]].key in draft["values"]
        ):
            await accept(
                bot,
                state,
                callback.from_user.id,
                draft,
                draft["values"][SCHEMAS[draft["tool"]][draft["step"]].key],
            )
        else:
            await callback.answer("Экран устарел", show_alert=True)
            return
        await callback.answer()

    @router.message(
        StateFilter("calc_input", "calc_factors"),
        ~F.text.startswith("/"),
        flags={"requires_access": True},
    )
    async def input_value(message: Message, state: FSMContext, bot: Bot) -> None:
        data = await state.get_data()
        draft = data.get("calc_draft")
        if data.get("mode") != "expert" or data.get("paused"):
            return
        if not resumable(draft) or not draft:
            await state.set_state(None)
            await message.answer("Старый калькулятор заменён. Откройте /calc для нового расчёта.")
            return
        if draft.get("phase") or draft["step"] >= len(SCHEMAS[draft["tool"]]):
            return
        try:
            await accept(bot, state, message.chat.id, draft, message.text or "")
        except ValueError as error:
            draft["revision"] += 1
            await state.update_data(calc_draft=draft)
            await render(bot, message.chat.id, state, str(error))

    @router.callback_query((F.data == "calc:history") | F.data.startswith("cr:"))
    async def history(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if (await state.get_data()).get("mode") != "expert" or not isinstance(
            callback.message, Message
        ):
            await callback.answer("Экран устарел", show_alert=True)
            return
        parts = (callback.data or "").split(":")

        def read() -> tuple[str, list[list[tuple[str, str]]]]:
            with Session(engine) as session:
                owner = session.scalar(
                    select(User.id).where(User.telegram_id == callback.from_user.id)
                )
                if owner is None:
                    raise ObjectUnavailable
                if callback.data == "calc:history":
                    records = list(
                        session.scalars(
                            select(ToolResult)
                            .where(
                                ToolResult.owner_user_id == owner,
                                ToolResult.bot_id == bot.id,
                                ToolResult.tool.in_(LABELS),
                            )
                            .order_by(ToolResult.id.desc())
                            .limit(5)
                        )
                    )
                    return "📚 Сохранённые расчёты" + (
                        "\n\nРасчётов пока нет. Выберите инструмент ниже."
                        if not records
                        else "\n\nПоследние пять результатов; полная история — в профиле."
                    ), [
                        [(f"{LABELS[row.tool]} · #{row.id}", f"cr:open:{row.id}")]
                        for row in records
                    ] + menu_rows()
                if (
                    len(parts) != 3
                    or not parts[2].isdecimal()
                    or parts[1] not in {"open", "export"}
                ):
                    raise ObjectUnavailable
                row = get_result(session, owner, bot.id, int(parts[2]))
                if row.tool not in LABELS:
                    raise ObjectUnavailable
                actions = [
                    [("📄 Скачать .md", f"cr:export:{row.id}")],
                    [("📚 Сохранённые расчёты", "calc:history")],
                    [("🧮 Другой расчёт", "section:calc")],
                    [("🏠 Главное меню", "menu")],
                ]
                if row.tool in SCHEMAS and row.template_version in EDITABLE_VERSIONS:
                    actions.insert(0, [("✏️ Изменить параметры", f"hr:edit:{row.id}")])
                return row.result, actions

        try:
            text, rows = await asyncio.to_thread(read)
        except ObjectUnavailable:
            await callback.answer("Результат недоступен", show_alert=True)
            return
        if parts[0] == "cr" and parts[1] == "export":
            await bot.send_document(
                callback.from_user.id,
                BufferedInputFile(text.encode(), filename=f"calculation-{parts[2]}.md"),
            )
        else:
            await state.update_data(paused=True)
            await next_message(callback.message, text, reply_markup=keyboard(rows))
        await callback.answer()

    return router
