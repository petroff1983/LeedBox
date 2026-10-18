# ruff: noqa: F811 -- pytest fixture injection.
import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_content_agent import plan, posts, source_row
from test_dzen_agent import ai_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import ai_tokens
from leedbox.application import content_agent as app
from leedbox.application.cabinet_access import issue_ticket
from leedbox.application.settings import set_setting
from leedbox.application.versions import fingerprint
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.agents import AgentError
from leedbox.infrastructure.models import AIOperation, ContentTask, Setting
from leedbox.telegram.agents import build_agent_router
from leedbox.telegram.content_agent import notify
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app

RENDER_SNAPSHOTS = {}
RENDER_ASSETS = {}


@pytest.mark.integration
@pytest.mark.parametrize("days", [7, 30, 90, 365, 14])
def test_period_is_explicit_validated_and_stale_selection_is_free(content_fixture, days):
    engine, config, actors, owners, bot = content_fixture
    with Session(engine) as db, db.begin():
        row = app.create(db, config, owners[1], bot, actors[1], "link")
        app.change(row, url="https://t.me/example", source_platform="telegram")
        offer = app.quote(db, row)
        digest = fingerprint({**offer, "billing": "balance"})[:12]
        with pytest.raises(ValueError, match="выберите период"):
            app.launch(db, config, owners[1], bot, row.token, digest)
        for invalid in (0, 366):
            with pytest.raises(ValueError, match="365"):
                app.period_launch(db, config, row, invalid, digest)
        app.change(row, url="https://t.me/another")
        with pytest.raises(ValueError, match="изменились"):
            app.period_launch(db, config, row, days, digest)
        assert row.operation_id is None
        offer = app.quote(db, row)
        digest = fingerprint({**offer, "billing": "balance"})[:12]
        app.period_launch(db, config, row, days, digest)
        number = row.operation_id
        app.period_launch(db, config, row, days, digest)
        assert row.operation_id == number
        assert row.payload["offer"]["days"] == days and row.status == "queued"


@pytest.fixture
def content_fixture(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    config = config.model_copy(update={"apify_api_token": SecretStr("fixture-apify")})
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owners[1], bot, True, "content-fixture", 300000)
    try:
        yield engine, config, actors, owners, bot
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(ContentTask).where(ContentTask.owner_user_id.in_(owners)))


def ready(fixture, mode="manual", target="telegram", index=1, legacy_link=True):
    engine, config, actors, owners, bot = fixture
    with Session(engine) as db, db.begin():
        row = app.create(db, config, owners[index], bot, actors[index], mode)
        app.change(
            row,
            target=target,
            brief="Комнатные растения для начинающих, дружелюбно",
            phase="materials",
            patterns_version=1,
            workflow="legacy" if mode == "link" and legacy_link else row.payload["workflow"],
            period_selected=mode == "link",
        )
        if mode == "manual":
            app.add_part(
                row, 1, "Первый исходный пост для анализа, без вымышленных результатов. " * 5
            )
        else:
            app.change(row, source_platform="telegram", url="https://t.me/example")
        offer = app.quote(db, row)
        billing = ai_tokens.funding(db, config, owners[index], bot, True, offer)
        confirm = fingerprint({**offer, "billing": billing})[:12]
        token = row.token
    return token, confirm


def tick(fixture):
    engine, config, _, _, bot = fixture
    with Session(engine) as db, db.begin():
        for row in db.scalars(
            select(ContentTask).where(
                ContentTask.bot_id == bot, ContentTask.status.in_({"queued", "waiting"})
            )
        ):
            row.ready_at = datetime.now(UTC)
    token = app.claim(engine, config, bot)
    if token:
        app.advance(engine, config, token)
    return token


def response(value):
    return {
        "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}
        ],
    }


def start(fixture, token, confirm, index=1):
    engine, config, _, owners, bot = fixture
    with Session(engine) as db, db.begin():
        return app.launch(db, config, owners[index], bot, token, confirm).operation_id


