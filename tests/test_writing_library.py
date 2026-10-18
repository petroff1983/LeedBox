# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name.
import asyncio
import re
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.orm import Session
from test_dzen_agent import ai_fixture, writing_brief, writing_result  # noqa: F401, F811
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import agents, ai_tokens, writing_library
from leedbox.application.cabinet_access import issue_ticket
from leedbox.domain.access import ObjectUnavailable
from leedbox.telegram.agents import build_agent_router
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app


def seed(engine, owner, bot, count=3):
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owner, bot, True, "library-fixture-grant", 50000)
        for index in range(count):
            brief = {**writing_brief(), "topic": f"Тематика {index + 1}"}
            offer = agents.quote_profile(db, brief)
            row = ai_tokens.reserve(
                db, owner, bot, True, f"library-{index}", offer["maximum"], offer
            )
            ai_tokens.settle(
                db,
                owner,
                bot,
                True,
                row.id,
                dict(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                {"prompt": writing_result()["prompt"] + f"\nУникальный текст {index + 1}"},
                True,
            )
        writing_library.ensure(db, owner, bot, True)
        return [(p.id, p.operation_id, p.name) for p in writing_library.page(db, owner, bot, True)]


@pytest.mark.integration
def test_library_scope_pagination_rename_archive(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    items = seed(engine, owners[1], bot, 8)
    with patch("leedbox.application.agents.deepseek_generate") as api:
        with Session(engine) as db, db.begin():
            before = ai_tokens.available(db, owners[1], bot, True)
            assert len(items) == 8
            assert len(writing_library.page(db, owners[1], bot, True, 1)) == 1
            assert writing_library.page(db, owners[2], bot, True) == []
            for owner, scoped_bot, test in [
                (owners[2], bot, True),
                (owners[1], bot + 1, True),
                (owners[1], bot, False),
            ]:
                with pytest.raises(ObjectUnavailable):
                    writing_library.get(db, owner, scoped_bot, test, items[0][0])
            writing_library.rename(db, owners[1], bot, True, items[0][0], "  Мой   сад ")
            assert writing_library.get(db, owners[1], bot, True, items[0][0]).name == "Мой сад"
            for invalid in ["", "x" * 81]:
                with pytest.raises(ValueError):
                    writing_library.rename(db, owners[1], bot, True, items[0][0], invalid)
            writing_library.get(db, owners[1], bot, True, items[0][0]).archived = True
            db.flush()
            writing_library.ensure(db, owners[1], bot, True)
            assert len(writing_library.page(db, owners[1], bot, True)) == 7
            assert (
                agents.operation(db, owners[1], bot, True, items[0][1])
                .result["prompt"]
                .endswith("Уникальный текст 8")
            )
            assert ai_tokens.available(db, owners[1], bot, True) == before
        api.assert_not_called()


@pytest.mark.integration
def test_library_telegram_selects_exact_prompt_without_api(ai_fixture):
    engine, config, actors, owners, ledger_bot = ai_fixture
    ledger_bot = int(config.bot_token.get_secret_value().split(":")[0])
    items = seed(engine, owners[1], ledger_bot, 8)
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        assert bot.id == ledger_bot
        dp = Dispatcher(storage=PostgresStorage(engine))
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Fixture")
        chat = Chat(id=actor.id, type="private")
        message = Message(
            message_id=1, date=datetime.now(UTC), chat=chat, from_user=actor, text="test"
        )
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await context.set_data(dict(mode="expert", active_tool="agents", paused=False))
        update = 0

        async def click(value):
            nonlocal update
            update += 1
            await dp.feed_update(
                bot,
                Update(
                    update_id=update,
                    callback_query=CallbackQuery(
                        id=str(update),
                        from_user=actor,
                        chat_instance="1",
                        data=value,
                        message=message,
                    ),
                ),
            )

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, (SendMessage, SendDocument)):
                sent.append(method)
            return True

        with patch.object(bot.session, "make_request", side_effect=fake_request):
            await click("wp:open")
            buttons = [b for row in sent[-1].reply_markup.inline_keyboard for b in row]
            assert sum((b.callback_data or "").startswith("wp:view:") for b in buttons) == 7
            assert "Уникальный текст" not in sent[-1].text
            await click("wp:page:1")
            await click(f"wp:view:{items[-1][0]}")
            assert "Тематика 1" in sent[-1].text
            assert any(
                b.callback_data == "wp:page:1"
                for row in sent[-1].reply_markup.inline_keyboard
                for b in row
            )
            assert any(
                b.callback_data == f"ait:report:{items[-1][1]}"
                for row in sent[-1].reply_markup.inline_keyboard
                for b in row
            )
            await click(f"wp:download:{items[-1][0]}")
            assert sent[-1].document.data.decode().endswith("Уникальный текст 1")
            await click("wp:edit")
            assert (await context.get_data())["writing_draft"]["brief"] == {}
            assert await context.get_state() == "agent_writing_profile"
            await click("wp:open")
            assert await context.get_state() is None
            assert any(
                b.callback_data == "wp:resume"
                for row in sent[-1].reply_markup.inline_keyboard
                for b in row
            )
            await click("wp:resume")
            assert await context.get_state() == "agent_writing_profile"
            await context.clear()
        await bot.session.close()
        await dp.storage.close()

    with patch("leedbox.application.agents.deepseek_generate") as api:
        asyncio.run(scenario())
        api.assert_not_called()


@pytest.mark.integration
def test_web_library_exact_report_login_and_no_auto_latest(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    items = seed(engine, owners[1], bot, 8)
    with Session(engine) as db, db.begin():
        ticket = urlsplit(issue_ticket(db, config, actors[1])).fragment.removeprefix("token=")
    client = create_app(config, engine).test_client()
    page = client.get(f"/cabinet/login?next=agents&report={items[-1][1]}")
    assert f'name="report" value="{items[-1][1]}"' in page.text
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)[1]
    response = client.post(
        "/cabinet/login", data=dict(csrf=csrf, token=ticket, next="agents", report=items[-1][1])
    )
    assert response.location == f"/cabinet/agents/{items[-1][1]}"
    with patch("leedbox.application.agents.deepseek_generate") as api:
        report = client.get(response.location)
        assert "Уникальный текст 1" in report.text
        assert "Уникальный текст 8" not in report.text
        page = client.get("/cabinet/agents")
        assert 'id="my-prompts"' in page.text
        assert "Следующие" in page.text
        assert "Уникальный текст" not in page.text
        assert "Тематика 1" in client.get("/cabinet/agents?prompts_page=1").text
        assert client.get("/cabinet/agents?prompts_page=-1").status_code == 400
        assert client.get("/cabinet/agents?prompts_page=bad").status_code == 400
        api.assert_not_called()
