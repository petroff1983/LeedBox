import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_admin_postgres import admin_fixture  # noqa: F401
from test_content_postgres import content_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.content import save_catalog, snapshot
from leedbox.application.monetization import (
    claim_recommendation,
    manual_conversion,
    preference,
    redirect_code,
    shown,
)
from leedbox.application.objections import save_answer
from leedbox.application.partners import PartnerInput, save_partner
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import (
    ArticleCategory,
    ContentVersion,
    Objection,
    Partner,
    PersonalObjection,
    ProductEvent,
    RecommendationEvent,
    ServiceRedirect,
    Setting,
    ToolResult,
)
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app


@pytest.mark.integration
def test_personal_answers_are_separate_scoped_revisioned(request):
    engine, _, _, owners, *_ = request.getfixturevalue("content_fixture")
    with Session(engine) as db, db.begin():
        source = db.scalar(select(Objection).order_by(Objection.id))
        before = snapshot(source)
        first = save_answer(db, owners[1], source.id, "Мой ответ", 0)
        second = save_answer(db, owners[2], source.id, "Другой ответ", 0)
        assert first.id != second.id and first.answer != second.answer
        assert save_answer(db, owners[1], source.id, "Мой ответ", 0).id == first.id
        with pytest.raises(ValueError, match="изменился"):
            save_answer(db, owners[1], source.id, "Повторный ответ", 0)
        with pytest.raises(ObjectUnavailable):
            save_answer(db, 2**62, source.id, "Нет владельца", 0)
        assert snapshot(source) == before


@pytest.mark.integration
def test_recommendation_session_marking_optout_redirect_and_manual_conversion(request):
    engine, config, actors, owners, *_ = request.getfixturevalue("content_fixture")
    bot_id = 123456
    partner_ids = []
    with Session(engine) as db:
        previous = db.get(Setting, "recommendations_enabled")
        previous_value = previous.value if previous else None
    try:
        with Session(engine) as db, db.begin():
            if previous is None:
                db.add(Setting(key="recommendations_enabled", value=True))
            else:
                db.get(Setting, "recommendations_enabled").value = True
            blocked = save_partner(
                db,
                config,
                actors[0],
                PartnerInput("Черновик", "Не опубликован", "https://example.org/draft"),
            )
            marked = save_partner(
                db,
                config,
                actors[0],
                PartnerInput(
                    "Партнёр",
                    "Тестовое описание",
                    "https://example.org/partner",
                    "Тестовый рекламодатель",
                    "1234567890",
                    "test-erid",
                    "Реклама",
                    enabled=True,
                ),
            )
            partner_ids = [blocked.id, marked.id]
            save_catalog(
                db,
                config,
                actors[0],
                "recommendation",
                {
                    "slug": "test-rule",
                    "title": "Тестовое правило",
                    "enabled": True,
                    "payload": {"tools": ["offer"], "partner_ids": partner_ids},
                },
                action_key="c" * 32,
            )
            item = claim_recommendation(db, owners[1], bot_id, "offer")
            assert item and item["partner_id"] == marked.id and "erid: test-erid" in item["body"]
            assert "1234567890" in item["body"]
            assert claim_recommendation(db, owners[1], bot_id, "offer") is None
            assert db.scalar(select(func.count()).select_from(RecommendationEvent)) >= 0
            shown(db, owners[1], bot_id, item)
            shown(db, owners[1], bot_id, item)
            code = item["code"]
            pref = preference(db, owners[1], bot_id)
            pref.last_activity = datetime.now(UTC) - timedelta(minutes=31)
            pref.opted_out = True
            assert claim_recommendation(db, owners[1], bot_id, "offer") is None
            assert not pref.claimed
            pref.opted_out = False
            assert claim_recommendation(db, owners[1], bot_id, "offer")
            manual_conversion(db, config, actors[0], bot_id, marked.id, "d" * 32)
            manual_conversion(db, config, actors[0], bot_id, marked.id, "d" * 32)
        client = create_app(config, engine).test_client()
        with Session(engine) as db:
            count = db.scalar(
                select(func.count())
                .select_from(RecommendationEvent)
                .where(RecommendationEvent.partner_id == partner_ids[1])
            )
        assert client.head("/go/" + code).status_code == 302
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(RecommendationEvent)
                    .where(RecommendationEvent.partner_id == partner_ids[1])
                )
                == count
            )
        response = client.get("/go/" + code)
        assert (
            response.status_code == 302
            and response.headers["Location"] == "https://example.org/partner"
        )
        assert response.headers["Referrer-Policy"] == "no-referrer"
        with Session(engine) as db, db.begin():
            db.get(Partner, partner_ids[1]).enabled = False
        assert client.get("/go/" + code).status_code == 404
        with Session(engine) as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(RecommendationEvent)
                    .where(
                        RecommendationEvent.partner_id == partner_ids[1],
                        RecommendationEvent.kind == "conversion",
                    )
                )
                == 1
            )
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(ServiceRedirect).where(ServiceRedirect.bot_id == bot_id))
            db.execute(delete(Partner).where(Partner.id.in_(partner_ids)))
            db.execute(
                delete(ContentVersion).where(
                    ContentVersion.entity == "partner",
                    ContentVersion.object_key.in_([str(id) for id in partner_ids]),
                )
            )
            db.execute(
                delete(ProductEvent).where(
                    ProductEvent.name.in_(["recommendation_shown", "recommendation_clicked"]),
                    ProductEvent.bot_id == bot_id,
                )
            )
            if previous_value is None:
                db.execute(delete(Setting).where(Setting.key == "recommendations_enabled"))
            else:
                db.get(Setting, "recommendations_enabled").value = previous_value


