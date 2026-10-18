"""Regression tests for the six paid-expert acceptance findings. No live providers."""
# ruff: noqa: F811 -- pytest injects imported fixtures by name.

import asyncio
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User, Voice
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_dzen_agent import ai_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import ai_tokens
from leedbox.application.cabinet_access import issue_ticket
from leedbox.application.entitlements import access_for
from leedbox.application.settings import setting as real_setting
from leedbox.infrastructure.models import AccessGrant, AccessPolicy, AIOperation, ContentTask
from leedbox.telegram.assistance import help_intent
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app


class Journey:
    def __init__(self, fixture):
        self.engine, self.config, self.actors, self.owners, _ = fixture
        self.bot = Bot(self.config.bot_token.get_secret_value())
        self.storage = PostgresStorage(self.engine)
        self.dp = Dispatcher(storage=self.storage)
        self.dp.include_router(build_router(self.engine, self.config))
        self.actor = User(id=self.actors[1], is_bot=False, first_name="Paid expert")
        self.bot_user = User(id=self.bot.id, is_bot=True, first_name="QA", username="qa_bot")
        self.chat = Chat(id=self.actor.id, type="private")
        self.state = self.dp.fsm.get_context(
            bot=self.bot, chat_id=self.actor.id, user_id=self.actor.id
        )
        self.calls, self.sequence, self.screen = [], 1000, 1000

    async def api(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return self.bot_user
        if isinstance(method, AnswerCallbackQuery):
            return True
        self.screen = getattr(method, "message_id", None) or self.screen + 1
        return Message(
            message_id=self.screen,
            date=datetime.now(UTC),
            chat=self.chat,
            from_user=self.bot_user,
            text=getattr(method, "text", None),
        )

    async def message(self, text=None, voice=False):
        self.sequence += 1
        start = len(self.calls)
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=self.sequence,
                message=Message(
                    message_id=self.sequence,
                    date=datetime.now(UTC),
                    chat=self.chat,
                    from_user=self.actor,
                    text=text,
                    voice=Voice(file_id="fixture", file_unique_id="fixture", duration=2)
                    if voice
                    else None,
                ),
            ),
        )
        return self.calls[start:]

    async def click(self, code, screen=None):
        self.sequence += 1
        start = len(self.calls)
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=self.sequence,
                callback_query=CallbackQuery(
                    id=str(self.sequence),
                    chat_instance="audit",
                    from_user=self.actor,
                    data=code,
                    message=Message(
                        message_id=screen or self.screen,
                        date=datetime.now(UTC),
                        chat=self.chat,
                        from_user=self.bot_user,
                    ),
                ),
            ),
        )
        return self.calls[start:]

    async def reset(self):
        await self.state.set_state(None)
        await self.state.set_data(
            dict(mode="expert", paused=True, purchase=None, service_purchase=None)
        )

    async def close(self):
        await self.storage.close()
        await self.bot.session.close()


def texts(calls):
    return "\n".join(str(getattr(c, "text", "") or getattr(c, "caption", "")) for c in calls)


def codes(calls):
    return [
        b.callback_data
        for c in calls
        if getattr(c, "reply_markup", None) and hasattr(c.reply_markup, "inline_keyboard")
        for row in c.reply_markup.inline_keyboard
        for b in row
    ]


@pytest.fixture
def paid_fixture(ai_fixture):
    engine, config, actors, owners, bot = ai_fixture
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        for grant in db.scalars(select(AccessGrant).where(AccessGrant.user_id == owners[1])):
            grant.starts_at, grant.ends_at = now - timedelta(days=100), now - timedelta(days=10)
        db.add(
            AccessGrant(
                user_id=owners[1],
                kind="paid",
                starts_at=now - timedelta(days=1),
                ends_at=now + timedelta(days=30),
                source="synthetic_acceptance",
                action_key=f"acceptance-paid:{owners[1]}",
            )
        )
    policy = AccessPolicy(
        id=1,
        mode="paid",
        price=100,
        currency="RUB",
        period_days=30,
        trial_days=0,
        grace_days=0,
        activation_grace_days=0,
        expired_delivery="bypass",
        hide_recommendations_for_paid=False,
    )
    with patch("leedbox.application.entitlements.policy", return_value=policy):
        with Session(engine) as db:
            assert access_for(db, owners[1]).allowed
        yield ai_fixture


