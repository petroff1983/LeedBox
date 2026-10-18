# ruff: noqa: F811 -- pytest fixture injection.
import asyncio
import re
from datetime import UTC, datetime
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_content_agent_postgres import content_fixture, response, tick  # noqa: F401
from test_dzen_agent import ai_fixture  # noqa: F401
from test_lead_magnet_agent import critique, legacy_magnets, magnets
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import ai_tokens
from leedbox.application import content_agent as tasks
from leedbox.application import lead_magnet_agent as app
from leedbox.application.cabinet_access import issue_ticket
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import AIOperation, ContentTask
from leedbox.telegram.agents import build_agent_router
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app

RENDER_SNAPSHOTS = {}
RENDER_ASSETS = {}


@pytest.mark.integration
def test_feedback_rounds_use_latest_ideas_and_keep_original_receipts(content_fixture):
    engine, config, _, owners, bot = content_fixture
    token = ready(content_fixture)
    second_critique = critique()
    second_critique["critiques"][0]["fix"] = "Вторая редакция: только текстовый формат"
    with patch(
        "leedbox.application.lead_magnet_agent.deepseek_generate",
        side_effect=[
            response(magnets()),
            response(critique()),
            response(magnets("Вторая")),
            response(second_critique),
            response(magnets("Третья")),
        ],
    ) as api:
        tick(content_fixture)
        with Session(engine) as db, db.begin():
            row = tasks.owned(db, config, owners[1], bot, token, locked=True)
            original = row.operation_id
            _, stale, _, _ = app.confirmation(db, config, row, True)
            app.feedback(row, "Нужны более простые темы без видео", 101)
            revision = row.revision
            app.feedback(row, "Нужны более простые темы без видео", 101)
            assert row.revision == revision
            with pytest.raises(ValueError, match="изменились"):
                app.launch(db, config, row, stale, True)
            _, digest, _, _ = app.confirmation(db, config, row, True)
            app.launch(db, config, row, digest, True)
            first_revision = row.operation_id
            app.launch(db, config, row, digest, True)
        tick(content_fixture)
        tick(content_fixture)
        with Session(engine) as db, db.begin():
            row = tasks.owned(db, config, owners[1], bot, token, locked=True)
            assert row.status == "completed"
            app.feedback(row, "Оставь только памятки и инструкции", 102)
            offer, digest, _, _ = app.confirmation(db, config, row, True)
            assert offer["initial"]["lead_magnets"][0]["title"].startswith("Вторая")
            app.launch(db, config, row, digest, True)
            second_revision = row.operation_id
        tick(content_fixture)
        tick(content_fixture)
        assert api.call_count == 5
        assert api.call_args_list[1].args[3]["feedback"] == "Нужны более простые темы без видео"
        assert api.call_args_list[4].args[3]["critique"] == second_critique
    with Session(engine) as db:
        assert len({original, first_revision, second_revision}) == 3
        assert db.get(AIOperation, original).result["lead_magnets"][0]["title"].startswith("Первый")
        assert (
            db.get(AIOperation, first_revision)
            .result["lead_magnets"][0]["title"]
            .startswith("Вторая")
        )
        assert (
            db.get(AIOperation, second_revision)
            .result["lead_magnets"][0]["title"]
            .startswith("Третья")
        )
        assert db.get(ContentTask, token).status == "completed"


def ready(fixture, index=1):
    engine, config, actors, owners, bot = fixture
    with Session(engine) as db, db.begin():
        row = app.create(db, config, owners[index], bot, actors[index])
        tasks.change(
            row,
            phase="confirm",
            brief={
                "niche": "Реклама",
                "audience": "Начинающие эксперты",
                "pain": "Как не слить первый бюджет",
                "product": "Консультация",
            },
        )
        _, digest, _, _ = app.confirmation(db, config, row, False)
        app.launch(db, config, row, digest)
        return row.token


