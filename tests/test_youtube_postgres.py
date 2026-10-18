import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from threading import Event
from unittest.mock import patch

import pytest
import test_links_postgres
from aiogram import Bot, Dispatcher
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    GetMe,
    SendDocument,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from leedbox.application import agents, youtube_billing
from leedbox.application.entitlements import start_expert
from leedbox.application.youtube import analyze, cached_snapshot, quota_day, reserve, snapshot_key
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.youtube import ChannelSnapshot, Video
from leedbox.infrastructure.models import AIAccount, AIOperation, RateBucket, User, YouTubeCache
from leedbox.infrastructure.youtube import YouTubeError
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage

link_fixture = test_links_postgres.link_fixture


@pytest.fixture
def youtube_fixture(link_fixture):
    engine, config, actors, owners, projects = link_fixture
    config = config.model_copy(update={"youtube_api_key": SecretStr("synthetic-youtube-key")})
    with Session(engine) as db, db.begin():
        for owner in owners[:2]:
            start_expert(db, db.get(User, owner))
        db.execute(delete(RateBucket).where(RateBucket.key.like("yt:%")))
        db.execute(delete(YouTubeCache))
    yield engine, config, actors, owners, projects
    with Session(engine) as db, db.begin():
        db.execute(delete(RateBucket).where(RateBucket.key.like("yt:%")))
        db.execute(delete(YouTubeCache))
        db.execute(delete(AIOperation).where(AIOperation.owner_user_id.in_(owners)))
        db.execute(delete(AIAccount).where(AIAccount.owner_user_id.in_(owners)))


def snapshot():
    now = datetime.now(UTC).isoformat()
    return ChannelSnapshot(
        "UC" + "a" * 22,
        "Синтетический канал",
        now,
        1,
        False,
        [Video("abcdefghijk", "Тестовое видео", now, 100, None, 0)],
    )


@pytest.mark.integration
def test_shared_cache_expiry_and_expert_access(youtube_fixture):
    engine, config, _, owners, _ = youtube_fixture
    with patch("leedbox.application.youtube.YouTubeClient.scan", return_value=snapshot()) as scan:
        first, reused = analyze(engine, config, owners[0], 123456, "@test")
        assert not reused
        second, reused = analyze(engine, config, owners[1], 123456, "@test")
        assert reused and first == second
        scan.assert_called_once()
        with pytest.raises(ObjectUnavailable):
            analyze(engine, config, owners[2], 123456, "@test")
        with Session(engine) as db, db.begin():
            row = db.get(YouTubeCache, snapshot_key(config, "@test"))
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(YouTubeError, match="устарел"):
            cached_snapshot(engine, config, "@test")
        analyze(engine, config, owners[0], 123456, "@test")
        assert scan.call_count == 2


@pytest.mark.integration
def test_atomic_budget_under_concurrency_and_day_reset(youtube_fixture):
    engine, _, _, _, _ = youtube_fixture

    def attempt(_):
        try:
            reserve(engine, "yt:concurrency-test", 3)
            return True
        except YouTubeError:
            return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt, range(10))) == 3
    with Session(engine) as db, db.begin():
        db.get(RateBucket, "yt:concurrency-test").started_at = quota_day(
            datetime.now(UTC)
        ) - timedelta(days=2)
    reserve(engine, "yt:concurrency-test", 3)
    with Session(engine) as db:
        assert db.get(RateBucket, "yt:concurrency-test").count == 1


@pytest.mark.integration
def test_daily_user_cap_and_provider_quota_close_budget(youtube_fixture):
    engine, config, _, owners, _ = youtube_fixture
    config = config.model_copy(update={"youtube_daily_scans": 1})
    with patch("leedbox.application.youtube.YouTubeClient.scan", return_value=snapshot()) as scan:
        analyze(engine, config, owners[0], 123456, "@first")
        with pytest.raises(YouTubeError, match="лимит"):
            analyze(engine, config, owners[0], 123456, "@second")
        scan.assert_called_once()
    with Session(engine) as db, db.begin():
        reserve_key = "yt:units"
        db.add(RateBucket(key=reserve_key, started_at=quota_day(datetime.now(UTC)), count=1))
    with (
        patch(
            "leedbox.application.youtube.YouTubeClient.scan",
            side_effect=YouTubeError("Квота", quota=True),
        ),
        pytest.raises(YouTubeError),
    ):
        analyze(engine, config, owners[1], 123456, "@other")
    with Session(engine) as db:
        assert db.get(RateBucket, "yt:units").count == config.youtube_daily_units


