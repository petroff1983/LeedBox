import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from unittest.mock import patch
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import agents, ai_tokens
from leedbox.application.cabinet_access import issue_ticket
from leedbox.application.profile import delete_my_data
from leedbox.application.settings import set_setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.dzen import (
    ARTICLE,
    article_prompt,
    channel_slug,
    period_start,
    validated_result,
)
from leedbox.domain.writing_profile import FIELDS, SECTIONS, validate_brief, validate_prompt
from leedbox.infrastructure.agents import AgentError, dzen_posts
from leedbox.infrastructure.models import (
    AIAccount,
    AIOperation,
    RateBucket,
    Setting,
    WalletAccount,
    WalletEntry,
)
from leedbox.web.app import create_app


def writing_brief():
    return dict(
        zip(
            (key for key, _ in FIELDS),
            (
                "Поведение кошек",
                "Владельцы кошек, полезные наблюдения",
                "От первого лица мужчины, на вы",
                "Дружеский, с юмором",
                "6000 знаков без пробелов",
                "Нет",
                "Короткое тире, вопрос в конце",
            ),
            strict=True,
        )
    )


def writing_result():
    return {"prompt": article_prompt(ARTICLE, "[ВСТАВЬТЕ ЗАГОЛОВОК]", writing_brief())}


def test_writing_prompt_contract():
    assert validate_brief(writing_brief())["topic"] == "Поведение кошек"
    assert validate_prompt(writing_result())["prompt"].count("[ВСТАВЬТЕ ЗАГОЛОВОК]") == 1
    for value in [
        {},
        {"prompt": "Короткая инструкция"},
        {"prompt": writing_result()["prompt"] + " {{inputs}}"},
        {"prompt": writing_result()["prompt"] + " [ВСТАВЬТЕ ЗАГОЛОВОК]"},
    ]:
        with pytest.raises(ValueError):
            validate_prompt(value)


def structured_writing_result():
    sections = {}
    for key, title in SECTIONS:
        start = writing_result()["prompt"].split("# " + title + "\n", 1)[1]
        sections[key] = start.split("\n\n# ", 1)[0]
    return {"sections": sections}


def test_writing_sections_are_assembled_by_code_and_legacy_typography_is_normalized():
    result = validate_prompt(structured_writing_result(), writing_brief())["prompt"]
    assert result.count("[ВСТАВЬТЕ ЗАГОЛОВОК]") == 1
    assert all("# " + heading in result for _, heading in SECTIONS)
    assert "6000 знаков без пробелов" in result
    assert "От первого лица мужчины" in result
    concise_role = structured_writing_result()
    concise_role["sections"]["role"] = "Редактор канала."
    assert "Редактор канала." in validate_prompt(concise_role, writing_brief())["prompt"]
    legacy = (
        writing_result()["prompt"].replace("# ОБЪЁМ", "## Объем:").replace("# РОЛЬ", "### Роль")
    )
    restored = validate_prompt({"prompt": legacy})["prompt"]
    assert "# ОБЪЁМ\n" in restored and "# РОЛЬ\n" in restored
    empty = structured_writing_result()
    empty["sections"]["voice"] = ""
    with pytest.raises(ValueError, match="содержание"):
        validate_prompt(empty, writing_brief())
    broken = structured_writing_result()
    broken["sections"].pop("facts")
    with pytest.raises(ValueError, match="содержание"):
        validate_prompt(broken, writing_brief())


