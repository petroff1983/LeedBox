import asyncio
import io
import re
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import test_admin_postgres
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.orm import Session
from test_banner_and_styles import PNG
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.settings import set_setting, setting
from leedbox.application.ui_texts import read_text, set_text
from leedbox.application.versions import fingerprint
from leedbox.infrastructure.banner_assets import consultation_source
from leedbox.telegram.handlers import build_router
from leedbox.web.consultation_admin import page_values

admin_fixture = test_admin_postgres.admin_fixture
RENDER_SNAPSHOTS = {}
RENDER_ASSETS = {}


def test_default_portrait_and_disable(tmp_path):
    image = consultation_source("default", tmp_path)
    assert image.is_file() and image.name == "consultation-portrait-v1.png"
    assert consultation_source("off", tmp_path) is None
    assert (
        consultation_source("https://example.com/photo.jpg", tmp_path)
        == "https://example.com/photo.jpg"
    )


@pytest.mark.integration
def test_consultation_admin_upload_links_csrf_conflict_atomic_validation(admin_fixture, tmp_path):
    client, csrf, config, fixture, _ = admin_fixture
    engine, _, actors, *_ = fixture
    config.owner_media_directory = tmp_path
    url = "/owner/consultations/"
    page = client.get(url)
    assert page.status_code == 200 and "Приветствие от создателя" in page.text
    assert client.get(url + "photo").status_code == 200
    RENDER_SNAPSHOTS["consultations"] = page.data
    for path in re.findall(
        rb'(?:src|href)="(/(?:owner/)?static/[^"?]+|/owner/consultations/photo)', page.data
    ):
        asset = client.get(path.decode())
        RENDER_ASSETS[path.decode()] = (asset.data, asset.content_type)
    with Session(engine) as db:
        before = page_values(db)
    data = {
        **before,
        "csrf_token": csrf,
        "confirmed": "1",
        "expected": fingerprint(before),
        "consultations_greeting": "Привет, эксперт! Добро пожаловать.",
        "consultation_channel_url": "@test_channel",
        "consultation_blog_url": "https://example.com/blog",
        "consultation_contact_url": "https://t.me/test_contact",
    }
    assert client.post(url, data={**data, "csrf_token": "invalid"}).status_code == 403
    invalid = {**data, "consultation_blog_url": "javascript:alert(1)"}
    assert "http" in client.post(url, data=invalid).text.lower()
    with Session(engine) as db:
        assert page_values(db) == before
    payload = {**data, "photo_file": (io.BytesIO(PNG), "../../portrait.png")}
    assert client.post(url, data=payload).status_code == 302
    assert client.get(url + "photo").data == PNG
    with Session(engine) as db:
        assert setting(db, "consultation_photo").startswith("upload:")
        assert read_text(db, "consultations_greeting") == data["consultations_greeting"]
        assert read_text(db, "consultations_intro") == before["consultations_intro"]
    assert "изменились" in client.post(url, data=data).text
    with Session(engine) as db:
        current = page_values(db)
    excessive = {**data, "expected": fingerprint(current), "consultations_greeting": "a" * 401}
    assert "до 400" in client.post(url, data=excessive).text
    with Session(engine) as db:
        assert page_values(db) == current
    assert client.get("/owner/settings/edit?key=consultation_photo").status_code == 302
    client.post("/owner/logout", data={"csrf_token": csrf})
    assert client.get(url).status_code == 302 and client.get(url + "photo").status_code == 302


@pytest.mark.integration
@pytest.mark.parametrize(
    "photo,long_text,rejected",
    [
        ("default", False, False),
        ("off", False, False),
        ("default", True, False),
        ("https://example.com/photo.jpg", False, True),
    ],
)
def test_consultation_photo_caption_links_limits_and_fallback(
    admin_fixture, photo, long_text, rejected
):
    _, _, config, fixture, _ = admin_fixture
    engine, _, actors, *_ = fixture
    body = "🧠" * 2000 if long_text else "🎓 Темы и цены консультаций"
    with Session(engine) as db, db.begin():
        for key, value in {
            "consultation_photo": photo,
            "consultation_channel_url": "@test_channel",
            "consultation_blog_url": "https://example.com/blog",
            "consultation_contact_url": "@test_contact",
        }.items():
            set_setting(db, config, actors[0], key, value)
        set_text(
            db,
            config,
            actors[0],
            "consultations_intro",
            body,
            fingerprint(read_text(db, "consultations_intro")),
        )

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(build_router(engine, config))
        actor = User(id=actors[1], is_bot=False, first_name="Expert")
        bot_user = User(id=bot.id, is_bot=True, first_name="Synthetic")
        state = dp.fsm.get_context(bot=bot, chat_id=actor.id, user_id=actor.id)
        await state.update_data(mode="expert", paused=True)
        calls = []

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, AnswerCallbackQuery):
                return True
            if isinstance(method, SendPhoto) and rejected:
                raise TelegramBadRequest(method=method, message="invalid photo")
            return Message(
                message_id=len(calls),
                date=datetime.now(UTC),
                chat=Chat(id=actor.id, type="private"),
                from_user=bot_user,
            )

        async def callback(code, number):
            await dp.feed_update(
                bot,
                Update(
                    update_id=number,
                    callback_query=CallbackQuery(
                        id=str(number),
                        chat_instance="fixture",
                        data=code,
                        from_user=actor,
                        message=Message(
                            message_id=10,
                            date=datetime.now(UTC),
                            chat=Chat(id=actor.id, type="private"),
                            from_user=bot_user,
                        ),
                    ),
                ),
            )

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                await callback("section:consultations", 1)
                sends = [call for call in calls if isinstance(call, (SendPhoto, SendMessage))]
                assert sends
                last = sends[-1]
                content = "".join(
                    (getattr(call, "caption", "") or getattr(call, "text", "")) for call in sends
                )
                assert "Привет, эксперт" in content and body in content
                assert all(
                    len(call.caption.encode("utf-16-le")) // 2 <= 1024
                    for call in sends
                    if isinstance(call, SendPhoto)
                )
                assert all(
                    len(call.text.encode("utf-16-le")) // 2 <= 4096
                    for call in sends
                    if isinstance(call, SendMessage)
                )
                grid = last.reply_markup.inline_keyboard
                assert grid[0][0].url == "https://t.me/test_contact"
                assert grid[0][0].style == "success"
                assert [button.url for button in grid[1]] == [
                    "https://t.me/test_channel",
                    "https://example.com/blog",
                ]
                assert grid[-1][0].callback_data == "menu"
                if photo != "off" and not rejected and not long_text:
                    assert len(sends) == 1 and isinstance(last, SendPhoto)
                await state.update_data(mode="recipient")
                previous = len(calls)
                await callback("section:consultations", 2)
                assert not any(
                    isinstance(call, (SendPhoto, SendMessage)) for call in calls[previous:]
                )
                assert calls[-1].show_alert
        finally:
            await bot.session.close()
            await dp.storage.close()

    asyncio.run(scenario())