@pytest.mark.parametrize(
    "raw",
    [
        "Почему растения гибнут? Вот мои наблюдения.",
        "## Что написать\nТекст статьи",
        "Поддержка клиентов магазинов",
        "Помоги настроить рекламу для мастеров",
        "Назад в будущее",
    ],
)
def test_help_does_not_steal_real_answers(raw):
    assert help_intent(raw) is None


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("Как пользоваться ботом?", "Как пользоваться"),
        ("Что входит в оплату?", "разные счета"),
        ("Где мои результаты?", "Ваши результаты"),
        ("Как вернуть деньги?", "возврата"),
        ("Связаться с поддержкой", "Поддержка"),
        ("Назад", "Как пользоваться"),
        ("/help", "Как пользоваться"),
        ("/unknown", "Не удалось"),
        (None, "Голосовой ввод"),
    ],
)
def test_expert_gets_help_without_ai(paid_fixture, intent, expected):
    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with (
                patch.object(j.bot.session, "make_request", side_effect=j.api),
                patch("leedbox.application.agents.deepseek_generate") as ai,
            ):
                before = await j.state.get_data()
                calls = await j.message(intent, voice=intent is None)
                assert expected in texts(calls), texts(calls)
                assert await j.state.get_data() == before
                ai.assert_not_called()
        finally:
            await j.close()

    asyncio.run(run())


def test_help_in_lm_preserves_answers_phase_and_original_keyboard(paid_fixture):
    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                await j.click("lm:begin")
                before = await j.state.get_data()
                calls = await j.message("Я не понял, что сюда писать?")
                assert "Чем вы занимаетесь" in texts(calls)
                assert await j.state.get_data() == before
                with Session(j.engine) as db:
                    row = db.get(ContentTask, before["lead_magnet_token"])
                    assert row.payload["brief"] == {} and row.payload["phase"] == "niche"
                await j.message("Настройка рекламы для салонов красоты")
                with Session(j.engine) as db:
                    row = db.get(ContentTask, before["lead_magnet_token"])
                    assert row.payload["phase"] == "audience"
                    assert row.payload["brief"]["niche"] == "Настройка рекламы для салонов красоты"
        finally:
            await j.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "exit_action", ["/menu", "menu", "section:texts", "section:consultations", "/cancel", "/start"]
)
def test_topup_does_not_intercept_after_navigation(paid_fixture, exit_action):
    engine, config, actors, owners, bot = paid_fixture
    configured = (
        engine,
        config.model_copy(update={"robokassa_enabled": True}),
        actors,
        owners,
        bot,
    )

    async def run():
        j = Journey(configured)
        try:
            await j.reset()
            with (
                patch.object(j.bot.session, "make_request", side_effect=j.api),
                patch(
                    "leedbox.telegram.wallet.setting",
                    side_effect=lambda db, key: (
                        True if key == "wallet_topup_enabled" else real_setting(db, key)
                    ),
                ),
            ):
                await j.click("wallet:topup")
                assert await j.state.get_state() == "wallet_amount"
                if exit_action.startswith("/"):
                    await j.message(exit_action)
                else:
                    await j.click(exit_action)
                assert await j.state.get_state() is None
                calls = await j.message("500")
                assert "Введите положительную сумму" not in texts(calls)
                assert "Не удалось" in texts(calls)
                assert not (await j.state.get_data()).get("wallet_offer")
        finally:
            await j.close()

    asyncio.run(run())