@pytest.mark.integration
def test_saved_writing_versions_failure_and_idempotency(ai_fixture):
    from leedbox.application import writing_library

    engine, config, actors, owners, bot = ai_fixture
    with Session(engine) as db:
        offer = agents.quote_profile(db, writing_brief())
        assert offer["purpose"] == "writing_profile"
    payload = {
        "usage": dict(prompt_tokens=900, completion_tokens=1700, total_tokens=2600),
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(structured_writing_result(), ensure_ascii=False),
                },
            }
        ],
    }
    with patch("leedbox.application.agents.deepseek_generate", return_value=payload) as api:
        first = agents.generate(engine, config, owners[1], bot, "writing-1", offer)
        assert agents.generate(engine, config, owners[1], bot, "writing-1", offer) == first
        assert api.call_count == 1
        second = agents.generate(engine, config, owners[1], bot, "writing-2", offer)
    with Session(engine) as db:
        prompts = writing_library.page(db, owners[1], bot, True)
        assert {p.operation_id for p in prompts} == {first, second}
        assert len(prompts) == 2
        assert agents.writing_profile(db, owners[1], bot, True).id == second
        assert agents.writing_profile(db, owners[2], bot, True) is None
        assert agents.writing_profile(db, owners[1], bot + 1, True) is None
        assert agents.writing_profile(db, owners[1], bot, False) is None
        assert agents.operation(db, owners[1], bot, True, first).result == validate_prompt(
            structured_writing_result(), writing_brief()
        )
        before = ai_tokens.available(db, owners[1], bot, True)
    payload["choices"][0]["message"]["content"] = '{"prompt":"short"}'
    with (
        patch("leedbox.application.agents.deepseek_generate", return_value=payload),
        pytest.raises(ValueError, match="неподходящий"),
    ):
        agents.generate(engine, config, owners[1], bot, "writing-bad", offer)
    with Session(engine) as db:
        assert agents.writing_profile(db, owners[1], bot, True).id == second
        assert ai_tokens.available(db, owners[1], bot, True) == before


@pytest.mark.integration
def test_writing_interview_resume_without_api(ai_fixture):
    import asyncio
    from datetime import UTC, datetime

    from aiogram import Bot, Dispatcher
    from aiogram.methods import SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from leedbox.telegram.agents import build_agent_router
    from leedbox.telegram.storage import PostgresStorage

    engine, config, actors, owners, _ = ai_fixture
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=PostgresStorage(engine))
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Fixture")
        chat = Chat(id=actor.id, type="private")
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await context.set_data(dict(mode="expert", active_tool="agents", paused=False))

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, SendMessage):
                sent.append(method)
            return True

        def message(text):
            return Message(
                message_id=1, date=datetime.now(UTC), chat=chat, from_user=actor, text=text
            )

        with patch.object(bot.session, "make_request", side_effect=fake_request):
            callback = CallbackQuery(
                id="wp-start",
                from_user=actor,
                chat_instance="1",
                data="wp:edit",
                message=message("begin"),
            )
            await dp.feed_update(bot, Update(update_id=1, callback_query=callback))
            for index, value in enumerate(writing_brief().values()):
                await dp.feed_update(bot, Update(update_id=index + 2, message=message(value)))
                # A new storage instance observes the persisted survey at every step.
                restored = PostgresStorage(engine)
                assert await restored.get_data(context.key) == await context.get_data()
            assert (await context.get_data())["writing_draft"]["phase"] == "confirm"
            assert "На создание нужно до" in sent[-1].text
            assert len(sent[-1].text) < 4096
            await context.clear()
        await bot.session.close()
        await dp.storage.close()

    with patch("leedbox.application.agents.deepseek_generate") as api:
        asyncio.run(scenario())
        api.assert_not_called()


def test_existing_deepseek_environment_alias(monkeypatch):
    from leedbox.infrastructure.config import Settings

    monkeypatch.delenv("LEEDBOX_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LEEDBOX_DEEPSEEK_API", "synthetic-fixture-key")
    config = Settings(
        database_url="postgresql+psycopg://fixture:fixture@localhost/fixture_test",
        bot_token="123456:TEST-TOKEN-ONLY-DO-NOT-USE",
    )
    assert config.deepseek_api_key.get_secret_value() == "synthetic-fixture-key"
    assert "synthetic-fixture-key" not in repr(config)


def result():
    return {
        "entities": ["растения"],
        "markers": ["польза"],
        "limitations": ["Нет текста статьи"],
        "variants": [
            dict(
                id=key,
                title=key + " Как выбрать домашние растения и не ошибиться новичку",
                angle="польза",
                rationale="Сохранена тема",
            )
            for key in "ABCDE"
        ],
        "recommended_id": "A",
    }