@pytest.mark.integration
@pytest.mark.parametrize("critic", [False, True])
@pytest.mark.parametrize("index", [0, 1])
def test_first_five_then_approve_or_automatic_critic_without_double_charge(
    content_fixture, critic, index
):
    engine, config, _, owners, bot = content_fixture
    with Session(engine) as db:
        before = ai_tokens.available(db, owners[index], bot, True)
    token = ready(content_fixture, index)
    with patch(
        "leedbox.application.lead_magnet_agent.deepseek_generate",
        side_effect=[response(magnets()), response(critique()), response(magnets("Улучшенный"))],
    ) as api:
        tick(content_fixture)
        assert tick(content_fixture) is None  # Mandatory human decision, not automatic criticism.
        assert api.call_count == 1
        assert api.call_args.args[4] == 1800
        with Session(engine) as db, db.begin():
            row = tasks.owned(db, config, owners[index], bot, token)
            assert row.status == "draft" and row.payload["phase"] == "lm_review"
            initial_id = row.operation_id
            assert db.get(AIOperation, initial_id).status == "completed"
            _, digest, _, _ = app.confirmation(db, config, row, True)
            if critic:
                app.launch(db, config, row, digest, True)
                app.launch(db, config, row, digest, True)
            else:
                row.status = "completed"
                row.payload = {**row.payload, "approved": True}
        if critic:
            tick(content_fixture)
            tick(content_fixture)
        assert tick(content_fixture) is None
        assert api.call_count == (3 if critic else 1)
        if critic:
            assert [call.args[4] for call in api.call_args_list] == [1800, 1600, 1800]
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        assert row.status == "completed"
        actual = ai_tokens.available(db, owners[index], bot, True)
        assert actual == before - ((900 if critic else 300) if index == 1 else 0)
        initial = db.get(AIOperation, initial_id)
        assert initial.result["lead_magnets"][0]["title"].startswith("Первый")
        assert initial.result["output_kind"] == "ideas"
        assert "content" not in initial.result["lead_magnets"][0]
        if critic:
            final = db.get(AIOperation, row.operation_id)
            assert final.result["version"] == "final"
            assert final.input_tokens == 400 and final.output_tokens == 200
            assert final.result["lead_magnets"][0]["title"].startswith("Улучшенный")
            assert final.result["critique"]["critiques"]
        with pytest.raises(ObjectUnavailable):
            tasks.owned(db, config, owners[2], bot, token)


