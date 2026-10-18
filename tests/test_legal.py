# ruff: noqa: F811
"""Legal HTML and consent checks use only dummy data and the isolated test database."""

import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_admin_postgres import admin_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

from leedbox.application import agents, broadcasts, legal
from leedbox.application.settings import set_setting, setting
from leedbox.infrastructure.models import ContentVersion, LegalConsentEvent, Setting
from leedbox.telegram.handlers import build_router
from leedbox.web.app import create_app

BODY = "<h2>Тестовый документ</h2><p>Синтетический текст политики и согласия для проверки.</p>"


@pytest.fixture
def legal_fixture(service_fixture):
    engine, config, actors, owners, *_ = service_fixture
    config = config.model_copy(
        update={"bot_token": SecretStr("123456:synthetic_test_token_without_real_bot")}
    )
    with Session(engine) as db:
        old = {
            r.key: r.value
            for r in db.scalars(select(Setting).where(Setting.key.startswith("legal_")))
        }
        ids = list(
            db.scalars(
                select(ContentVersion.id).where(ContentVersion.entity == "legal_publication")
            )
        )
    try:
        yield engine, config, actors, owners, 123456
    finally:
        with Session(engine) as db, db.begin():
            added = list(
                db.scalars(
                    select(ContentVersion.id).where(
                        ContentVersion.entity == "legal_publication",
                        ContentVersion.id.not_in(ids),
                    )
                )
            )
            db.execute(delete(LegalConsentEvent).where(LegalConsentEvent.version_id.in_(added)))
            db.execute(delete(ContentVersion).where(ContentVersion.id.in_(added)))
            db.execute(delete(Setting).where(Setting.key.startswith("legal_")))
            db.add_all(Setting(key=k, value=v) for k, v in old.items())


def publish(fixture, slug="personal-data", suffix=""):
    engine, config, actors, *_ = fixture
    with Session(engine) as db, db.begin():
        doc = legal.save(
            db,
            config,
            actors[0],
            slug,
            legal.DOCUMENTS[slug],
            BODY + suffix,
            legal.editor_stamp(db, slug),
            True,
        )
        return doc.id, doc.number


def prepare(fixture):
    consent, _ = publish(fixture)
    policy, _ = publish(fixture, "privacy")
    engine, config, actors, *_ = fixture
    with Session(engine) as db, db.begin():
        set_setting(db, config, actors[0], "legal_gate_enabled", True)
    return consent, policy


@pytest.mark.parametrize(
    "payload",
    [
        "<script>CANARY</script>",
        '<iframe src="https://evil.test">CANARY</iframe>',
        "<svg><script>CANARY</script></svg>",
        '<form action="https://evil.test"><input name="CANARY"></form>',
        '<img src=x onerror="CANARY">',
        '<a href="javascript:CANARY">текст</a>',
        '<a href="&#x6a;avascript:CANARY">текст</a>',
        '<a href="data:text/html,CANARY">текст</a>',
        '<p style="background:url(https://evil.test/CANARY)" onclick="CANARY">текст</p>',
        '<math><mtext><img src=x onerror="CANARY"></mtext></math>',
    ],
)
def test_html_cannot_execute_or_load_remote_content(payload):
    result = legal.clean_html(payload)
    assert "CANARY" not in result and "<script" not in result and "<img" not in result


def test_formatting_links_tables_and_template_text_are_preserved():
    result = legal.clean_html(
        "<h2>Условия</h2><p><strong>Жирный</strong> {{ 7*7 }}</p>"
        '<a href="https://example.com/terms">Условия</a>'
        '<table><tr><td colspan="2">Реквизиты</td></tr></table>'
    )
    assert "<h2>Условия</h2>" in result and "<strong>Жирный</strong>" in result
    assert 'colspan="2"' in result and "noopener noreferrer" in result
    assert "{{ 7*7 }}" in result
    assert legal.clean_html(result) == result


def test_draft_publication_old_versions_and_stale_save(legal_fixture):
    engine, config, actors, *_ = legal_fixture
    with Session(engine) as db, db.begin():
        expected = legal.editor_stamp(db, "personal-data")
        legal.save(db, config, actors[0], "personal-data", "Первый черновик", BODY, expected)
        assert legal.current(db, "personal-data") is None
    first, number = publish(legal_fixture)
    with Session(engine) as db:
        stale = legal.editor_stamp(db, "personal-data")
    second, new_number = publish(legal_fixture, suffix="<p>Новая редакция</p>")
    assert first != second and new_number == number + 1
    with Session(engine) as db:
        assert legal.version(db, "personal-data", number).id == first
        assert legal.current(db, "personal-data").id == second
        with pytest.raises(ValueError, match="уже изменён"):
            legal.save(db, config, actors[0], "personal-data", "Старое окно", BODY, stale, True)
        with pytest.raises(PermissionError):
            legal.save(db, config, actors[1], "personal-data", "Чужой", BODY, stale)


