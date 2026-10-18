import asyncio
import base64
import io
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import test_admin_postgres
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from sqlalchemy.orm import Session
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application.settings import set_setting, setting
from leedbox.application.versions import fingerprint
from leedbox.infrastructure.banner_assets import (
    MAX_BANNER_BYTES,
    banner_source,
    image_extension,
    save_banner,
    validate_banner,
)
from leedbox.telegram.button_styles import ButtonStyleMiddleware, button_style
from leedbox.telegram.handlers import build_router
from leedbox.telegram.services import service_pages

admin_fixture = test_admin_postgres.admin_fixture
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jTioAAAAASUVORK5CYII="
)


def test_banner_upload_is_immutable_and_paths_are_confined(tmp_path):
    key = save_banner(PNG, tmp_path)
    assert image_extension(PNG) == "png"
    assert save_banner(PNG, tmp_path) == key
    assert len(list(tmp_path.iterdir())) == 1
    assert banner_source(key, tmp_path).read_bytes() == PNG
    assert banner_source("default", tmp_path).is_file()
    assert banner_source("off", tmp_path) is None
    for value in ("../../secret", "upload:../secret.png", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            validate_banner(value)
    for data in (b"<svg></svg>", b"", b"a" * (MAX_BANNER_BYTES + 1)):
        with pytest.raises(ValueError):
            save_banner(data, tmp_path)


@pytest.mark.parametrize(
    "label,style",
    [
        ("⬅️ Назад", "danger"),
        ("🏠 В главное меню", "danger"),
        ("💬 Связаться", "success"),
        ("📋 Копировать", "success"),
        ("👤 Профиль", None),
    ],
)
def test_native_button_colors(label, style):
    assert button_style(label) == style


def test_outgoing_middleware_styles_url_and_copy_buttons_without_overriding_explicit_style():
    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="💬 Связаться", url="https://example.com"),
                    InlineKeyboardButton(text="Назад", callback_data="back"),
                    InlineKeyboardButton(text="Начать", callback_data="start", style="primary"),
                ]
            ]
        )

        async def send(_bot, method):
            assert [button.style for button in method.reply_markup.inline_keyboard[0]] == [
                "success",
                "danger",
                "primary",
            ]
            return True

        try:
            await ButtonStyleMiddleware()(
                send, bot, SendMessage(chat_id=1, text="test", reply_markup=markup)
            )
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_long_services_paginate_without_losing_content_or_html_escaping():
    from types import SimpleNamespace

    items = [
        SimpleNamespace(
            title=f"Услуга {i} <script>",
            price_label="1 ₽",
            duration="1 день",
            description="x" * 1000,
            conditions="y" * 500,
        )
        for i in range(8)
    ]
    pages = service_pages(items, "🤖 Услуги")
    assert len(pages) > 1
    assert all(len(page) < 4096 for page in pages)
    assert "<script>" not in "".join(pages)
    assert all(f"Услуга {i}" in "".join(pages) for i in range(8))


@pytest.mark.integration
def test_admin_banner_upload_auth_csrf_conflict_and_preview(admin_fixture, tmp_path):
    client, csrf, config, fixture, _ = admin_fixture
    engine, _, actors, *_ = fixture
    config.owner_media_directory = tmp_path
    url = "/owner/settings/edit?key=main_menu_banner"
    page = client.get(url)
    assert page.status_code == 200 and 'enctype="multipart/form-data"' in page.text
    with Session(engine) as db:
        expected = fingerprint(setting(db, "main_menu_banner"))
    fields = {"csrf_token": csrf, "confirmed": "1", "expected": expected, "value": "default"}
    assert (
        client.post(
            url, data={**fields, "csrf_token": "bad", "banner_file": (io.BytesIO(PNG), "x.png")}
        ).status_code
        == 403
    )
    assert list(tmp_path.iterdir()) == []
    assert (
        client.post(
            url, data={**fields, "banner_file": (io.BytesIO(PNG), "../../x.png")}
        ).status_code
        == 302
    )
    assert client.get("/owner/settings/banner-image").data == PNG
    assert "изменилась" in client.post(url, data=fields).text
    with Session(engine) as db, db.begin():
        set_setting(db, config, actors[0], "main_menu_banner", "off")
    page = client.get(url)
    assert 'enctype="multipart/form-data"' in page.text
    assert "<figure>" not in page.text
    assert client.get("/owner/settings/banner-image").status_code == 404
    client.post("/owner/logout", data={"csrf_token": csrf})
    assert client.get("/owner/settings/banner-image").status_code == 302


@pytest.mark.integration
@pytest.mark.parametrize(
    "banner,long_text,rejected",
    [
        ("default", False, False),
        ("off", False, False),
        ("default", True, False),
        ("https://example.com/banner.png", False, True),
    ],
)
def test_main_banner_fallback_and_caption_limits(admin_fixture, banner, long_text, rejected):
    _, _, config, fixture, _ = admin_fixture
    engine, _, actors, *_ = fixture
    with Session(engine) as db, db.begin():
        set_setting(db, config, actors[0], "main_menu_banner", banner)

    async def scenario():
        bot = Bot("123456:TEST-TOKEN-ONLY")
        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.include_router(build_router(engine, config))
        user = User(id=actors[1], is_bot=False, first_name="Test")
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=user.id, type="private"),
            from_user=user,
            text="/start",
        )
        calls = []

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, SendPhoto) and rejected:
                raise TelegramBadRequest(method=method, message="invalid photo")
            return Message(message_id=2, date=datetime.now(UTC), chat=message.chat)

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                if long_text:
                    with patch(
                        "leedbox.telegram.handlers.ui_text",
                        side_effect=lambda _e, key: (
                            "x" * 1500 if key == "expert_start" else "Раздел"
                        ),
                    ):
                        await dispatcher.feed_update(bot, Update(update_id=1, message=message))
                else:
                    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
            if banner == "off" or long_text or rejected:
                assert isinstance(calls[-1], SendMessage) and calls[-1].reply_markup
            else:
                assert isinstance(calls[-1], SendPhoto) and calls[-1].reply_markup
                assert len(calls[-1].caption) <= 1024
            if long_text:
                assert calls[0].caption is None
                assert len(calls[-1].text) == 1500
        finally:
            await bot.session.close()
            await dispatcher.storage.close()

    asyncio.run(scenario())