@pytest.mark.integration
@pytest.mark.parametrize("index", [0, 1])
def test_automatic_pipeline_owner_and_customer_resume_exact_billing(content_fixture, index):
    engine, config, _, owners, bot = content_fixture
    token, confirmation = ready(content_fixture, index=index)
    with Session(engine) as db:
        before = ai_tokens.available(db, owners[index], bot, True)
    number = start(content_fixture, token, confirmation, index=index)
    assert start(content_fixture, token, confirmation, index=index) == number
    with (
        patch(
            "leedbox.application.content_agent.deepseek_generate",
            side_effect=[
                response(plan()),
                response(posts("Черновик", long=True)),
                response(posts("Итог", long=True)),
            ],
        ) as api,
        patch("leedbox.infrastructure.apify.start") as scrape,
    ):
        for stage in range(3):
            assert tick(content_fixture) == token
            with Session(engine) as db:
                row = db.get(ContentTask, token)
                assert len(row.steps) == stage + 1
                if stage < 2:
                    assert row.status == "queued"
        assert tick(content_fixture) is None
        assert api.call_count == 3
        scrape.assert_not_called()
        assert "sources" in api.call_args_list[0].args[3]
        assert "sources" not in api.call_args_list[1].args[3]
        assert "drafts" in api.call_args_list[2].args[3]
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        op = db.get(AIOperation, number)
        assert row.status == op.status == "completed"
        assert "Итог" in op.result["posts"][0]["parts"][0]
        assert "Черновик" not in json.dumps(op.result, ensure_ascii=False)
        assert op.input_tokens == 600 and op.output_tokens == 300
        assert ai_tokens.available(db, owners[index], bot, True) == before - (
            900 if index == 1 else 0
        )
        assert all(len(item["parts"]) == 1 for item in op.result["posts"])
        assert any(len(item["parts"][0]) > 500 for item in op.result["posts"])
        assert len(op.result["sources"]) == 1 and op.result["analysis"]["topics"]
        with pytest.raises(ObjectUnavailable):
            app.owned(db, config, owners[2], bot, token)
        with pytest.raises(ObjectUnavailable):
            app.owned(db, config, owners[index], bot + 1, token)


@pytest.mark.integration
def test_collecting_duplicate_messages_revision_and_price_fence(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    token, confirm = ready(content_fixture)
    with Session(engine) as db, db.begin():
        row = app.owned(db, config, owners[1], bot, token, locked=True)
        app.add_part(row, 2, "Ещё одна часть длинной публикации с продолжением мысли.")
        app.add_part(row, 2, "Дубликат не должен добавиться")
        assert len(row.payload["parts"]) == 2
    with Session(engine) as db, db.begin(), pytest.raises(ValueError, match="изменились"):
        app.launch(db, config, owners[1], bot, token, confirm)
    with Session(engine) as db, db.begin():
        assert app.create(db, config, owners[1], bot, actors[1], "manual").token == token
        row = app.owned(db, config, owners[1], bot, token)
        old = list(row.payload["parts"])
        with pytest.raises(ValueError):
            app.add_part(row, 3, "Очень длинно " * 20000)
        assert row.payload["parts"] == old
        app.cancel(db, row)
        assert row.status == "cancelled"


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["invalid", "timeout", "disabled"])
def test_failure_never_charges_bad_generation_or_replays_unknown(content_fixture, failure):
    engine, config, actors, owners, bot = content_fixture
    token, confirm = ready(content_fixture)
    with Session(engine) as db:
        before = ai_tokens.available(db, owners[1], bot, True)
    number = start(content_fixture, token, confirm)
    if failure == "disabled":
        with Session(engine) as db, db.begin():
            set_setting(db, config, actors[0], "ai_content_enabled", False)
    try:
        with patch(
            "leedbox.application.content_agent.deepseek_generate",
            side_effect=AgentError("timeout") if failure == "timeout" else None,
            return_value=response({"patterns": []}),
        ) as api:
            tick(content_fixture)
            assert tick(content_fixture) is None
            assert api.call_count == (0 if failure == "disabled" else 1)
        with Session(engine) as db:
            row = db.get(ContentTask, token)
            op = db.get(AIOperation, number)
            assert row.status == ("uncertain" if failure == "timeout" else "failed")
            assert op.status == row.status
            available = ai_tokens.available(db, owners[1], bot, True)
            assert available == before - (op.reserve_tokens if failure == "timeout" else 0)
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(Setting).where(Setting.key == "ai_content_enabled"))


@pytest.mark.integration
def test_stale_inflight_becomes_uncertain_without_repeating_api(content_fixture):
    engine, config, _, _, bot = content_fixture
    token, confirm = ready(content_fixture)
    start(content_fixture, token, confirm)
    assert app.claim(engine, config, bot) == token
    with Session(engine) as db, db.begin():
        db.get(ContentTask, token).locked_at = datetime.now(UTC) - timedelta(minutes=11)
    with patch("leedbox.application.content_agent.deepseek_generate") as api:
        assert app.claim(engine, config, bot) is None
        api.assert_not_called()
    with Session(engine) as db:
        assert db.get(ContentTask, token).status == "uncertain"