def test_inputs_and_literal_templates():
    assert channel_slug("https://dzen.ru/example?share=1") == "example"
    assert channel_slug("https://dzen.ru/id/123") == "id/123"
    for value in ["https://evil.test/foo", "https://dzen.ru/a/123", "http://dzen.ru/foo"]:
        with pytest.raises(ValueError):
            channel_slug(value)
    assert period_start(date(2024, 3, 31), "1") == date(2024, 2, 29)
    assert period_start(date(2026, 1, 1), "week") == date(2025, 12, 26)
    assert "{{inputs}}" in article_prompt(ARTICLE, "{{inputs}}", {"x": "{% unsafe %}"})
    assert validated_result(result())["recommended_id"] == "A"
    concise = result()
    for key, item in zip("ABCDE", concise["variants"], strict=True):
        item["title"] = key + ". Почему кошка избегает вашего дивана"
    assert validated_result(concise)["variants"] == concise["variants"]
    too_short = result()
    too_short["variants"][0]["title"] = "Кошка"
    with pytest.raises(ValueError, match="20–160"):
        validated_result(too_short)
    for invalid in [None, "AB", [], {}]:
        bad = result()
        bad["recommended_id"] = invalid
        with pytest.raises((ValueError, TypeError)):
            validated_result(bad)


def test_provider_filter_and_unknowns():
    post = dict(
        channel_slug="example",
        type_post="card",
        title="Title",
        url="https://dzen.ru/a/123",
        date_publish="2026-10-01T12:00:00Z",
        publication_id="123",
        views=100,
        views_till_end=None,
    )
    payload = {"status": True, "response": {"posts": [post]}}
    with patch("leedbox.infrastructure.agents.request_json", return_value=payload) as call:
        assert (
            dzen_posts("not-a-secret", "example", "2026-10-01", "2026-10-13")[0]["views_till_end"]
            is None
        )
        assert "limit=5" in call.call_args.args[0]
        assert "sort=views_till_end" in call.call_args.args[0]
    post["url"] = "https://evil.test/a"
    with (
        patch("leedbox.infrastructure.agents.request_json", return_value=payload),
        pytest.raises(AgentError),
    ):
        dzen_posts("not-a-secret", "example", "2026-10-01", "2026-10-13")


@pytest.fixture
def ai_fixture(request):
    engine, config, actors, owners, *_ = request.getfixturevalue("service_fixture")
    config = config.model_copy(
        update={
            "deepseek_api_key": SecretStr("fixture"),
            "bot_token": SecretStr("123456:synthetic_test_token_without_real_bot"),
            "zenstat_api_key": SecretStr("fixture"),
            "web_secret_key": SecretStr("fixture-secret-for-cabinet-123456789"),
            "public_base_url": None,
            "admin_enabled": False,
        }
    )
    values = {
        "ai_dzen_enabled": True,
        # Synthetic retail price: fixed dummy search prices cover mocked provider costs.
        "ai_token_price_rub": "1.00",
        "ai_content_search_tokens": 1000,
        "ai_dzen_search_tokens": 1000,
        "ai_images_enabled": True,
        "ai_free_dzen": 0,
        "ai_free_image_prompts": 0,
        "ai_free_lead_magnet": 0,
        "ai_free_content_search": 0,
        "ai_free_youtube_search": 0,
        "wallet_enabled": True,
        "wallet_visible": True,
        "payment_terms": "Тестовые условия",
        "ai_token_packages": [
            {"id": "basic", "tokens": 10000, "price_rub": "100.00", "enabled": True}
        ],
    }
    with Session(engine) as db, db.begin():
        global_key = f"ai:global:{datetime.now(ZoneInfo('Europe/Moscow')).date()}"
        bucket = db.get(RateBucket, global_key)
        previous_bucket = (bucket.started_at, bucket.count) if bucket else None
        if bucket:
            bucket.count = 0
        saved = {key: row.value for key in values if (row := db.get(Setting, key)) is not None}
        for key, value in values.items():
            set_setting(db, config, actors[0], key, value)
        ai_tokens.grant(db, owners[1], 123456, True, "initial", 50000)
    try:
        yield engine, config, actors, owners, 123456
    finally:
        with Session(engine) as db, db.begin():
            bucket = db.get(RateBucket, global_key)
            if bucket:
                if previous_bucket:
                    bucket.started_at, bucket.count = previous_bucket
                else:
                    db.delete(bucket)
            db.execute(delete(AIOperation).where(AIOperation.owner_user_id.in_(owners)))
            db.execute(delete(AIAccount).where(AIAccount.owner_user_id.in_(owners)))
            db.execute(delete(WalletEntry).where(WalletEntry.owner_user_id.in_(owners)))
            db.execute(delete(WalletAccount).where(WalletAccount.owner_user_id.in_(owners)))
            for key in values:
                db.execute(delete(Setting).where(Setting.key == key))
                if key in saved:
                    db.add(Setting(key=key, value=saved[key]))


