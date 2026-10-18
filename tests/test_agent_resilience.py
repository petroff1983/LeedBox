# ruff: noqa: F811 -- pytest injects imported fixtures by parameter name.
import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.orm import Session
from test_dzen_agent import ai_fixture, structured_writing_result, writing_brief  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import agents, ai_tokens
from leedbox.infrastructure.agents import AgentError
from leedbox.telegram.agents import build_agent_router
from leedbox.telegram.storage import PostgresStorage


@pytest.mark.integration
@pytest.mark.parametrize("unknown", [False, True])
def test_writing_expired_callback_failure_resume_and_unknown_no_retry(ai_fixture, unknown):
    engine, config, actors, owners, _ = ai_fixture
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        with Session(engine) as db, db.begin():
            ai_tokens.grant(db, owners[1], bot.id, True, "writing-resilience", 50000)
            offer = agents.quote_profile(db, writing_brief())
            initial_balance = ai_tokens.available(db, owners[1], bot.id, True)
        dp = Dispatcher(storage=PostgresStorage(engine))
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Fixture")
        chat = Chat(id=actor.id, type="private")
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        draft = dict(
            phase="confirm",
            token="old",
            index=7,
            brief=writing_brief(),
            offer={**offer, "version": 1},
        )
        await context.set_data(
            dict(mode="expert", active_tool="agents", paused=False, writing_draft=draft)
        )

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, AnswerCallbackQuery):
                raise TelegramBadRequest(method=method, message="query is too old")
            if isinstance(method, SendMessage):
                sent.append(method)
            return True

        async def click(code):
            await dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    callback_query=CallbackQuery(
                        id="fixture",
                        from_user=actor,
                        chat_instance="1",
                        data=code,
                        message=Message(
                            message_id=1,
                            date=datetime.now(UTC),
                            chat=chat,
                            from_user=actor,
                            text="fixture",
                        ),
                    ),
                ),
            )

        def payload(value):
            return {
                "usage": dict(prompt_tokens=900, completion_tokens=1700, total_tokens=2600),
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
            }

        first = AgentError("fixture timeout") if unknown else payload({"sections": {}})
        with (
            patch.object(bot.session, "make_request", side_effect=fake_request),
            patch(
                "leedbox.application.agents.deepseek_generate",
                side_effect=[first, payload(structured_writing_result())],
            ) as api,
        ):
            await click("wp:resume")
            current = (await context.get_data())["writing_draft"]
            assert current["offer"]["version"] == 2 and current["token"] != "old"
            api.assert_not_called()
            await click("wp:generate:" + current["token"])
            api.assert_called_once()
            assert sent[-1].text.startswith("⚠️ ")
            failed = (await context.get_data())["writing_draft"]
            assert failed["phase"] == "confirm" and failed["brief"] == writing_brief()
            with Session(engine) as db:
                row = ai_tokens.previous(db, owners[1], bot.id, True, "writing:" + current["token"])
                assert row.status == ("uncertain" if unknown else "failed")
                assert ai_tokens.available(db, owners[1], bot.id, True) == (
                    initial_balance - offer["maximum"] if unknown else initial_balance
                )
            await click("wp:resume")
            assert api.call_count == 1
            resumed = (await context.get_data())["writing_draft"]
            await click("wp:generate:" + resumed["token"])
            if unknown:
                api.assert_called_once()
                assert resumed["token"] == current["token"]
            else:
                assert api.call_count == 2
                ready = (await context.get_data())["writing_draft"]
                assert ready["phase"] == "ready"
                with Session(engine) as db:
                    assert (
                        ai_tokens.available(db, owners[1], bot.id, True) == initial_balance - 2600
                    )
            await context.clear()
        await bot.session.close()
        await dp.storage.close()

    asyncio.run(scenario())