@pytest.mark.integration
def test_case_proposal_funnel_objection_durability_and_recipient_guard(request):
    engine, config, actors, owners, *_ = request.getfixturevalue("content_fixture")

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        bot_user = User(id=bot.id, is_bot=True, first_name="Test", username="test_bot")
        storage = PostgresStorage(engine)
        key = StorageKey(bot_id=bot.id, chat_id=actors[1], user_id=actors[1])
        dp = Dispatcher(storage=storage)
        dp.include_router(
            build_router(
                engine, config.model_copy(update={"public_base_url": "https://example.org"})
            )
        )
        calls, update_id = [], 0

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            return Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=actors[1], type="private"),
                from_user=bot_user,
                text="Тест",
            )

        async def send(value, callback=True, actor=actors[1]):
            nonlocal update_id
            update_id += 1
            user = User(id=actor, is_bot=False, first_name="Test")
            message = Message(
                message_id=1 if callback else 100 + update_id,
                date=datetime.now(UTC),
                chat=Chat(id=actor, type="private"),
                from_user=bot_user if callback else user,
                text=None if callback else value,
            )
            update = (
                Update(
                    update_id=update_id,
                    callback_query=CallbackQuery(
                        id=str(update_id),
                        data=value,
                        chat_instance="test",
                        from_user=user,
                        message=message,
                    ),
                )
                if callback
                else Update(update_id=update_id, message=message)
            )
            await dp.feed_update(bot, update)

        async def action(command):
            draft = (await storage.get_data(key))["text_draft"]
            await send(f"tw:{draft['revision']}:{command}")

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await send("/start", False)
                for tool in ("case", "proposal-consultation", "proposal-product"):
                    await send("ct:new:" + tool)
                    if tool == "case":
                        for answer in (
                            "Клиент: магазин; мало заявок",
                            "Настроили рекламу",
                            "20 заявок за месяц",
                        ):
                            await send(answer, False)
                        await action("skip")
                    else:
                        for _ in range(7):
                            await action("skip")
                    draft = (await storage.get_data(key))["text_draft"]
                    with Session(engine) as db:
                        result = db.get(ToolResult, draft["result_id"])
                        assert result.owner_user_id == owners[1] and "[впишите]" in result.result
                    assert any(getattr(call, "document", None) for call in calls)
                await send("ct:new:sequence")
                with Session(engine) as db:
                    result = db.scalar(
                        select(ToolResult)
                        .where(ToolResult.owner_user_id == owners[1], ToolResult.tool == "sequence")
                        .order_by(ToolResult.id.desc())
                    )
                    assert "16 заголовков" in result.result
                    assert "# СТАРТ" in result.result
                    result_id = result.id
                await send(f"ap:{result_id}:0")
                assert any(getattr(call, "parse_mode", None) == "HTML" for call in calls)
                with Session(engine) as db:
                    objection_id = db.scalar(select(Objection.id).order_by(Objection.id))
                await send(f"ob:write:{objection_id}")
                await send("Мой личный ответ", False)
                fresh = PostgresStorage(engine)
                pending = (await fresh.get_data(key))["objection_draft"]
                await fresh.close()
                await send("/start", False)
                await send("ob:resume:0")
                await send("ob:save:" + pending["token"])
                with Session(engine) as db:
                    assert (
                        db.scalar(
                            select(PersonalObjection.answer).where(
                                PersonalObjection.owner_user_id == owners[1]
                            )
                        )
                        == "Мой личный ответ"
                    )
                await send("/start lm_missing", False, actors[2])
                for command in (
                    f"ob:open:{objection_id}",
                    "ct:new:sequence",
                    "kb:categories",
                    "rec:on",
                ):
                    await send(command, actor=actors[2])
                    assert calls[-1].show_alert
        finally:
            await storage.close()
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_articles_redirect_categories_and_admin_forms(request):
    context = request.getfixturevalue("content_fixture")
    client, csrf, config, *_ = request.getfixturevalue("admin_fixture")
    engine, _, actors, *_ = context
    bot_id = int(config.bot_token.get_secret_value().split(":")[0])
    code = None
    try:
        with Session(engine) as db, db.begin():
            category = save_catalog(
                db,
                config,
                actors[0],
                "category",
                {
                    "slug": "test-knowledge",
                    "title": "Тестовая категория",
                    "enabled": True,
                    "payload": {"description": "Тестовое описание"},
                },
                action_key="e" * 32,
            )
            category_id = category.id
            article = save_catalog(
                db,
                config,
                actors[0],
                "article",
                {
                    "slug": "test-article",
                    "title": "Тестовая статья",
                    "enabled": True,
                    "payload": {
                        "category": category.slug,
                        "description": "Тестовое описание",
                        "url": "https://telegra.ph/Test-article-10-10",
                        "partner_ids": [],
                    },
                },
                action_key="f" * 32,
            )
            article_id = article.id
            code = redirect_code(db, bot_id, "article", article_id)
        assert client.head("/go/" + code).status_code == 302
        assert client.get("/go/" + code).status_code == 302
        with Session(engine) as db, db.begin():
            events = list(
                db.scalars(
                    select(ProductEvent).where(
                        ProductEvent.name == "article_opened",
                        ProductEvent.details["article_id"].astext == str(article_id),
                    )
                )
            )
            assert len(events) == 1 and events[0].telegram_user_id is None
            db.get(ArticleCategory, category_id).enabled = False
        assert client.get("/go/" + code).status_code == 404
        for kind in ("objection", "topic", "category", "article", "recommendation"):
            response = client.get(f"/owner/content-{kind}/edit")
            assert response.status_code == 200 and 'name="csrf_token"' in response.text
        for tool in ("case", "proposal", "funnel"):
            assert client.get("/owner/content-template/edit?tool=" + tool).status_code == 200
        assert client.get("/owner/operations/partners").status_code == 200
    finally:
        with Session(engine) as db, db.begin():
            if code:
                db.execute(delete(ServiceRedirect).where(ServiceRedirect.code == code))
            db.execute(
                delete(ProductEvent).where(
                    ProductEvent.bot_id == bot_id, ProductEvent.name == "article_opened"
                )
            )