@pytest.mark.integration
def test_link_telegram_collects_once_then_automatic_generation(content_fixture):
    engine, config, _, owners, bot = content_fixture
    token, confirm = ready(content_fixture, mode="link")
    number = start(content_fixture, token, confirm)
    source = source_row(views=100)
    with (
        patch(
            "leedbox.application.content_agent.apify.start", return_value={"id": "syntheticRun123"}
        ) as run,
        patch(
            "leedbox.application.content_agent.apify.poll",
            return_value={
                "status": "SUCCEEDED",
                "defaultDatasetId": "syntheticDataset123",
                "usageTotalUsd": 0.01,
            },
        ),
        patch("leedbox.application.content_agent.apify.items", return_value=[source]),
        patch(
            "leedbox.application.content_agent.deepseek_generate",
            side_effect=[
                response(plan([source["link"]])),
                response(posts("Черновик")),
                response(posts("Итог")),
            ],
        ) as api,
    ):
        for _ in range(5):
            tick(content_fixture)
        assert run.call_count == 1 and api.call_count == 3
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        assert row.status == "completed"
        search = db.get(AIOperation, row.search_operation_id)
        assert search.status == "completed" and search.kind == "search"
        assert search.reserve_tokens == 1000 and str(search.cost_usd).startswith("0.01")
        assert db.get(AIOperation, number).input_tokens == 600


@pytest.mark.integration
def test_private_web_posts_copy_export_and_isolation(content_fixture):
    engine, config, actors, owners, bot = content_fixture
    token, confirm = ready(content_fixture)
    number = start(content_fixture, token, confirm)
    with patch(
        "leedbox.application.content_agent.deepseek_generate",
        side_effect=[
            response(plan()),
            response(posts()),
            response(posts("Итог <script>alert(1)</script>", long=True)),
        ],
    ):
        for _ in range(3):
            tick(content_fixture)
    with Session(engine) as db, db.begin():
        operation = db.get(AIOperation, number)
        assert operation.status == "completed", (operation.status, db.get(ContentTask, token).stage)
        assert operation.kind == "generation", operation.kind
        assert operation.result is not None
        assert operation.owner_user_id == owners[1]
        assert operation.bot_id == bot and operation.test_mode
        ticket = urlsplit(issue_ticket(db, config, actors[1])).fragment.removeprefix("token=")
    client = create_app(config, engine).test_client()
    client.environ_base["REMOTE_ADDR"] = f"2001:db8::{actors[1] >> 16:x}:{actors[1] & 65535:x}"
    login = client.get("/cabinet/login?next=agents")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', login.text)[1]
    assert (
        client.post(
            "/cabinet/login", data={"csrf": csrf, "token": ticket, "next": "agents"}
        ).status_code
        == 303
    )
    assert "Готовые посты" in client.get("/cabinet/agents").text
    page = client.get(f"/cabinet/agents/{number}")
    assert page.status_code == 200 and "Скопировать" in page.text
    RENDER_SNAPSHOTS["content"] = page.get_data()
    for path in re.findall(rb'(?:src|href)="(/static/[^"?]+)', page.get_data()):
        asset = client.get(path.decode())
        RENDER_ASSETS[path.decode()] = (asset.get_data(), asset.content_type)
    assert "Паттерны и контент-план" in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert client.get(f"/cabinet/agents/{number}/prompt").status_code == 200
    # A ledger entry belonging to another expert is never exposed.
    with Session(engine) as db, db.begin():
        other = ai_tokens.grant(db, owners[2], bot, True, "other-content", 100).id
    assert client.get(f"/cabinet/agents/{other}").status_code == 404