@pytest.mark.parametrize("code", ["ip:begin", "az:begin", "ca:begin", "lm:begin"])
def test_manual_unlimited_is_uniform_after_subscription_expires(paid_fixture, code):
    engine, config, actors, owners, bot = paid_fixture
    with Session(engine) as db, db.begin():
        for grant in db.scalars(select(AccessGrant).where(AccessGrant.user_id == owners[1])):
            grant.starts_at = datetime.now(UTC) - timedelta(days=30)
            grant.ends_at = datetime.now(UTC) - timedelta(days=2)
        ai_tokens.account(db, owners[1], bot, True).unlimited = True
    with Session(engine) as db:
        assert not access_for(db, owners[1]).allowed

    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                calls = await j.click(code)
                assert calls and "Доступ закончился" not in texts(calls)
                assert "Что-то пошло не так" not in texts(calls)
                if code == "lm:begin":
                    await j.message("Настройка рекламы для экспертов")
                    assert await j.state.get_state() == "agent_lm_input"
                    with Session(j.engine) as db:
                        row = db.get(ContentTask, (await j.state.get_data())["lead_magnet_token"])
                        assert row.payload["phase"] == "audience"
        finally:
            await j.close()

    asyncio.run(run())


def test_cancel_preserves_lm_draft_and_can_resume(paid_fixture):
    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                await j.click("lm:begin")
                await j.message("Маркетолог для экспертов")
                token = (await j.state.get_data())["lead_magnet_token"]
                calls = await j.message("/cancel")
                assert "Ответы и результаты сохранены" in texts(calls)
                assert await j.state.get_state() is None
                await j.click("lm:begin")
                assert (await j.state.get_data())["lead_magnet_token"] == token
                assert await j.state.get_state() == "agent_lm_input"
                with Session(j.engine) as db:
                    assert (
                        db.get(ContentTask, token).payload["brief"]["niche"]
                        == "Маркетолог для экспертов"
                    )
        finally:
            await j.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "begin,resume,key",
    [
        ("lm:begin", "lm:begin", "lead_magnet_token"),
        ("ip:begin", "ip:resume", "image_draft"),
        ("az:begin", "ait:return", "agent_draft"),
        ("ct:new:offer", "ct:resume", "text_draft"),
        ("calc:new:ads", "calc:resume", "calc_draft"),
    ],
)
def test_menu_stops_every_input_without_destroying_resumable_draft(
    paid_fixture, begin, resume, key
):
    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                await j.click(begin)
                if begin == "calc:new:ads":
                    draft = (await j.state.get_data())["calc_draft"]
                    await j.click(f"cw:{draft['revision']}:platform:yandex", screen=draft["screen"])
                before = (await j.state.get_data())[key]
                assert await j.state.get_state() is not None
                await j.message("/menu")
                assert await j.state.get_state() is None
                assert (await j.state.get_data())[key] == before
                assert "Не удалось" in texts(await j.message("Обычное сообщение вне анкеты"))
                assert (await j.state.get_data())[key] == before
                calls = await j.click(resume)
                assert "Что-то пошло не так" not in texts(calls)
                assert await j.state.get_state() is not None
        finally:
            await j.close()

    asyncio.run(run())


def test_image_confirmation_explains_freeform_and_preserves_offer(paid_fixture):
    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                await j.click("ip:begin")
                await j.message("## Уход за растениями\n" + "Поливайте растения умеренно. " * 20)
                draft = (await j.state.get_data())["image_draft"]
                await j.click("ip:finish:" + draft["token"])
                before = await j.state.get_data()
                calls = await j.message("Сделай в стиле настоящей фотографии")
                assert "фотореализм уже предусмотрен" in texts(calls)
                assert any(str(c).startswith("ip:edit:") for c in codes(calls))
                assert await j.state.get_data() == before
        finally:
            await j.close()

    asyncio.run(run())


def test_agent_catalogue_matches_actual_results(paid_fixture):
    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                text = texts(await j.click("section:agents"))
                assert "ссылки публикаций и метрики, без генерации" in text
                assert "5 тем с описанием и форматом" in text
                assert "5 материалов" not in text
        finally:
            await j.close()

    asyncio.run(run())