@pytest.mark.integration
def test_exact_settlement_and_duplicate_concurrent(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    with Session(engine) as db, pytest.raises(ValueError, match="поддержку"):
        delete_my_data(db, actors[1])
    with Session(engine) as db:
        offer = agents.quote_generation(db, {"title": "Растения"}, "Мой канал", [])
    payload = {
        "usage": {"prompt_tokens": 800, "completion_tokens": 450, "total_tokens": 1250},
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result())}}],
    }

    def run():
        return agents.generate(engine, config, owners[1], bot, "once", offer)

    with patch("leedbox.application.agents.deepseek_generate", return_value=payload) as api:
        number = run()
        with ThreadPoolExecutor(2) as pool:
            assert list(pool.map(lambda _: run(), range(2))) == [number, number]
        assert api.call_count == 1
    with Session(engine) as db:
        assert ai_tokens.available(db, owners[1], bot, True) == 48750
        row = agents.operation(db, owners[1], bot, True, number)
        assert (row.input_tokens, row.output_tokens) == (800, 450)
        with pytest.raises(ObjectUnavailable):
            agents.operation(db, owners[2], bot, True, number)


@pytest.mark.integration
def test_invalid_and_unknown_no_retry(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    with Session(engine) as db:
        offer = agents.quote_generation(db, {"title": "Растения"}, "Мой канал", [])
    payload = {
        "usage": {"prompt_tokens": 800, "completion_tokens": 450, "total_tokens": 1250},
        "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
    }
    with (
        patch("leedbox.application.agents.deepseek_generate", return_value=payload),
        pytest.raises(ValueError, match="неподходящий"),
    ):
        agents.generate(engine, config, owners[1], bot, "bad", offer)
    with Session(engine) as db:
        assert ai_tokens.available(db, owners[1], bot, True) == 50000
        failed = ai_tokens.previous(db, owners[1], bot, True, "bad")
        assert failed.result == {"failure": "analysis_fields", "finish_reason": "stop"}
    payload["choices"][0]["finish_reason"] = "length"
    with (
        patch("leedbox.application.agents.deepseek_generate", return_value=payload),
        pytest.raises(ValueError, match="обрезан"),
    ):
        agents.generate(engine, config, owners[1], bot, "truncated", offer)
    with Session(engine) as db:
        assert ai_tokens.available(db, owners[1], bot, True) == 50000
        assert ai_tokens.previous(db, owners[1], bot, True, "truncated").result == {
            "failure": "output_truncated",
            "finish_reason": "length",
        }
    with patch("leedbox.application.agents.deepseek_generate", side_effect=AgentError) as api:
        for _ in range(2):
            with pytest.raises(ValueError):
                agents.generate(engine, config, owners[1], bot, "unknown", offer)
        assert api.call_count == 1
    with Session(engine) as db:
        assert ai_tokens.available(db, owners[1], bot, True) == 50000 - offer["maximum"]
        assert ai_tokens.previous(db, owners[1], bot, True, "unknown").status == "uncertain"


@pytest.mark.integration
def test_insufficient_balance_blocks_provider(ai_fixture):
    engine, config, _, owners, bot = ai_fixture
    with Session(engine) as db:
        offer = agents.quote_generation(db, {"title": "Растения"}, "Мой канал", [])
    with patch("leedbox.application.agents.deepseek_generate") as api:
        with pytest.raises(ValueError, match="Недостаточно"):
            agents.generate(engine, config, owners[2], bot, "empty", offer)
        api.assert_not_called()


@pytest.mark.integration
def test_refund_debt_blocks_paid_generation(ai_fixture):
    from decimal import Decimal

    from leedbox.application.wallet import account as wallet_account

    engine, config, _, owners, bot = ai_fixture
    with Session(engine) as db, db.begin():
        wallet_account(db, owners[1], bot, True).balance = Decimal("-100")
        offer = agents.quote_generation(db, {"title": "Растения"}, "Мой канал", [])
    with patch("leedbox.application.agents.deepseek_generate") as api:
        with pytest.raises(ValueError, match="задолженность"):
            agents.generate(engine, config, owners[1], bot, "refunded", offer)
        api.assert_not_called()


@pytest.mark.integration
def test_package_atomic_idempotent_and_changed_price(ai_fixture):
    from decimal import Decimal

    from leedbox.application.wallet import account as wallet_account

    engine, config, actors, owners, bot = ai_fixture
    with Session(engine) as db, db.begin():
        wallet_account(db, owners[1], bot, True).balance = Decimal("300")
        offer = agents.package_offer(db, config, "basic")
    with pytest.raises(RuntimeError), Session(engine) as db, db.begin():
        agents.buy_package(db, config, owners[1], bot, "rollback", offer)
        raise RuntimeError("rollback")

    def buy():
        with Session(engine) as db, db.begin():
            return agents.buy_package(db, config, owners[1], bot, "purchase", offer).id

    with ThreadPoolExecutor(2) as pool:
        numbers = list(pool.map(lambda _: buy(), range(2)))
    assert numbers[0] == numbers[1]
    with Session(engine) as db, db.begin():
        assert wallet_account(db, owners[1], bot, True).balance == 200
        assert ai_tokens.available(db, owners[1], bot, True) == 60000
        set_setting(
            db,
            config,
            actors[0],
            "ai_token_packages",
            [{"id": "basic", "tokens": 10000, "price_rub": "200.00", "enabled": True}],
        )
    with Session(engine) as db, db.begin(), pytest.raises(ValueError, match="изменилась"):
        agents.buy_package(db, config, owners[1], bot, "stale", offer)


@pytest.mark.integration
def test_private_web_reports_login_redirect_and_escaping(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    # Cabinet's token is derived from its synthetic bot, not this fixture's arbitrary ledger bot.
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owners[1], bot, True, "web-grant", 50000)
        initial_balance = ai_tokens.available(db, owners[1], bot, True)
        offer = agents.quote_generation(db, {"title": "<script>alert(1)</script>"}, "Мой канал", [])
        row = ai_tokens.reserve(db, owners[1], bot, True, "web", offer["maximum"], offer)
        number = row.id
        ai_tokens.settle(
            db,
            owners[1],
            bot,
            True,
            number,
            dict(prompt_tokens=800, completion_tokens=450, total_tokens=1250),
            result(),
            True,
        )
        ticket = urlsplit(issue_ticket(db, config, actors[1])).fragment.removeprefix("token=")
    client = create_app(config, engine).test_client()
    client.environ_base["REMOTE_ADDR"] = f"2001:db8::{actors[1] >> 16:x}:{actors[1] & 65535:x}"
    page = client.get("/cabinet/login?next=agents")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)[1]
    response = client.post("/cabinet/login", data={"csrf": csrf, "token": ticket, "next": "agents"})
    assert response.status_code == 303 and response.location == "/cabinet/agents"
    page = client.get("/cabinet/agents")
    expected_balance = f"{initial_balance - 1250:,}".replace(",", " ")
    assert page.status_code == 200 and expected_balance in page.text
    report = client.get(f"/cabinet/agents/{number}")
    assert report.status_code == 200 and "<script>alert(1)</script>" not in report.text
    for explanation in ("Сохранена тема", "Сущности:", "Маркеры:", "Рекомендация агента"):
        assert explanation not in report.text
    assert client.get(f"/cabinet/agents/{number}/prompt?variant=A").status_code == 200
    with Session(engine) as db, db.begin():
        stranger = ai_tokens.grant(db, owners[2], bot, True, "other", 100).id
    assert client.get(f"/cabinet/agents/{stranger}").status_code == 404
    assert client.get(f"/cabinet/agents/{stranger}/prompt?variant=A").status_code == 404
    from leedbox.application import dzen_history

    selected = {"title": "<script>alert(1)</script>"}
    with Session(engine) as db, db.begin():
        dzen_history.save(
            db,
            owners[1],
            bot,
            True,
            "ownsearch",
            {
                "slug": "fixture",
                "posts": [selected],
            },
        )
        dzen_history.save(
            db,
            owners[2],
            bot,
            True,
            "foreignsearch",
            {
                "slug": "PRIVATE_FOREIGN_CHANNEL",
                "posts": [{"title": "PRIVATE_FOREIGN_TITLE"}],
            },
        )
        with pytest.raises(ValueError, match="перезаписать"):
            dzen_history.save(db, owners[1], bot, True, "ownsearch", {"posts": []})
    with patch("leedbox.application.agents.deepseek_generate") as api:
        search = client.get("/cabinet/agents/search/ownsearch")
        assert search.status_code == 200
        assert "<script>alert(1)</script>" not in search.text
        assert "Открыть заголовки" in search.text
        assert client.get("/cabinet/agents/search/foreignsearch").status_code == 404
        history = client.get("/cabinet/agents").text
        assert "Подборки статей" in history and "Результаты агентов" in history
        for internal in ("deepseek-flash", "Оценка API", "Резерв:", "completed", "generation"):
            assert internal not in history
            assert internal not in report.text
        assert "PRIVATE_FOREIGN_CHANNEL" not in history
        api.assert_not_called()
    with Session(engine) as db, db.begin():
        writing_offer = agents.quote_profile(db, writing_brief())
        saved = ai_tokens.reserve(
            db, owners[1], bot, True, "saved-web", writing_offer["maximum"], writing_offer
        )
        saved_id = saved.id
        ai_tokens.settle(
            db,
            owners[1],
            bot,
            True,
            saved_id,
            dict(prompt_tokens=900, completion_tokens=1700, total_tokens=2600),
            writing_result(),
            True,
        )
    with patch("leedbox.application.agents.deepseek_generate") as api:
        page = client.get(f"/cabinet/agents/{saved_id}")
        assert page.status_code == 200
        assert page.text.count('id="saved-prompt"') == 1
        assert "[ВСТАВЬТЕ ЗАГОЛОВОК]" in page.text
        assert "Оценка себестоимости API" not in page.text
        assert client.get(f"/cabinet/agents/{saved_id}/prompt").text == writing_result()["prompt"]
        assert "Выбрать мой промпт" in client.get(f"/cabinet/agents/{number}").text
        assert "Мои промпты" in client.get("/cabinet/agents").text
        api.assert_not_called()
    with Session(engine) as db, db.begin():
        pending = ai_tokens.reserve(
            db, owners[1], bot, True, "writing-web-pending", writing_offer["maximum"], writing_offer
        )
        pending_id = pending.id
        pending.status = "uncertain"
    page = client.get(f"/cabinet/agents/{pending_id}")
    assert "Промпт этой версии ещё не готов" in page.text
    assert "нет готовых заголовков" not in page.text
    assert "Оценка API: 0.0000" not in client.get("/cabinet/agents").text