@pytest.mark.integration
def test_telegram_batch_input_one_receipt_no_api_until_launch_and_recipient_guard(content_fixture):
    engine, config, actors, owners, _ = content_fixture

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        storage = PostgresStorage(engine)
        dp = Dispatcher(storage=storage)
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Synthetic")
        chat = Chat(id=actor.id, type="private")
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await context.set_data({"mode": "expert"})
        calls, sequence = [], 0

        async def request(_bot, method, timeout=None):
            nonlocal sequence
            calls.append(method)
            if isinstance(method, AnswerCallbackQuery):
                # Expired query must not prevent upload, launch or opening results.
                raise TelegramBadRequest(method=method, message="query is too old")
            if isinstance(method, SendMessage):
                sequence += 1
                return Message(message_id=sequence, date=datetime.now(UTC), chat=chat)
            return True

        async def feed(value, callback=True):
            nonlocal sequence
            sequence += 1
            msg = Message(
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
                        message=msg,
                    ),
                )
                if callback
                else Update(update_id=sequence, message=msg)
            )
            await dp.feed_update(bot, update)

        try:
            with (
                patch.object(bot.session, "make_request", request),
                patch("leedbox.application.content_agent.deepseek_generate") as ai,
                patch("leedbox.infrastructure.apify.start") as scrape,
            ):
                await feed("ca:begin")
                assert "ca:new:manual" in str(calls[-1].reply_markup)
                await feed("ca:new:manual")
                token = (await context.get_data())["content_token"]
                assert "Перешлите выбранные посты" in calls[-1].text
                assert "Для кого" not in calls[-1].text and "MAX" not in str(calls[-1].reply_markup)
                await feed(f"ca:{token}:target:threads")
                with Session(engine) as db:
                    assert db.get(ContentTask, token).payload["target"] == "telegram"
                for i in range(10):
                    await feed(
                        f"Выбранный пост {i}. Полный текст с полезным примером для анализа.", False
                    )
                # Data is durable before the coalesced acknowledgement is emitted.
                with Session(engine) as db:
                    row = app.owned(db, config, owners[1], bot.id, token)
                    assert len(row.payload["parts"]) == 10
                await asyncio.sleep(1.5)
                controls = [
                    c
                    for c in calls
                    if isinstance(c, SendMessage) and "Получено сообщений/файлов: 10" in c.text
                ]
                assert len(controls) == 1
                ai.assert_not_called()
                scrape.assert_not_called()
                launch = controls[0].reply_markup.inline_keyboard[0][0].callback_data
                await feed(launch)
                with Session(engine) as db:
                    row = app.owned(db, config, owners[1], bot.id, token)
                    assert row.status == "queued"
                await feed(launch)
                ai.assert_not_called()  # Only the independent worker performs the paid calls.
                with Session(engine) as db:
                    source_id = db.get(ContentTask, token).payload["parts"][0]["id"]
                analyzed = {
                    "patterns": [{"name": "Вопрос и пример", "source_ids": [source_id]}],
                    "topics": [{"title": "Маркетинг"}, {"title": "Бизнес"}],
                    "source_platform": "unknown",
                }
                with patch(
                    "leedbox.application.content_agent.deepseek_generate",
                    return_value=response(analyzed),
                ):
                    tick(content_fixture)
                await notify(bot, engine, config, token)
                assert await context.get_state() == "agent_content_request"
                assert "Изучил посты/фрагменты: 10" in calls[-1].text
                await feed("3 поста про маркетинг и 2 про бизнес", False)
                batch_token = (await context.get_data())["content_token"]
                assert batch_token != token
                assert "Ваш заказ" in calls[-1].text
                assert "маркетинг" in calls[-1].text and "бизнес" in calls[-1].text
                ai.assert_not_called()
                batch_start = calls[-1].reply_markup.inline_keyboard[0][0].callback_data
                await feed(batch_start)
                await feed(batch_start)
                generated = {
                    "posts": [
                        {
                            "title": f"Тема {i}",
                            "parts": [
                                f"Пост {i}. Проверка <script> "
                                "и уникальный полезный совет про бизнес."
                            ],
                        }
                        for i in range(5)
                    ]
                }
                with patch(
                    "leedbox.application.content_agent.deepseek_generate",
                    side_effect=[response(generated), response(generated)],
                ):
                    tick(content_fixture)
                    tick(content_fixture)
                await notify(bot, engine, config, batch_token)
                post_messages = [
                    c for c in calls if isinstance(c, SendMessage) and c.text.startswith("✍️ Пост")
                ]
                assert len(post_messages) == 5
                assert all(
                    c.parse_mode == "HTML" and "<pre>" in c.text and "&lt;script&gt;" in c.text
                    for c in post_messages
                )
                assert post_messages[0].reply_markup.inline_keyboard[0][0].copy_text
                await feed(f"ca:{token}:open")
                assert await context.get_state() == "agent_content_request"
                with Session(engine) as db:
                    assert (
                        len(
                            list(
                                db.scalars(
                                    select(AIOperation).where(
                                        AIOperation.action_key == "content:" + token
                                    )
                                )
                            )
                        )
                        == 1
                    )
                await feed("ca:new:link")
                competitor_token = (await context.get_data())["content_token"]
                assert competitor_token != token  # No mixed or mutually blocking drafts.
                assert "Пришлите ссылку" in calls[-1].text and "Для кого" not in calls[-1].text
                assert "Apify" not in calls[-1].text and "30 дней" not in calls[-1].text
                assert "days:" not in str(calls[-1].reply_markup)
                await feed("https://www.threads.com/@example", False)
                scrape.assert_not_called()
                with Session(engine) as db:
                    assert not db.get(ContentTask, competitor_token).payload.get("url")
                await feed("https://t.me/example", False)
                assert "30 дней" in str(calls[-1].reply_markup)
                assert "За какой период" in calls[-1].text
                with Session(engine) as db:
                    competitor = app.owned(db, config, owners[1], bot.id, competitor_token)
                    assert competitor.payload["workflow"] == "competitor"
                    assert competitor.payload["source_platform"] == "telegram"
                    assert competitor.operation_id is None
                scrape.assert_not_called()
                period = calls[-1].reply_markup.inline_keyboard[1][0].callback_data
                assert ":period:90:" in period
                await feed(period)
                await feed(period)  # The second click must not purchase another search.
                with Session(engine) as db:
                    competitor = app.owned(db, config, owners[1], bot.id, competitor_token)
                    assert competitor.status == "queued"
                    assert competitor.payload["days"] == 90
                    assert competitor.payload["offer"]["days"] == 90
                    assert (
                        len(
                            list(
                                db.scalars(
                                    select(AIOperation).where(
                                        AIOperation.action_key == "competitor:" + competitor_token
                                    )
                                )
                            )
                        )
                        == 1
                    )
                scrape.assert_not_called()
                await context.update_data(mode="recipient")
                before = len(calls)
                await notify(bot, engine, config, token)
                assert len(calls) == before
                await feed(f"ca:{token}:open")
                assert isinstance(calls[-1], AnswerCallbackQuery)
        finally:
            await context.clear()
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_threads_disabled_before_quotes_and_paid_calls(content_fixture):
    engine, config, _, owners, bot = content_fixture
    token, _ = ready(content_fixture, mode="link")
    with (
        patch("leedbox.application.content_agent.apify.start") as run,
        patch("leedbox.application.content_agent.deepseek_generate") as ai,
        Session(engine) as db,
        db.begin(),
    ):
        row = app.owned(db, config, owners[1], bot, token)
        app.change(row, source_platform="threads", url="https://www.threads.com/@example")
        with pytest.raises(ValueError, match="Threads"):
            app.quote(db, row)
        app.change(row, workflow="competitor")
        with pytest.raises(ValueError, match="Telegram"):
            app.quote(db, row)
        run.assert_not_called()
        ai.assert_not_called()
        assert row.operation_id is None and row.status == "draft"