def test_expired_core_access_does_not_block_separately_funded_agents(paid_fixture):
    engine, config, actors, owners, bot = paid_fixture
    with Session(engine) as db, db.begin():
        for grant in db.scalars(select(AccessGrant).where(AccessGrant.user_id == owners[1])):
            grant.starts_at = datetime.now(UTC) - timedelta(days=30)
            grant.ends_at = datetime.now(UTC) - timedelta(days=2)
        ai_tokens.account(db, owners[1], bot, True).unlimited = False

    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                assert "Как пользоваться" in texts(await j.message("/help"))
                for code in ("ca:begin", "lm:begin", "ip:begin"):
                    assert "Доступ закончился" not in texts(await j.click(code))
                assert "Доступ закончился" in texts(await j.click("calc:new:ads"))
                assert "Как пользоваться" in texts(await j.click("help:open"))
        finally:
            await j.close()

    asyncio.run(run())


def test_help_and_cancel_do_not_switch_recipient_to_expert(paid_fixture):
    async def run():
        j = Journey(paid_fixture)
        try:
            await j.state.set_state(None)
            await j.state.set_data(dict(mode="recipient", paused=True, material_slug="fixture"))
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                for command in ("/help", "/cancel"):
                    calls = await j.message(command)
                    assert "Для подарка" in texts(calls)
                    assert not codes(calls)
                    assert (await j.state.get_data())["mode"] == "recipient"
                    assert (await j.state.get_data())["material_slug"] == "fixture"
        finally:
            await j.close()

    asyncio.run(run())


def test_all_saved_agent_results_paginate_without_duplicates_and_owner_leaks(paid_fixture):
    engine, config, actors, owners, bot = paid_fixture
    with Session(engine) as db, db.begin():
        rows = [
            AIOperation(
                owner_user_id=owners[1],
                bot_id=bot,
                test_mode=True,
                action_key=f"acceptance-history:{owners[1]}:{n}",
                kind="generation",
                status="completed",
                model="synthetic",
                balance_after=50000,
                snapshot={"purpose": "image_prompts"},
                result={"images": []},
            )
            for n in range(51)
        ]
        db.add_all(rows)
        db.flush()
        expected = {r.id for r in rows}
        first = rows[0].id

    async def run():
        j = Journey(paid_fixture)
        try:
            await j.reset()
            with patch.object(j.bot.session, "make_request", side_effect=j.api):
                found = []
                for page in range(6):
                    calls = await j.click(
                        "ait:operations" if not page else f"ait:operations:{page}"
                    )
                    values = codes(calls)
                    found.extend(
                        int(c.split(":")[-1]) for c in values if c and c.startswith("ait:result:")
                    )
                    assert (f"ait:operations:{page + 1}" in values) is (page < 5)
                    if page:
                        assert f"ait:operations:{page - 1}" in values
                assert set(found) == expected and len(found) == 51
                for invalid in ("-1", "abc", "100001", "99999999999", "١"):
                    assert "Неверная страница" in texts(await j.click("ait:operations:" + invalid))
        finally:
            await j.close()

    asyncio.run(run())
    with Session(engine) as db, db.begin():
        ticket = urlsplit(issue_ticket(db, config, actors[1])).fragment.removeprefix("token=")
    client = create_app(config, engine).test_client()
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', client.get("/cabinet/login").text)[1]
    client.post("/cabinet/login", data=dict(csrf=csrf, token=ticket, next="agents"))
    page = client.get("/cabinet/agents")
    assert page.status_code == 200 and "Страницы результатов агентов" in page.text
    assert f'href="/cabinet/agents/{first}"' not in page.text
    page2 = client.get("/cabinet/agents?results_page=1&prompts_page=2")
    assert f'href="/cabinet/agents/{first}"' in page2.text
    assert "results_page=0" in page2.text and "prompts_page=2" in page2.text
    assert len(re.findall(r'href="/cabinet/agents/\d+"', page.text)) == 50
    assert len(re.findall(r'href="/cabinet/agents/\d+"', page2.text)) == 1
    for invalid in ("-1", "abc", "100001"):
        assert client.get("/cabinet/agents?results_page=" + invalid).status_code == 400
