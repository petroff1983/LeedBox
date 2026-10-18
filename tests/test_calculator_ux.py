"""Mock Telegram only; persisted PostgreSQL drafts and owner-scoped results."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import test_links_postgres
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendDocument,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leedbox.application.results import save_result
from leedbox.infrastructure.models import ToolResult
from leedbox.telegram.calculators import VERSION
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage

link_fixture = test_links_postgres.link_fixture


@pytest.mark.integration
@pytest.mark.parametrize("bot_username", ["fixture_bot", None])
@pytest.mark.parametrize(
    "tool,answers,expected",
    [
        ("ads", ["30000", "1000", "33%", "10%", "15"], "16 363,64 ₽"),
        ("tg_subscribers", ["5000", "0", "2%", "10%", "50000", "6%"], "470 000,00 ₽"),
        ("tg_budget", ["1000", "300", "10", "20", "50000", "6"], "1 920,00 ₽"),
        ("tg_goal", ["100000", "300", "10", "20", "50000", "6"], "100 480,00 ₽"),
        ("tg_goal", ["100000", "1000", "10", "20", "50000", "6"], "цель пока недостижима"),
    ],
)
def test_two_planning_flows_resume_export_and_owner_isolation(
    link_fixture, tool, answers, expected, bot_username
):
    engine, config, actors, owners, projects = link_fixture

    async def run():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        bot_user = User(id=bot.id, is_bot=True, first_name="Synthetic", username=bot_username)
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        state = dispatcher.fsm.get_context(bot=bot, chat_id=actors[0], user_id=actors[0])
        calls, screen, sequence = [], 1000, 0

        async def api(_bot, method, timeout=None):
            nonlocal screen
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            screen += 1
            return Message(
                message_id=screen,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def message(text, actor=actors[0]):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    message=Message(
                        message_id=sequence,
                        date=datetime.now(UTC),
                        chat=Chat(id=actor, type="private"),
                        from_user=User(id=actor, is_bot=False, first_name="Expert"),
                        text=text,
                    ),
                ),
            )

        async def callback(code, screen_id=None, actor=actors[0]):
            nonlocal sequence
            sequence += 1
            await dispatcher.feed_update(
                bot,
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        chat_instance="fixture",
                        data=code,
                        from_user=User(id=actor, is_bot=False, first_name="Expert"),
                        message=Message(
                            message_id=screen_id or screen,
                            date=datetime.now(UTC),
                            chat=Chat(id=actor, type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        async def action(command):
            draft = (await state.get_data())["calc_draft"]
            await callback(f"cw:{draft['revision']}:{command}", draft["screen"])

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await message("/start")
                await callback("section:calc")
                assert "calc:new:funnel" not in str(calls[-2].reply_markup)
                await callback("calc:new:" + tool)
                if tool == "ads":
                    await action("platform:yandex")
                initial = (await state.get_data())["calc_draft"]
                await message("not a number")
                draft = (await state.get_data())["calc_draft"]
                assert draft["step"] == 0 and "⚠️" in calls[-1].text
                await message(answers[0])
                old = (await state.get_data())["calc_draft"]
                await message("/calc")
                menu_screen = (await state.get_data())["calc_menu_screen"]
                before_resume = len(calls)
                await callback("calc:resume", menu_screen)
                assert any(
                    isinstance(call, EditMessageReplyMarkup) and call.message_id == menu_screen
                    for call in calls[before_resume:]
                )
                key = StorageKey(bot.id, actors[0], actors[0])
                fresh_storage = PostgresStorage(engine)
                assert (await fresh_storage.get_data(key))["calc_draft"]["step"] == 1
                await message("/menu")
                different_tool = "tg_goal" if tool == "ads" else "ads"
                await callback("calc:new:" + different_tool)
                choice = [call for call in calls if isinstance(call, SendMessage)][-1]
                assert "расчёт будет заменён" in choice.text
                assert "calc:resume" in str(choice.reply_markup)
                assert f"calc:restart:{different_tool}" in str(choice.reply_markup)
                assert (await state.get_data())["calc_draft"]["values"] == old["values"]
                await message("123456789")
                assert (await state.get_data())["calc_draft"]["step"] == 1
                await callback("calc:resume")
                restored = await state.get_data()
                assert restored["calc_draft"]["tool"] == tool
                assert restored["calc_draft"]["values"] == old["values"]
                assert restored["active_tool"] == "calc" and not restored["paused"]
                await callback(f"cw:{old['revision']}:back", old["screen"])
                assert isinstance(calls[-1], AnswerCallbackQuery) and calls[-1].show_alert
                assert (await state.get_data())["calc_draft"]["step"] == 1
                for answer in answers[1:]:
                    await message(answer)
                draft = (await state.get_data())["calc_draft"]
                result_id = draft["result_id"]
                assert expected in calls[-1].text and calls[-1].parse_mode == "HTML"
                assert calls[-1].text.startswith(("<b>📊", "<b>📱"))
                assert "Как считаем" not in calls[-1].text
                buttons = [
                    button for row in calls[-1].reply_markup.inline_keyboard for button in row
                ]
                share = next(button for button in buttons if button.text == "📤 Отправить клиенту")
                if bot_username:
                    assert share.url is not None
                    shared_text = parse_qs(urlsplit(share.url).query)["text"][0]
                    assert shared_text.startswith(("📊", "📱"))
                    assert "Ваши параметры" not in shared_text and "Как считаем" not in shared_text
                else:
                    assert share.url is None and share.callback_data.endswith(":share")
                    await action("share")
                    assert isinstance(calls[-2], SendMessage)
                    assert calls[-2].chat_id == actors[0] and calls[-2].reply_markup is None
                    assert "Как считаем" not in calls[-2].text
                    assert calls[-1].show_alert and "Переслать" in calls[-1].text
                    before_stale = len(calls)
                    await callback(f"cw:{draft['revision']}:share", draft["screen"] + 999)
                    assert calls[-1].show_alert
                    assert not any(isinstance(call, SendMessage) for call in calls[before_stale:])
                assert any(button.text == "📁 В историю" for button in buttons)
                # Details are behind a separate button; sharing has no service copy.
                assert not any(isinstance(call, EditMessageText) for call in calls)
                assert any(isinstance(call, EditMessageReplyMarkup) for call in calls)
                await callback("calc:resume")
                assert (await state.get_data())["calc_draft"]["result_id"] == result_id
                with Session(engine) as session:
                    row = session.get(ToolResult, result_id)
                    assert row.owner_user_id == owners[0] and row.template_version == VERSION
                    assert "Как считаем" in row.result and "<b>" not in row.result
                    assert (
                        session.scalar(
                            select(func.count())
                            .select_from(ToolResult)
                            .where(ToolResult.owner_user_id == owners[0], ToolResult.tool == tool)
                        )
                        == 1
                    )
                await callback(f"cr:export:{result_id}")
                assert isinstance(calls[-2], SendDocument)
                assert not (await state.get_data()).get("paused")
                await action("edit")
                assert (await state.get_data())["calc_draft"]["step"] == 0
                await callback(f"cw:{initial['revision']}:cancel", initial["screen"])
                assert calls[-1].show_alert
                await message("/start", actor=actors[1])
                await callback(f"cr:export:{result_id}", actor=actors[1])
                assert isinstance(calls[-1], AnswerCallbackQuery) and calls[-1].show_alert
                await message("/start lm_missing", actor=actors[2])
                before = len([call for call in calls if isinstance(call, SendMessage)])
                await callback("calc:new:ads", actor=actors[2])
                assert len([call for call in calls if isinstance(call, SendMessage)]) == before
                # Old result remains readable, but obsolete buttons never launch an old wizard.
                with Session(engine) as session, session.begin():
                    legacy_id = save_result(
                        session,
                        owners[0],
                        projects[0],
                        bot.id,
                        "campaign",
                        {},
                        "Old saved result",
                        f"legacy-test:{owners[0]}",
                    ).id
                await callback("calc:new:funnel")
                assert "заменён" in calls[-2].text
                await callback(f"cr:open:{legacy_id}")
                assert calls[-2].text == "Old saved result"
                await callback(f"hr:edit:{legacy_id}")
                assert calls[-1].show_alert
        finally:
            await bot.session.close()

    asyncio.run(run())


@pytest.mark.integration
def test_zero_subscribers_start_goal_planning_immediately(link_fixture):
    engine, config, actors, _, _ = link_fixture

    async def run():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        storage = PostgresStorage(engine)
        state = Dispatcher(storage=storage).fsm.get_context(
            bot=bot, chat_id=actors[0], user_id=actors[0]
        )
        router = build_router(engine, config)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(router)
        calls = []
        bot_user = User(id=bot.id, is_bot=True, first_name="Synthetic", username="fixture_bot")

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            return Message(
                message_id=len(calls),
                date=datetime.now(UTC),
                chat=Chat(id=actors[0], type="private"),
                from_user=bot_user,
            )

        user = User(id=actors[0], is_bot=False, first_name="Expert")
        chat = Chat(id=actors[0], type="private")
        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await state.update_data(mode="expert")
                await dispatcher.feed_update(
                    bot,
                    Update(
                        update_id=1,
                        callback_query=CallbackQuery(
                            id="1",
                            chat_instance="fixture",
                            data="calc:new:tg_subscribers",
                            from_user=user,
                            message=Message(
                                message_id=1, date=datetime.now(UTC), chat=chat, from_user=bot_user
                            ),
                        ),
                    ),
                )
                await dispatcher.feed_update(
                    bot,
                    Update(
                        update_id=2,
                        message=Message(
                            message_id=2,
                            date=datetime.now(UTC),
                            chat=chat,
                            from_user=user,
                            text="0",
                        ),
                    ),
                )
                assert "Финансовая цель запуска" in calls[-1].text
                draft = (await state.get_data())["calc_draft"]
                assert draft["tool"] == "tg_goal" and draft["step"] == 0
                assert draft["values"] == {}
                for sequence, answer in enumerate(["100000", "150", "5", "30", "50000", "6"], 3):
                    await dispatcher.feed_update(
                        bot,
                        Update(
                            update_id=sequence,
                            message=Message(
                                message_id=sequence,
                                date=datetime.now(UTC),
                                chat=chat,
                                from_user=user,
                                text=answer,
                            ),
                        ),
                    )
                assert "100 455,00 ₽" in calls[-1].text and "27 150,00 ₽" in calls[-1].text
                assert "181 подписчиков" in calls[-1].text
        finally:
            await bot.session.close()

    asyncio.run(run())