@pytest.mark.integration
def test_no_credits_and_cancel_release_without_api(content_fixture):
    engine, config, _, owners, bot = content_fixture
    token, confirm = ready(content_fixture, index=2)
    with Session(engine) as db, db.begin(), pytest.raises(ValueError, match="Недостаточно"):
        app.launch(db, config, owners[2], bot, token, confirm)
    with Session(engine) as db:
        assert db.get(ContentTask, token).status == "draft"
        assert db.get(ContentTask, token).operation_id is None
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owners[2], bot, True, "cancel-fixture", 200000)
    start(content_fixture, token, confirm, index=2)
    with Session(engine) as db, db.begin():
        row = app.owned(db, config, owners[2], bot, token)
        app.cancel(db, row)
    with patch("leedbox.application.content_agent.deepseek_generate") as ai:
        assert tick(content_fixture) is None
        ai.assert_not_called()
    with Session(engine) as db:
        assert ai_tokens.available(db, owners[2], bot, True) == 200000
        assert db.get(ContentTask, token).status == "cancelled"


@pytest.mark.integration
def test_empty_collection_refunds_customer_but_preserves_known_provider_cost(content_fixture):
    engine, config, _, owners, bot = content_fixture
    with Session(engine) as db:
        before = ai_tokens.available(db, owners[1], bot, True)
    token, confirm = ready(content_fixture, mode="link", legacy_link=False)
    start(content_fixture, token, confirm)
    with (
        patch("leedbox.application.content_agent.apify.start", return_value={"id": "emptyRun123"}),
        patch(
            "leedbox.application.content_agent.apify.poll",
            return_value={
                "status": "SUCCEEDED",
                "defaultDatasetId": "emptyDataset123",
                "usageTotalUsd": 0.02,
            },
        ),
        patch("leedbox.application.content_agent.apify.items", return_value=[]),
        patch("leedbox.application.content_agent.deepseek_generate") as ai,
    ):
        tick(content_fixture)
        tick(content_fixture)
        ai.assert_not_called()
    with Session(engine) as db:
        row = db.get(ContentTask, token)
        assert row.status == "failed"
        assert ai_tokens.available(db, owners[1], bot, True) == before
        search = db.get(AIOperation, row.search_operation_id)
        assert search.status == "failed" and str(search.cost_usd).startswith("0.02")