@pytest.mark.integration
def test_busy_capacity_does_not_spend_user_budget_and_locks_release(youtube_fixture):
    engine, config, _, owners, _ = youtube_fixture
    with engine.connect() as blocker:
        for key in range(731910000000000001, 731910000000000004):
            blocker.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        blocker.commit()
        try:
            with patch("leedbox.application.youtube.YouTubeClient.scan") as scan:
                with pytest.raises(YouTubeError, match="несколько каналов"):
                    analyze(engine, config, owners[0], 123456, "@busy")
                scan.assert_not_called()
            with Session(engine) as db:
                assert db.get(RateBucket, f"yt:user:123456:{owners[0]}") is None
        finally:
            blocker.execute(text("SELECT pg_advisory_unlock_all()"))
            blocker.commit()
    with patch("leedbox.application.youtube.YouTubeClient.scan", return_value=snapshot()):
        _, cached = analyze(engine, config, owners[0], 123456, "@busy")
        assert not cached


@pytest.mark.integration
def test_telegram_wizard_restart_export_stale_and_recipient_isolation(youtube_fixture):
    engine, config, actors, _, _ = youtube_fixture

    async def run():
        bot = Bot(config.bot_token.get_secret_value())
        bot_user = TelegramUser(id=bot.id, is_bot=True, first_name="Synthetic bot")
        dispatcher = Dispatcher(storage=PostgresStorage(engine))
        dispatcher.include_router(build_router(engine, config))
        calls = []
        sequence = 100

        async def api(_bot, method, timeout=None):
            nonlocal sequence
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            sequence += 1
            return Message(
                message_id=sequence,
                date=datetime.now(UTC),
                chat=Chat(id=actors[0], type="private"),
                from_user=bot_user,
            )

        async def feed(actor, data=None, text=None, screen=1):
            nonlocal sequence
            sequence += 1
            user = TelegramUser(id=actor, is_bot=False, first_name="Synthetic expert")
            if data is not None:
                event = CallbackQuery(
                    id=str(sequence),
                    chat_instance="test",
                    from_user=user,
                    data=data,
                    message=Message(
                        message_id=screen,
                        date=datetime.now(UTC),
                        chat=Chat(id=actor, type="private"),
                        from_user=bot_user,
                    ),
                )
                update = Update(update_id=sequence, callback_query=event)
            else:
                update = Update(
                    update_id=sequence,
                    message=Message(
                        message_id=sequence,
                        text=text,
                        date=datetime.now(UTC),
                        chat=Chat(id=actor, type="private"),
                        from_user=user,
                    ),
                )
            await dispatcher.feed_update(bot, update)

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                state = dispatcher.fsm.get_context(bot=bot, chat_id=actors[0], user_id=actors[0])
                await state.update_data(mode="expert")
                await feed(actors[0], data="section:agents")
                catalogue = [c for c in calls if isinstance(c, SendMessage)][-1]
                assert "section:youtube" in str(catalogue.reply_markup)
                assert "az:begin" in str(catalogue.reply_markup)
                assert "ip:begin" in str(catalogue.reply_markup)
                await feed(actors[0], data="section:youtube")
                welcome = [c for c in calls if isinstance(c, SendMessage)][-1]
                assert "Анализ YouTube" in welcome.text
                assert "section:agents" in str(welcome.reply_markup)
                await feed(actors[0], text="@test")
                # A fresh storage instance reads the persisted step after a simulated restart.
                storage = PostgresStorage(engine)
                assert (await storage.get_data(state.key))["youtube_draft"]["phase"] == "period"
                draft = (await state.get_data())["youtube_draft"]
                await feed(actors[0], data=f"yt:{draft['token']}:period:30", screen=draft["screen"])
                draft = (await state.get_data())["youtube_draft"]
                with patch(
                    "leedbox.application.youtube.YouTubeClient.scan", return_value=snapshot()
                ) as scan:
                    await feed(
                        actors[0], data=f"yt:{draft['token']}:count:5", screen=draft["screen"]
                    )
                    scan.assert_called_once()
                ready = (await state.get_data())["youtube_draft"]
                assert ready["phase"] == "ready"
                report = [c for c in calls if isinstance(c, SendMessage)][-1]
                assert "Топ-1" in report.text and "нет данных" in report.text
                assert "section:agents" in str(report.reply_markup)
                assert report.reply_markup.inline_keyboard[0][0].url.endswith("abcdefghijk")
                await feed(
                    actors[0], data=f"yt:{ready['token']}:csv:download", screen=ready["screen"]
                )
                assert any(isinstance(c, SendDocument) for c in calls)
                await feed(actors[0], data=f"yt:{draft['token']}:count:5", screen=draft["screen"])
                assert isinstance(calls[-1], AnswerCallbackQuery) and calls[-1].show_alert
                await feed(actors[0], data="section:agents")
                assert (await state.get_data())["active_tool"] == "agents"
                assert "section:youtube" in str(
                    [c for c in calls if isinstance(c, SendMessage)][-1].reply_markup
                )
                recipient = dispatcher.fsm.get_context(
                    bot=bot, chat_id=actors[2], user_id=actors[2]
                )
                await recipient.update_data(mode="recipient")
                before = len([c for c in calls if isinstance(c, SendMessage)])
                await feed(actors[2], data="section:youtube")
                assert len([c for c in calls if isinstance(c, SendMessage)]) == before
                with Session(engine) as db:
                    assert (
                        db.scalar(
                            select(RateBucket).where(
                                RateBucket.key == f"yt:user:{bot.id}:{actors[2]}"
                            )
                        )
                        is None
                    )
        finally:
            await bot.session.close()

    asyncio.run(run())