@pytest.mark.integration
def test_telegram_insufficient_then_resume_without_paid_call(ai_fixture):
    import asyncio
    from datetime import UTC, datetime

    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.methods import SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from leedbox.telegram.agents import build_agent_router

    engine, config, actors, owners, bot_id = ai_fixture
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[2], is_bot=False, first_name="Fixture")
        chat = Chat(id=actor.id, type="private")
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=chat,
            from_user=actor,
            text="Канал о растениях для начинающих",
        )
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await context.set_state("agent_dzen_expert")
        await context.set_data(
            dict(
                mode="expert",
                active_tool="agents",
                paused=False,
                agent_draft=dict(
                    token="resume",
                    phase="expert",
                    selected={"title": "Растения"},
                    report={"posts": [{"title": "Растения"}]},
                ),
            )
        )

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, SendMessage):
                sent.append(method)
            return True

        with patch.object(bot.session, "make_request", side_effect=fake_request):
            await dp.feed_update(bot, Update(update_id=1, message=message))
            first = sent[-1]
            assert "Остаток: 0" in first.text
            assert not any(
                ":generate:" in (b.callback_data or "")
                for row in first.reply_markup.inline_keyboard
                for b in row
            )
            with Session(engine) as db, db.begin():
                ai_tokens.grant(db, owners[2], bot.id, True, "resume-grant", 50000)
            await context.update_data(active_tool=None, paused=True)
            callback = CallbackQuery(
                id="resume-test",
                from_user=actor,
                chat_instance="1",
                data="ait:return",
                message=message,
            )
            await dp.feed_update(bot, Update(update_id=2, callback_query=callback))
            last = sent[-1]
            assert "Остаток: 50 000" in last.text
            assert any(
                ":generate:" in (b.callback_data or "")
                for row in last.reply_markup.inline_keyboard
                for b in row
            )
            assert (await context.get_data())["active_tool"] == "agents"
            await context.update_data(
                agent_draft=dict(
                    token="resume",
                    phase="error",
                    report={"posts": [{"title": "Растения"}]},
                )
            )
            await dp.feed_update(bot, Update(update_id=3, callback_query=callback))
            assert (await context.get_data())["agent_draft"]["phase"] == "posts"
            keys = []
            for update_id in (4, 5):
                current = (await context.get_data())["agent_draft"]
                await context.update_data(agent_draft={**current, "phase": "posts"})
                choose = callback.model_copy(update={"data": "az:resume:select:0"})
                await dp.feed_update(bot, Update(update_id=update_id, callback_query=choose))
                keys.append((await context.get_data())["agent_draft"]["generation_token"])
            assert keys[0] != keys[1]
        await bot.session.close()
        await dp.storage.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_saved_articles_survive_prompt_and_new_search_without_rebilling(ai_fixture):
    import asyncio
    from datetime import UTC, datetime

    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.methods import SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from leedbox.application import dzen_history
    from leedbox.domain.access import ObjectUnavailable
    from leedbox.telegram.agents import build_agent_router

    engine, config, actors, owners, _ = ai_fixture
    ledger_bot = int(config.bot_token.get_secret_value().split(":")[0])
    report = {"slug": "fixture", "posts": [{"title": f"Растения для дома {i}"} for i in range(5)]}
    with Session(engine) as db, db.begin():
        ai_tokens.grant(db, owners[1], ledger_bot, True, "history-grant", 50000)
        initial_balance = ai_tokens.available(db, owners[1], ledger_bot, True)
        dzen_history.save(db, owners[1], ledger_bot, True, "savedfive", report)
        with pytest.raises(ObjectUnavailable):
            dzen_history.get(db, owners[2], ledger_bot, True, "savedfive")
    sent = []

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(build_agent_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Fixture")
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=actor.id, type="private"),
            from_user=actor,
        )
        context = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await context.set_data(dict(mode="expert", active_tool="writing_profile", paused=True))
        update_id = 0

        async def click(value):
            nonlocal update_id
            update_id += 1
            callback = CallbackQuery(
                id=str(update_id), from_user=actor, chat_instance="1", data=value, message=message
            )
            await dp.feed_update(bot, Update(update_id=update_id, callback_query=callback))

        async def fake_request(bot, method, timeout=None):
            if isinstance(method, SendMessage):
                sent.append(method)
            return True

        payload = {
            "usage": dict(prompt_tokens=800, completion_tokens=450, total_tokens=1250),
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result())}}],
        }
        with (
            patch.object(bot.session, "make_request", side_effect=fake_request),
            patch("leedbox.application.agents.deepseek_generate", return_value=payload) as api,
        ):
            await click("az:savedfive:select:0")
            draft = (await context.get_data())["agent_draft"]
            assert draft["phase"] == "confirm"
            assert await context.get_state() is None
            first_confirmation = "az:savedfive:generate:" + draft["generation_token"]
            await click(first_confirmation)
            assert (await context.get_data())["agent_draft"]["phase"] == "ready"
            assert len(sent[-1].reply_markup.inline_keyboard[:5]) == 5
            assert "E." in sent[-1].text
            assert "Сохранена тема" not in sent[-1].text
            assert "польза" not in sent[-1].text
            api.assert_called_once()
            # The original second article still works while another tool owns FSM.
            await context.update_data(active_tool="writing_profile", paused=True)
            await click("az:savedfive:select:1")
            assert (await context.get_data())["agent_draft"]["selected"] == report["posts"][1]
            await click(first_confirmation)
            api.assert_called_once()  # An old confirmation cannot purchase the second article.
            await click("az:savedfive:select:0")
            assert "Новых списаний нет" in sent[-1].text
            assert "вход:" not in sent[-1].text
            assert "Оценка себестоимости" not in sent[-1].text
            api.assert_called_once()
            await context.update_data(agent_draft={"token": "another", "phase": "channel"})
            await click("az:savedfive:posts:all")
            assert len((await context.get_data())["agent_draft"]["report"]["posts"]) == 5
            await click("ait:searches")
            assert "Подборки статей" in sent[-1].text
            await click("ait:operations")
            assert "Результаты агентов" in sent[-1].text
            for internal in (
                "Остаток:",
                "deepseek",
                "generation",
                "completed",
                "резерв",
                "Оценка API",
            ):
                assert internal not in sent[-1].text
            result_buttons = [
                b
                for row in sent[-1].reply_markup.inline_keyboard
                for b in row
                if (b.callback_data or "").startswith("ait:result:")
            ]
            assert len(result_buttons) == 1  # Grants and unfinished calls are not results.
            await context.update_data(
                agent_draft={"token": "nav", "phase": "period", "slug": "fixture"},
                active_tool="agents",
                paused=False,
            )
            with patch("leedbox.application.agents.stats") as stats:
                await click("az:nav:channel:change")
                assert await context.get_state() == "agent_dzen_channel"
                assert (await context.get_data())["agent_draft"]["slug"] == "fixture"
                assert (await context.get_data())["agent_draft"]["phase"] == "channel"
                await dp.feed_update(
                    bot,
                    Update(
                        update_id=500,
                        message=message.model_copy(update={"text": "https://dzen.ru/newchannel"}),
                    ),
                )
                assert (await context.get_data())["agent_draft"]["slug"] == "newchannel"
                assert (await context.get_data())["agent_draft"]["phase"] == "period"
                assert any(
                    b.callback_data == "az:nav:channel:change"
                    for row in sent[-1].reply_markup.inline_keyboard
                    for b in row
                )
                stats.assert_not_called()
            with patch("leedbox.application.agents.buy_package") as buy:
                await click("ait:offer:basic")
                purchase_token = (await context.get_data())["ai_package_offer"]["token"]
                await click("ait:packages")
                assert (await context.get_data())["ai_package_offer"] == {}
                await click("ait:buy:" + purchase_token)
                buy.assert_not_called()
            api.assert_called_once()
        await bot.session.close()
        await dp.storage.close()

    asyncio.run(scenario())
    with Session(engine) as db:
        assert ai_tokens.available(db, owners[1], ledger_bot, True) == initial_balance - 1250