def test_concurrent_edit_has_one_winner(legal_fixture):
    engine, config, actors, *_ = legal_fixture
    with Session(engine) as db:
        expected = legal.editor_stamp(db, "offer")

    def save(i):
        try:
            with Session(engine) as db, db.begin():
                legal.save(db, config, actors[0], "offer", f"Оферта {i}", BODY, expected)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(save, [1, 2])) == [False, True]


def test_empty_documents_cannot_enable_gate(legal_fixture):
    engine, config, actors, *_ = legal_fixture
    with Session(engine) as db:
        with pytest.raises(ValueError, match="Сначала опубликуйте"):
            set_setting(db, config, actors[0], "legal_gate_enabled", True)
        assert not setting(db, "legal_gate_enabled")
        with pytest.raises(ValueError, match="пустой"):
            legal.save(
                db,
                config,
                actors[0],
                "privacy",
                "Политика",
                "<script>x</script>",
                legal.editor_stamp(db, "privacy"),
                True,
            )


def test_consents_versioned_isolated_idempotent_and_revocable(legal_fixture):
    engine, config, actors, owners, bot = legal_fixture
    consent, policy = prepare(legal_fixture)
    with Session(engine) as db, db.begin():
        legal.grant(db, config, actors[1], bot, consent, policy, "accept-one", 12)
        legal.grant(db, config, actors[1], bot, consent, policy, "accept-one", 12)
        assert legal.accepted(db, actors[1], bot)
        assert not legal.accepted(db, actors[2], bot)
        assert not legal.accepted(db, actors[1], bot + 1)
        assert len(list(db.scalars(select(LegalConsentEvent)))) == 1
        legal.require_expert(db, config, owners[1], bot)
        legal.revoke(db, actors[1], bot, "withdraw-one", 13)
        assert not legal.accepted(db, actors[1], bot)
        legal.grant(db, config, actors[1], bot, consent, policy, "accept-one", 12)
        assert not legal.accepted(db, actors[1], bot)  # Old callback cannot undo revocation.
        with pytest.raises(ValueError, match="согласие"):
            legal.require_expert(db, config, owners[1], bot)
    publish(legal_fixture, suffix="<p>Новые условия</p>")
    with Session(engine) as db, pytest.raises(ValueError, match="обновились"):
        legal.grant(db, config, actors[1], bot, consent, policy, "stale", 14)


def test_agents_cannot_bypass_consent_in_application_layer(legal_fixture):
    engine, config, actors, owners, bot = legal_fixture
    prepare(legal_fixture)
    with Session(engine) as db, pytest.raises(ValueError, match="согласие"):
        agents.allowed(db, owners[1], bot, True, "content", config)


def test_advertising_is_separate_and_old_version_is_not_current(legal_fixture):
    engine, config, actors, owners, bot = legal_fixture
    consent, policy = prepare(legal_fixture)
    publish(legal_fixture, "advertising")
    with Session(engine) as db, db.begin():
        legal.grant(db, config, actors[1], bot, consent, policy, "personal-only", 11)
        assert not broadcasts.preferences(db, actors[1], bot).advertising
        with pytest.raises(ValueError, match="рекламу"):
            broadcasts.change_preference(db, actors[1], bot, "advertising", True)
        broadcasts.change_preference(
            db, actors[1], bot, "advertising", True, broadcasts.advertising_version(db)
        )
        assert broadcasts.audience_count(db, bot, "advertising") == 1
    publish(legal_fixture, "advertising", "<p>Новая версия рекламы</p>")
    with Session(engine) as db:
        assert broadcasts.audience_count(db, bot, "advertising") == 0
        assert legal.accepted(db, actors[1], bot)


def test_public_routes_hide_drafts_and_never_render_jinja(legal_fixture):
    engine, config, actors, *_ = legal_fixture
    app = create_app(config.model_copy(update={"admin_enabled": False}), engine)
    client = app.test_client()
    assert client.get("/legal/privacy").status_code == 404
    publish(legal_fixture, "privacy", "<p>{{ 7*7 }}</p><script>CANARY</script>")
    page = client.get("/legal/privacy")
    assert page.status_code == 200 and "{{ 7*7 }}" in page.text
    assert "CANARY" not in page.text and "script-src" not in page.headers["Content-Security-Policy"]
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert client.get("/legal").status_code == 200
    assert client.get("/legal/unknown").status_code == 404
    assert client.get("/legal/privacy/v/999999").status_code == 404