@pytest.mark.integration
@pytest.mark.parametrize(
    "destination,failure,export",
    [
        ("audience", False, False),
        ("recipient", False, False),
        ("recipient", True, False),
        ("menu", False, False),
        ("recipient", False, True),
    ],
)
def test_delayed_work_never_enters_another_dialog(youtube_fixture, destination, failure, export):
    engine, config, actors, owners, _ = youtube_fixture
    config = config.model_copy(update={"web_secret_key": SecretStr(token_urlsafe(48))})

    async def run():
        bot = Bot(config.bot_token.get_secret_value())
        bot_user = TelegramUser(id=bot.id, is_bot=True, first_name="Synthetic bot")
        dispatcher = Dispatcher(storage=PostgresStorage(engine))
        dispatcher.include_router(build_router(engine, config))
        state = dispatcher.fsm.get_context(bot=bot, chat_id=actors[0], user_id=actors[0])
        with Session(engine) as db:
            offer = youtube_billing.quote(db, config, owners[0], bot.id, "@race")
            agents.payment_preview(db, config, owners[0], bot.id, offer)
        await state.set_state(None)
        await state.update_data(
            mode="expert",
            paused=False,
            active_tool="youtube",
            youtube_draft={
                "token": "race",
                "phase": "ready" if export else "count",
                "screen": 123,
                "raw": "@race",
                "offer": offer,
                "days": 0,
                "count": 5,
            },
        )
        calls = []
        started, release = Event(), Event()

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            return Message(
                message_id=124,
                date=datetime.now(UTC),
                from_user=bot_user,
                chat=Chat(id=actors[0], type="private"),
            )

        def delayed(*_args, **_kwargs):
            started.set()
            if not release.wait(10):
                raise AssertionError("Test timed out")
            if failure:
                raise YouTubeError("Synthetic API failure")
            return snapshot() if export else (snapshot(), False)

        async def feed(data):
            callback = CallbackQuery(
                id=data,
                chat_instance="test",
                data=data,
                from_user=TelegramUser(id=actors[0], is_bot=False, first_name="Synthetic expert"),
                message=Message(
                    message_id=123,
                    date=datetime.now(UTC),
                    from_user=bot_user,
                    chat=Chat(id=actors[0], type="private"),
                ),
            )
            await dispatcher.feed_update(bot, Update(update_id=123, callback_query=callback))

        target = (
            "leedbox.telegram.youtube.cached_snapshot"
            if export
            else "leedbox.telegram.youtube.analyze"
        )
        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
                patch(target, side_effect=delayed),
            ):
                task = asyncio.create_task(
                    feed("yt:race:csv:download" if export else "yt:race:count:5")
                )
                assert await asyncio.to_thread(started.wait, 5)
                if destination == "audience":
                    await feed("section:audience")
                    data = await state.get_data()
                    assert data["paused"] and data["active_tool"] is None
                elif destination == "recipient":
                    await state.update_data(mode="recipient")
                else:
                    await state.update_data(paused=True)
                before = len([c for c in calls if isinstance(c, (SendMessage, SendDocument))])
                release.set()
                await asyncio.wait_for(task, 10)
                after = len([c for c in calls if isinstance(c, (SendMessage, SendDocument))])
                assert after == before
        finally:
            release.set()
            await bot.session.close()

    asyncio.run(run())
