import asyncio
import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import test_admin_postgres
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.sections import SECTIONS, visibility
from leedbox.application.settings import set_setting
from leedbox.application.versions import fingerprint
from leedbox.infrastructure.models import AuditLog
from leedbox.telegram.handlers import build_router
from leedbox.telegram.storage import PostgresStorage

admin_fixture = test_admin_postgres.admin_fixture
RENDER_SNAPSHOTS = {}
RENDER_ASSETS = {}


@pytest.mark.integration
def test_sections_admin_csrf_atomic_save_stale_and_audit(admin_fixture):
    client, csrf, config, fixture, _ = admin_fixture
    engine, _, actors, *_ = fixture
    page = client.get("/owner/sections/")
    assert page.status_code == 200 and "Разделы бота" in page.text
    RENDER_SNAPSHOTS["sections"] = page.get_data()
    for path in re.findall(rb'(?:src|href)="(/(?:owner/)?static/[^"?]+)', page.get_data()):
        asset = client.get(path.decode())
        RENDER_ASSETS[path.decode()] = (asset.get_data(), asset.content_type)
    for code in SECTIONS:
        assert f'name="{code}"' in page.text
    with Session(engine) as db:
        old = visibility(db)
    form = {code: "1" for code, shown in old.items() if shown and code != "links"}
    form.update(expected=fingerprint(old), confirmed="1")
    assert client.post("/owner/sections/", data=form).status_code == 403
    unconfirmed = {**form, "csrf_token": csrf}
    unconfirmed.pop("confirmed")
    assert "Подтвердите" in client.post("/owner/sections/", data=unconfirmed).text
    with Session(engine) as db:
        assert visibility(db) == old
    response = client.post("/owner/sections/", data={**form, "csrf_token": csrf})
    assert response.status_code == 302
    with Session(engine) as db:
        saved = visibility(db)
        assert not saved["links"] and all(saved[c] == old[c] for c in SECTIONS if c != "links")
        assert db.scalar(
            select(AuditLog).where(
                AuditLog.actor_telegram_id == actors[0], AuditLog.action == "setting_changed"
            )
        )
    assert (
        "уже изменились" in client.post("/owner/sections/", data={**form, "csrf_token": csrf}).text
    )
    restore = {code: "1" for code in SECTIONS}
    restore.update(csrf_token=csrf, expected=fingerprint(saved), confirmed="1")
    assert client.post("/owner/sections/", data=restore).status_code == 302
    assert client.get("/owner/settings/edit?key=section_links_visible").status_code == 302
    assert "section_links_visible" not in client.get("/owner/settings/").text


@pytest.mark.integration
def test_bot_live_hide_blocks_old_buttons_commands_preserves_draft_and_gifts(admin_fixture):
    _, _, config, fixture, _ = admin_fixture
    engine, _, actors, *_ = fixture

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        bot_user = User(id=bot.id, is_bot=True, first_name="Synthetic")
        storage = PostgresStorage(engine)
        dispatcher = Dispatcher(storage=storage)
        dispatcher.include_router(build_router(engine, config))
        state = dispatcher.fsm.get_context(bot=bot, chat_id=actors[0], user_id=actors[0])
        calls, sequence, screen = [], 0, 1000

        async def api(_bot, method, timeout=None):
            nonlocal screen
            calls.append(method)
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, AnswerCallbackQuery):
                return True
            screen += 1
            return Message(
                message_id=screen,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=bot_user,
            )

        async def send(value, callback=False):
            nonlocal sequence
            sequence += 1
            user = User(id=actors[0], is_bot=False, first_name="Expert")
            message = Message(
                message_id=screen if callback else sequence,
                date=datetime.now(UTC),
                chat=Chat(id=actors[0], type="private"),
                from_user=bot_user if callback else user,
                text=None if callback else value,
            )
            update = (
                Update(
                    update_id=sequence,
                    callback_query=CallbackQuery(
                        id=str(sequence),
                        chat_instance="fixture",
                        data=value,
                        message=message,
                        from_user=user,
                    ),
                )
                if callback
                else Update(update_id=sequence, message=message)
            )
            await dispatcher.feed_update(bot, update)

        def toggle(code, enabled):
            with Session(engine) as db, db.begin():
                set_setting(db, config, actors[0], f"section_{code}_visible", enabled)

        def latest_menu():
            return [call for call in calls if isinstance(call, (SendMessage, SendPhoto))][-1]

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await send("/start")
                assert "section:links" in str(latest_menu().reply_markup)
                await send("ut:new:vk", True)
                draft = (await state.get_data())["utm_draft"]
                toggle("links", False)
                await send("example.com")
                assert "временно скрыт" in calls[-1].text
                assert (await state.get_data())["utm_draft"] == draft
                for code in ("section:links", "ut:new:vk", "sl:new:short"):
                    await send(code, True)
                    assert isinstance(calls[-1], AnswerCallbackQuery) and calls[-1].show_alert
                    assert "временно скрыт" in calls[-1].text
                await send("/utm")
                assert "временно скрыт" in calls[-1].text
                await send("/menu")
                assert "section:links" not in str(latest_menu().reply_markup)
                toggle("links", True)
                await send("/menu")
                assert "section:links" in str(latest_menu().reply_markup)
                await send("ut:new:vk", True)
                restored = await state.get_data()
                assert restored["utm_draft"]["token"] == draft["token"]
                assert not restored["paused"]
                # YouTube is nested: both the individual and the parent switch
                # apply to the catalogue, old callbacks, commands and text input.
                toggle("youtube", False)
                await send("section:agents", True)
                assert "section:youtube" not in str(latest_menu().reply_markup)
                assert "YouTube" not in latest_menu().text
                for value, callback in (("/youtube", False), ("section:youtube", True)):
                    await send(value, callback)
                    assert "временно скрыт" in calls[-1].text
                toggle("youtube", True)
                await send("section:agents", True)
                assert "section:youtube" in str(latest_menu().reply_markup)
                toggle("agents", False)
                await send("/menu")
                assert "section:agents" not in str(latest_menu().reply_markup)
                for value, callback in (
                    ("/youtube", False),
                    ("section:youtube", True),
                    ("yt:old:period:30", True),
                ):
                    await send(value, callback)
                    assert "временно скрыт" in calls[-1].text
                await state.update_data(active_tool="youtube", paused=False)
                await send("@synthetic_channel")
                assert "временно скрыт" in calls[-1].text
                toggle("agents", True)
                await send("section:agents", True)
                assert "section:youtube" in str(latest_menu().reply_markup)
                # Published gift URLs remain recipient workflows even when creation is hidden.
                toggle("materials", False)
                await send("/start lm_not-found")
                assert (await state.get_data())["mode"] == "recipient"
                assert not any(
                    "временно скрыт" in (getattr(call, "text", "") or "") for call in calls[-2:]
                )
        finally:
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())