@pytest.mark.integration
def test_reuse_content_audience_and_failed_critic_keeps_initial_materials(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        previous = tasks.create(db, config, owners[1], bot, actors[1], "manual")
        row = app.create(db, config, owners[1], bot, actors[1])
        assert not app.audience_options(db, row)  # Automatic instructions are not facts about CA.
        tasks.change(previous, brief="Выращивание растений для начинающих, дружелюбно")
        options = app.audience_options(db, row)
        assert options[0]["audience"].startswith("Выращивание")
        stranger = app.create(db, config, owners[2], bot, actors[2])
        assert not app.audience_options(db, stranger)
    token = ready(content_fixture)
    with patch(
        "leedbox.application.lead_magnet_agent.deepseek_generate", return_value=response(magnets())
    ):
        tick(content_fixture)
    with Session(engine) as db, db.begin():
        row = tasks.owned(db, config, owners[1], bot, token)
        before = ai_tokens.available(db, owners[1], bot, True)
        _, digest, _, _ = app.confirmation(db, config, row, True)
        app.launch(db, config, row, digest, True)
    with patch(
        "leedbox.application.lead_magnet_agent.deepseek_generate",
        return_value=response({"critiques": []}),
    ):
        tick(content_fixture)
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        assert row.status == "failed"
        assert row.steps[0]["result"]["lead_magnets"]
        assert ai_tokens.available(db, owners[1], bot, True) == before


@pytest.mark.integration
def test_lm_interview_controls_no_api_until_start(content_fixture):
    engine, config, actors, owners, _ = content_fixture

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        storage = PostgresStorage(engine)
        dp = Dispatcher(storage=storage)
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Synthetic")
        chat = Chat(id=actor.id, type="private")
        state = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await state.set_data({"mode": "expert"})
        sequence, calls = 0, []

        async def request(_bot, method, timeout=None):
            nonlocal sequence
            calls.append(method)
            if isinstance(method, AnswerCallbackQuery):
                return True
            if isinstance(method, SendMessage):
                sequence += 1
                return Message(message_id=sequence, date=datetime.now(UTC), chat=chat)
            return True

        async def feed(value, callback=False):
            nonlocal sequence
            sequence += 1
            message = Message(
                message_id=sequence,
                date=datetime.now(UTC),
                chat=chat,
                from_user=actor,
                text=None if callback else value,
            )
            update = (
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        from_user=actor,
                        chat_instance="test",
                        data=value,
                        message=message,
                    ),
                )
                if callback
                else Update(update_id=sequence, message=message)
            )
            await dp.feed_update(bot, update)

        try:
            with (
                patch.object(bot.session, "make_request", request),
                patch("leedbox.application.lead_magnet_agent.deepseek_generate") as api,
            ):
                await feed("lm:begin", True)
                token = (await state.get_data())["lead_magnet_token"]
                for answer in (
                    "Настройка рекламы",
                    "Начинающие эксперты",
                    "Не знают с чего начать",
                ):
                    await feed(answer)
                await feed(f"lm:{token}:skip", True)
                assert "Подобрать 5" in str(calls[-1].reply_markup)
                api.assert_not_called()
                start = calls[-1].reply_markup.inline_keyboard[0][0].callback_data
                await feed(start, True)
                await feed(start, True)
                with Session(engine) as db:
                    row = tasks.owned(db, config, owners[1], bot.id, token)
                    assert row.status == "queued"
                    assert (
                        len(
                            list(
                                db.scalars(
                                    select(AIOperation).where(
                                        AIOperation.action_key == "lm:" + token
                                    )
                                )
                            )
                        )
                        == 1
                    )
                api.assert_not_called()
                # Completing in the worker must restore the text-feedback FSM state.
                with patch(
                    "leedbox.application.lead_magnet_agent.deepseek_generate",
                    return_value=response(magnets()),
                ):
                    tick(content_fixture)
                from leedbox.telegram.lead_magnet_agent import notify

                await notify(bot, engine, config, token)
                assert await state.get_state() == "agent_lm_feedback"
                await feed("Сделай темы проще, без видео и без обещания роста продаж")
                assert "Ваши пожелания" in calls[-1].text
                assert "Доработать по пожеланиям" in str(calls[-1].reply_markup)
                with Session(engine) as db:
                    row = tasks.owned(db, config, owners[1], bot.id, token)
                    assert row.payload["feedback"].startswith("Сделай темы проще")
                    assert row.status == "draft"
                api.assert_not_called()
                assert "Новая подборка" in str(calls[-1].reply_markup)
                assert "сообщение" in calls[-1].text.casefold()
                await feed(f"lm:{token}:new", True)
                fresh_token = (await state.get_data())["lead_magnet_token"]
                assert fresh_token != token
                assert await state.get_state() == "agent_lm_input"
                await feed(f"lm:{token}:new", True)
                assert (await state.get_data())["lead_magnet_token"] == fresh_token
                with Session(engine) as db:
                    previous = db.get(ContentTask, token)
                    assert previous.payload["feedback"].startswith("Сделай темы проще")
                    assert previous.steps[0]["result"]["lead_magnets"]
                    fresh = db.get(ContentTask, fresh_token)
                    assert fresh.payload["phase"] == "niche" and not fresh.payload["brief"]
                    assert fresh.operation_id is None
                api.assert_not_called()
        finally:
            await state.clear()
            await storage.close()
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_private_lm_web_copy_download_and_isolation(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    token = ready(content_fixture)
    result = magnets()
    result["lead_magnets"][0]["title"] = "Проверка <script>alert(1)</script>"
    with patch(
        "leedbox.application.lead_magnet_agent.deepseek_generate", return_value=response(result)
    ):
        tick(content_fixture)
    with Session(engine) as db, db.begin():
        number = db.get(ContentTask, token).operation_id
        ticket = urlsplit(issue_ticket(db, config, actors[1])).fragment.removeprefix("token=")
    client = create_app(config, engine).test_client()
    client.environ_base["REMOTE_ADDR"] = f"2001:db8::{actors[1] >> 16:x}:{actors[1] & 65535:x}"
    assert client.get(f"/cabinet/agents/{number}").status_code == 302
    login = client.get("/cabinet/login?next=agents")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', login.text)[1]
    assert (
        client.post(
            "/cabinet/login", data={"csrf": csrf, "token": ticket, "next": "agents"}
        ).status_code
        == 303
    )
    page = client.get(f"/cabinet/agents/{number}")
    assert page.status_code == 200
    assert "Скопировать идею" in page.text and "Пять тем для лид-магнитов" in page.text
    assert "Содержание материала" not in page.text and "Анонс получения" not in page.text
    assert "<script>alert(1)</script>" not in page.text
    download = client.get(f"/cabinet/agents/{number}/prompt")
    assert download.status_code == 200 and "# Пять тем для лид-магнитов" in download.text
    with Session(engine) as db, db.begin():
        foreign = ai_tokens.grant(db, owners[2], bot, True, "foreign-lm", 100).id
    assert client.get(f"/cabinet/agents/{foreign}").status_code == 404
    RENDER_SNAPSHOTS["lead-magnets"] = page.get_data()
    for path in re.findall(rb'(?:src|href)="(/static/[^"?]+)', page.get_data()):
        asset = client.get(path.decode())
        RENDER_ASSETS[path.decode()] = (asset.get_data(), asset.content_type)


@pytest.mark.integration
def test_lm_notification_does_not_leak_into_recipient_dialog(content_fixture):
    from leedbox.telegram.content_agent import notify

    engine, config, actors, _, _ = content_fixture
    token = ready(content_fixture)
    with patch(
        "leedbox.application.lead_magnet_agent.deepseek_generate", return_value=response(magnets())
    ):
        tick(content_fixture)
    with Session(engine) as db, db.begin():
        db.get(ContentTask, token).screen_id = 123

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        storage = PostgresStorage(engine)
        dp = Dispatcher(storage=storage)
        state = dp.fsm.get_context(bot=bot, chat_id=actors[1], user_id=actors[1])
        await state.set_data(
            {"mode": "recipient", "active_tool": "lead_magnet_agent", "lead_magnet_token": token}
        )
        try:
            with patch.object(bot.session, "make_request") as request:
                await notify(bot, engine, config, token)
                request.assert_not_called()
        finally:
            await state.clear()
            await storage.close()
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_saved_dzen_audience_is_owner_scoped(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        operation = ai_tokens.reserve(
            db,
            owners[1],
            bot,
            True,
            "saved-dzen-audience",
            1000,
            {
                "purpose": "writing_profile",
                "model": "deepseek-flash",
                "inputs": {
                    "brief": {"topic": "Домашние растения", "audience": "Начинающие цветоводы"}
                },
            },
        )
        operation.status = "completed"
        # Release the synthetic fixture reservation without invoking an external API.
        ai_tokens.release_reservation(ai_tokens.account(db, owners[1], bot, True), operation)
        row = app.create(db, config, owners[1], bot, actors[1])
        options = app.audience_options(db, row)
        assert options[0]["niche"] == "Домашние растения"
        assert options[0]["audience"] == "Начинающие цветоводы"
        stranger = app.create(db, config, owners[2], bot, actors[2])
        assert not app.audience_options(db, stranger)


@pytest.mark.integration
def test_legacy_inflight_result_and_new_critic_keep_original_history(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    token = ready(content_fixture)
    with Session(engine) as db, db.begin():
        row = db.get(ContentTask, token)
        old_offer = dict(row.payload["offer"])
        for key in ("output_kind", "limits", "initial"):
            old_offer.pop(key)
        old_offer["systems"] = ["Историческая инструкция с полным содержанием"]
        old_offer["maximum"] = 20000
        op = db.get(AIOperation, row.operation_id)
        funds = ai_tokens.account(db, owners[1], bot, True)
        funds.reserved += old_offer["maximum"] - op.reserve_tokens
        op.reserve_tokens = old_offer["maximum"]
        op.snapshot = old_offer
        row.payload = {**row.payload, "offer": old_offer}
    with patch(
        "leedbox.application.lead_magnet_agent.deepseek_generate",
        return_value=response(legacy_magnets()),
    ) as api:
        tick(content_fixture)
        assert api.call_args.args[4] == 5500  # Honor a previously accepted quote.
    with Session(engine) as db, db.begin():
        row = db.get(ContentTask, token)
        initial_id = row.operation_id
        assert row.steps[0]["result"]["lead_magnets"][0]["content"]
        offer, digest, _, _ = app.confirmation(db, config, row, True)
        assert "content" not in offer["initial"]["lead_magnets"][0]
        app.launch(db, config, row, digest, True)
    with patch(
        "leedbox.application.lead_magnet_agent.deepseek_generate",
        side_effect=[response(critique()), response(magnets("Улучшенный"))],
    ) as api:
        tick(content_fixture)
        tick(content_fixture)
        assert "content" not in api.call_args_list[0].args[3]["initial"]["lead_magnets"][0]
    with Session(engine) as db, db.begin():
        row = db.get(ContentTask, token)
        assert row.status == "completed"
        assert db.get(AIOperation, initial_id).result["lead_magnets"][0]["content"]
        assert db.get(AIOperation, row.operation_id).result["output_kind"] == "ideas"
        ticket = urlsplit(issue_ticket(db, config, actors[1])).fragment.removeprefix("token=")
    client = create_app(config, engine).test_client()
    client.environ_base["REMOTE_ADDR"] = f"2001:db8::{actors[1] >> 16:x}:{actors[1] & 65535:x}"
    login = client.get("/cabinet/login?next=agents")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', login.text)[1]
    client.post("/cabinet/login", data={"csrf": csrf, "token": ticket, "next": "agents"})
    old_page = client.get(f"/cabinet/agents/{initial_id}")
    assert old_page.status_code == 200 and "Содержание материала" in old_page.text
    assert "Пункт 0" in client.get(f"/cabinet/agents/{initial_id}/prompt").text