def test_admin_html_editor_csrf_import_preview_and_publishing(legal_fixture, admin_fixture):
    client, csrf, config, _, _ = admin_fixture
    engine, _, actors, *_ = legal_fixture
    url = "/owner/legal/edit?slug=privacy"
    with Session(engine) as db:
        expected = legal.editor_stamp(db, "privacy")
    data = {
        "title": "Тестовая политика",
        "html": BODY,
        "expected": expected,
        "action": "publish",
        "confirmed": "1",
    }
    assert client.get(url).status_code == 200
    assert client.post(url, data=data).status_code == 403
    data["csrf_token"] = csrf
    assert client.post(url, data={**data, "action": "preview"}).status_code == 200
    assert client.get("/legal/privacy").status_code == 404
    result = client.post(url, data=data)
    assert result.status_code == 302
    assert client.get("/legal/privacy").status_code == 200
    assert "уже изменён" in client.post(url, data=data).text
    imported = client.post(
        url,
        data={
            **data,
            "action": "import",
            "document": (io.BytesIO((BODY + "<script>CANARY</script>").encode()), "policy.html"),
        },
    )
    assert imported.status_code == 200 and "CANARY" not in imported.text
    with Session(engine) as db:
        published = legal.current(db, "privacy").id
    with Session(engine) as db:
        assert legal.current(db, "privacy").id == published
    page = client.get("/owner/legal/")
    assert page.status_code == 200 and "Документы и согласия" in page.text
    assert "vendor/quill/quill.js" in client.get(url).text
    assert client.get("/static/vendor/quill/quill.js").status_code == 200


def test_telegram_start_stale_buttons_grant_revoke_and_gift_bypass(legal_fixture):
    engine, config, actors, owners, bot_id = legal_fixture
    prepare(legal_fixture)

    async def scenario():
        bot = Bot(config.bot_token.get_secret_value())
        storage = MemoryStorage()
        dp = Dispatcher(storage=storage)
        dp.include_router(build_router(engine, config))
        context = dp.fsm.get_context(bot=bot, chat_id=actors[1], user_id=actors[1])
        actor = User(id=actors[1], is_bot=False, first_name="Dummy")
        me = User(id=bot_id, is_bot=True, first_name="DummyBot", username="synthetic_bot")
        calls, counter = [], 0

        async def api(_bot, method, **kwargs):
            calls.append(method)
            if isinstance(method, GetMe):
                return me
            if isinstance(method, AnswerCallbackQuery):
                return True
            return Message(
                message_id=counter + 100,
                date=datetime.now(UTC),
                chat=Chat(id=actor.id, type="private"),
                from_user=me,
            )

        async def feed(value, callback=False):
            nonlocal counter
            counter += 1
            msg = Message(
                message_id=counter,
                date=datetime.now(UTC),
                chat=Chat(id=actor.id, type="private"),
                from_user=me if callback else actor,
                text=None if callback else value,
            )
            update = (
                Update(
                    update_id=counter,
                    callback_query=CallbackQuery(
                        id=str(counter),
                        from_user=actor,
                        chat_instance="test",
                        data=value,
                        message=msg,
                    ),
                )
                if callback
                else Update(update_id=counter, message=msg)
            )
            await dp.feed_update(bot, update)

        try:
            with (
                patch.object(bot.session, "make_request", side_effect=api),
                patch("leedbox.telegram.handlers.recipient_start", new_callable=AsyncMock) as gift,
            ):
                await feed("/start")
                assert "Перед началом работы" in calls[-1].text
                old = (await context.get_data())["legal_offer"]["nonce"]
                await feed("Мой текст пока без согласия")
                fresh = (await context.get_data())["legal_offer"]["nonce"]
                await feed("legal:grant:" + old, True)
                assert calls[-1].show_alert and "устарел" in calls[-1].text
                await feed("legal:grant:" + fresh, True)
                assert (await context.get_data())["mode"] == "expert"
                with Session(engine) as db:
                    assert legal.accepted(db, actor.id, bot_id)
                    assert not broadcasts.preferences(db, actor.id, bot_id).advertising
                await feed("/start")
                assert (await context.get_data()).get("legal_offer") is None
                await feed("/documents")
                assert "подтверждено" in calls[-1].text
                await feed("legal:revoke", True)
                token = (await context.get_data())["legal_revoke"]
                await feed("legal:withdraw:" + token, True)
                await feed("ca:new:manual", True)
                assert "Перед началом работы" in calls[-1].text
                await feed("/start lm_abcdef123456")
                gift.assert_awaited_once()
                assert (await context.get_data())["mode"] == "recipient"
                with Session(engine) as db:
                    assert not legal.accepted(db, actor.id, bot_id)
        finally:
            await context.clear()
            await bot.session.close()
            await storage.close()

    asyncio.run(scenario())