@pytest.mark.integration
@pytest.mark.parametrize("platform", ["telegram"])
@pytest.mark.parametrize("index", [0, 1])
def test_competitor_search_no_ai_no_text_exact_flat_charge(content_fixture, platform, index):
    engine, config, actors, owners, bot = content_fixture
    config = config.model_copy(update={"deepseek_api_key": SecretStr("")})
    fixture = (engine, config, actors, owners, bot)
    url = "https://t.me/example" if platform == "telegram" else "https://www.threads.com/@example"
    with Session(engine) as db, db.begin():
        before = ai_tokens.available(db, owners[index], bot, True)
        row = app.create(db, config, owners[index], bot, actors[index], "link")
        app.change(row, source_platform=platform, url=url, period_selected=True)
        offer = app.quote(db, row)
        assert offer["maximum"] == offer["search_price"] == 1000
        mode = ai_tokens.funding(db, config, owners[index], bot, True, offer)
        digest = fingerprint({**offer, "billing": mode})[:12]
        token = row.token
        app.launch(db, config, owners[index], bot, token, digest)
        app.launch(db, config, owners[index], bot, token, digest)
    with (
        patch(
            "leedbox.application.content_agent.apify.start", return_value={"id": "listRun123"}
        ) as run,
        patch(
            "leedbox.application.content_agent.apify.poll",
            return_value={
                "status": "SUCCEEDED",
                "defaultDatasetId": "listDataset123",
                "usageTotalUsd": 0.02,
            },
        ),
        patch(
            "leedbox.application.content_agent.apify.items",
            return_value=[
                source_row(
                    platform,
                    description="NEVER_PUBLIC " * 10000,
                    views=500,
                    like_count=12,
                    reply_count=2,
                )
            ],
        ),
        patch("leedbox.application.content_agent.deepseek_generate") as ai,
    ):
        tick(fixture)
        tick(fixture)
        assert tick(fixture) is None
        assert run.call_count == 1
        assert run.call_args.args[4] == "0.04"
        assert run.call_args.kwargs == {"competitor": True}
        ai.assert_not_called()
    with Session(engine) as db, db.begin():
        row = db.get(ContentTask, token)
        assert row.status == "completed" and row.steps == []
        assert row.operation_id == row.search_operation_id
        op = db.get(AIOperation, row.operation_id)
        assert op.kind == "search" and op.status == "completed"
        assert "text" not in op.result["source_links"][0]
        assert ai_tokens.available(db, owners[index], bot, True) == before - (1000 if index else 0)
        number = op.id
        ticket = urlsplit(issue_ticket(db, config, actors[index])).fragment.removeprefix("token=")
    client = create_app(config, engine).test_client()
    actor = actors[index]
    client.environ_base["REMOTE_ADDR"] = f"2001:db8::{actor >> 16:x}:{actor & 65535:x}"
    login = client.get("/cabinet/login?next=agents")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', login.text)[1]
    client.post("/cabinet/login", data={"csrf": csrf, "token": ticket, "next": "agents"})
    page = client.get(f"/cabinet/agents/{number}")
    assert page.status_code == 200 and "Анализ конкурента" in page.text
    assert "NEVER_PUBLIC" not in page.text and "Просмотры" in page.text
    assert "Ссылки и метрики конкурента" in client.get("/cabinet/agents").text
    download = client.get(f"/cabinet/agents/{number}/prompt")
    assert download.status_code == 200 and "NEVER_PUBLIC" not in download.text
    if index == 1 and platform == "telegram":
        RENDER_SNAPSHOTS["competitor"] = page.get_data()
        for path in re.findall(rb'(?:src|href)="(/static/[^"?]+)', page.get_data()):
            asset = client.get(path.decode())
            RENDER_ASSETS[path.decode()] = (asset.get_data(), asset.content_type)
