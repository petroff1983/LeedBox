import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import test_links_postgres
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leedbox.application.audience import create_source
from leedbox.application.audience_dashboard import PAGE_SIZE, Dashboard, period_for
from leedbox.application.cabinet_access import consume_ticket, digest, issue_ticket, session_owner
from leedbox.application.entitlements import start_expert
from leedbox.application.links import record_start
from leedbox.application.materials import publish
from leedbox.application.profile import delete_my_data
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import Content
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    CabinetCredential,
    DeliveryEvent,
    MaterialVersion,
    ProductEvent,
    Recipient,
    User,
)
from leedbox.telegram.handlers import build_router, cabinet_button, menu
from leedbox.telegram.storage import PostgresStorage
from leedbox.web.app import create_app
from leedbox.web.cabinet import COOKIE

link_fixture = test_links_postgres.link_fixture


@pytest.fixture
def cabinet_fixture(link_fixture):
    engine, config, actors, owners, projects = link_fixture
    config = config.model_copy(
        update={
            "web_secret_key": SecretStr(token_urlsafe(48)),
            "admin_enabled": False,
            "public_base_url": None,
        }
    )
    with Session(engine) as db, db.begin():
        for owner in owners:
            start_expert(db, db.get(User, owner))
    return engine, config, actors, owners, projects


def ticket_for(engine, config, actor):
    with Session(engine) as db, db.begin():
        return urlsplit(issue_ticket(db, config, actor)).fragment.removeprefix("token=")


def login(client, ticket):
    response = client.get("/cabinet/login")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', response.text)[1]
    return client.post("/cabinet/login", data={"csrf": csrf, "token": ticket})


def gift(db, owner, project, bot, title):
    return publish(db, owner, project, bot, Content(title, "", "text", "Подарок"), "everyone", None)


@pytest.mark.integration
def test_one_time_login_cookie_isolation_replay_and_logout(cabinet_fixture):
    engine, config, actors, owners, _ = cabinet_fixture
    ticket = ticket_for(engine, config, actors[0])
    app = create_app(config, engine)
    client = app.test_client()
    assert client.get("/cabinet/").status_code == 302
    assert client.get("/cabinet/login?token=" + ticket).status_code == 200
    with Session(engine) as db:
        assert db.get(CabinetCredential, digest(ticket)) is not None  # GET cannot consume
    assert client.post("/cabinet/login", data={"token": ticket}).status_code == 403
    response = login(client, ticket)
    assert response.status_code == 303
    cookie = client.get_cookie(COOKIE, path="/cabinet")
    assert cookie.http_only and cookie.same_site == "Lax" and cookie.path == "/cabinet"
    assert "leedbox_owner" not in response.headers.get("Set-Cookie", "")
    with Session(engine) as db:
        assert session_owner(db, config, cookie.value) == owners[0]
    other = app.test_client()
    assert login(other, ticket).status_code == 401
    page = client.get("/cabinet/")
    assert page.status_code == 200 and "Моя аудитория" in page.text
    assert "no-store" in page.headers["Cache-Control"]
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert cookie.value not in page.text and ticket not in page.text
    assert client.get("/owner/").status_code == 404
    assert client.post("/cabinet/logout").status_code == 403
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)[1]
    assert client.post("/cabinet/logout", data={"csrf": csrf}).status_code == 303
    assert client.get("/cabinet/").status_code == 302
    with Session(engine) as db:
        assert session_owner(db, config, cookie.value) is None


@pytest.mark.integration
def test_tickets_expire_rotate_and_require_expert(cabinet_fixture):
    engine, config, actors, owners, _ = cabinet_fixture
    first = ticket_for(engine, config, actors[0])
    second = ticket_for(engine, config, actors[0])
    with Session(engine) as db, db.begin():
        with pytest.raises(ObjectUnavailable):
            consume_ticket(db, config, first)
        db.get(CabinetCredential, digest(second)).expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
        db.flush()
        with pytest.raises(ObjectUnavailable):
            consume_ticket(db, config, second)
        db.get(User, owners[1]).expert_started_at = None
        db.flush()
        with pytest.raises(ObjectUnavailable):
            issue_ticket(db, config, actors[1])
    third = ticket_for(engine, config, actors[0])
    with Session(engine) as db, db.begin():
        raw = consume_ticket(db, config, third)
    ticket_for(engine, config, actors[0])  # does not revoke established sessions
    with Session(engine) as db, db.begin():
        assert session_owner(db, config, raw) == owners[0]
        assert (
            session_owner(
                db, config.model_copy(update={"bot_token": SecretStr("765432:" + "x" * 30)}), raw
            )
            is None
        )
        db.get(CabinetCredential, digest(raw)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with Session(engine) as db:
        assert session_owner(db, config, raw) is None


@pytest.mark.integration
def test_ticket_concurrent_consumption_and_user_deletion(cabinet_fixture):
    engine, config, actors, owners, _ = cabinet_fixture
    ticket = ticket_for(engine, config, actors[0])

    def consume():
        with Session(engine) as db, db.begin():
            try:
                return consume_ticket(db, config, ticket)
            except ObjectUnavailable:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sum(raw is not None for raw in results) == 1
    with Session(engine) as db, db.begin():
        delete_my_data(db, actors[0])
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(CabinetCredential)
                .where(CabinetCredential.owner_user_id == owners[0])
            )
            == 0
        )


@pytest.mark.integration
def test_reports_unique_people_sources_periods_and_ownership(cabinet_fixture):
    engine, config, actors, owners, projects = cabinet_fixture
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        a = gift(db, owners[0], projects[0], bot, "Чек-лист")
        b = gift(db, owners[0], projects[0], bot, "Видео")
        foreign = gift(db, owners[1], projects[1], bot, "Чужой подарок")
        other_bot = gift(db, owners[0], projects[0], bot + 1, "Другой бот")
        yt = create_source(
            db,
            owners[0],
            bot,
            a.id,
            "YouTube",
            "Ролик 1",
            "https://youtube.com/watch?v=one",
            "test_bot",
            "cabinet:" + a.slug,
        )
        zen = create_source(
            db, owners[0], bot, b.id, "Дзен", "Статья 2", "", "test_bot", "cabinet:" + b.slug
        )
        for index, (material, person) in enumerate(
            [
                (a, 777001),
                (a, 777001),
                (b, 777001),
                (b, 777002),
                (foreign, 888001),
                (other_bot, 888002),
            ]
        ):
            db.add(
                ProductEvent(
                    bot_id=material.bot_id,
                    telegram_user_id=person,
                    name="user_started",
                    material_id=material.id,
                    details={"kind": "recipient"},
                    created_at=now,
                )
            )
            if index != 1:
                db.add(
                    Recipient(
                        material_id=material.id, telegram_user_id=person, first_delivered_at=now
                    )
                )
            version = db.scalar(
                select(MaterialVersion.id).where(MaterialVersion.material_id == material.id)
            )
            db.add(
                DeliveryEvent(
                    material_id=material.id,
                    version_id=version,
                    telegram_user_id=person,
                    action_key=f"cabinet:{material.slug}:{index}",
                    status="sent" if index != 3 else "failed",
                    created_at=now,
                )
            )
        record_start(db, bot, yt.payload, 777001, "cabinet:yt:" + a.slug)
        record_start(db, bot, zen.payload, 777001, "cabinet:zen:" + b.slug)
        db.flush()
        from leedbox.infrastructure.models import LinkEvent

        events = list(
            db.scalars(
                select(LinkEvent)
                .where(LinkEvent.link_id.in_([yt.id, zen.id]))
                .order_by(LinkEvent.id)
            )
        )
        events[0].created_at = now - timedelta(days=40)
        events[1].created_at = now
        a.archived = True  # historical reporting remains available
        db.flush()
        report = Dashboard(db, owners[0], bot, period_for("30"))
        assert report.summary() == {"people": 2, "starts": 4, "deliveries": 3, "recipients": 1}
        rows = report.materials("recipients")
        assert {row["title"] for row in rows} == {"Чек-лист", "Видео"}
        assert sum(row["recipients"] for row in rows) == 2  # never use this sum for global unique
        assert report.sources()[0]["source"] == "Дзен"
        people = {row["person"]: row for row in report.people()}
        assert people[777001]["first_source"] == "YouTube · Ролик 1"
        assert people[777001]["last_source"] == "Дзен · Статья 2"
        assert people[777001]["gifts"] == ["Чек-лист", "Видео"]
        assert len(report.people("Дзен")) == 1
        assert len(report.people(query="777002")) == 1
        assert Dashboard(db, owners[0], bot, period_for("all")).sources()[0]["people"] == 1
        for owner in owners[1:]:
            with pytest.raises(ObjectUnavailable):
                Dashboard(db, owner, bot, period_for("all"), a.id)
        with pytest.raises(ObjectUnavailable):
            Dashboard(db, owners[0], bot + 1, period_for("all"), a.id)
        foreign_id = foreign.id
    client = create_app(config, engine).test_client()
    assert login(client, ticket_for(engine, config, actors[0])).status_code == 303
    for view in ("overview", "materials", "sources", "people"):
        response = client.get(f"/cabinet/?view={view}&period=all")
        assert response.status_code == 200
        assert "Чужой подарок" not in response.text and "Другой бот" not in response.text
        assert client.get(f"/cabinet/?view={view}&material={foreign_id}").status_code == 404
    assert "Некорректный фильтр" in client.get("/cabinet/?material=invalid").text
    assert "числовой Telegram ID" in client.get("/cabinet/?view=people&q=abc").text


@pytest.mark.integration
def test_people_pagination_and_escaped_publications(cabinet_fixture):
    engine, config, actors, owners, projects = cabinet_fixture
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    with Session(engine) as db, db.begin():
        a = gift(db, owners[0], projects[0], bot, "<b>Подарок</b>")
        source = create_source(
            db,
            owners[0],
            bot,
            a.id,
            "YouTube",
            "<script>alert(1)</script>",
            "",
            "test_bot",
            "escape:" + a.slug,
        )
        source.external_url = "javascript:alert(1)"  # legacy corrupt data must not become a link
        for i in range(PAGE_SIZE + 3):
            record_start(db, bot, source.payload, 777010 + i, f"cabinet:page:{a.slug}:{i}")
        db.flush()
        report = Dashboard(db, owners[0], bot, period_for("all"))
        assert len(report.people()) == PAGE_SIZE + 1
        assert len(report.people(page=1)) == 3
        assert report.sources()[0]["external_url"] is None
    client = create_app(config, engine).test_client()
    login(client, ticket_for(engine, config, actors[0]))
    response = client.get("/cabinet/?view=sources")
    assert "&lt;script&gt;" in response.text and "javascript:" not in response.text
    assert "<b>Подарок</b>" not in response.text


@pytest.mark.parametrize(
    "value,start,end",
    [("bad", "", ""), ("custom", "bad", ""), ("custom", "2026-10-12", "2026-10-01")],
)
def test_invalid_period(value, start, end):
    with pytest.raises(ValueError):
        period_for(value, start, end)


def test_period_boundaries_and_menu():
    period = period_for("7", now=datetime(2026, 10, 12, 10, tzinfo=UTC))
    assert period.start == datetime(2026, 10, 6, tzinfo=UTC)
    assert period.end == datetime(2026, 10, 13, tzinfo=UTC)
    settings = Settings(database_url=SecretStr("postgresql+psycopg://localhost/example"))
    assert any(
        button.callback_data == "section:audience"
        for row in menu(settings, 1).inline_keyboard
        for button in row
    )
    with pytest.raises(ValidationError):
        Settings(database_url=settings.database_url, admin_path="/cabinet")


@pytest.mark.integration
def test_cabinet_and_owner_login_are_independent(cabinet_fixture):
    engine, config, actors, _, _ = cabinet_fixture
    config = config.model_copy(update={"admin_enabled": True, "owner_ids": frozenset({actors[0]})})
    client = create_app(config, engine).test_client()
    assert login(client, ticket_for(engine, config, actors[0])).status_code == 303
    response = client.get("/owner/")
    assert response.status_code == 302 and response.location.endswith("/owner/login")
    assert client.get("/owner/login").status_code == 200
    assert client.get("/cabinet/").status_code == 200
    with client.session_transaction() as owner_session:
        owner_session["owner_id"] = actors[
            0
        ]  # forged signed owner data is not expert authentication
    client.delete_cookie(COOKIE, path="/cabinet")
    assert client.get("/cabinet/").status_code == 302


@pytest.mark.integration
def test_cabinet_production_https_cookie_and_host(cabinet_fixture):
    engine, config, actors, _, _ = cabinet_fixture
    config = config.model_copy(
        update={"environment": "production", "public_base_url": "https://localhost"}
    )
    client = create_app(config, engine).test_client()
    assert client.get("/cabinet/login").status_code == 403
    assert client.get("/cabinet/login", base_url="https://evil.example").status_code == 400
    page = client.get("/cabinet/login", base_url="https://localhost")
    csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)[1]
    ticket = ticket_for(engine, config, actors[0])
    response = client.post(
        "/cabinet/login", base_url="https://localhost", data={"csrf": csrf, "token": ticket}
    )
    assert response.status_code == 303
    assert client.get_cookie(COOKIE, path="/cabinet").secure
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


@pytest.mark.integration
def test_cabinet_login_rate_limit_and_missing_configuration(cabinet_fixture):
    engine, config, _, _, _ = cabinet_fixture
    client = create_app(config, engine).test_client()
    for index in range(16):
        response = login(client, "x" * 43)
        assert response.status_code == (401 if index < 15 else 429)
    unavailable = config.model_copy(update={"web_secret_key": SecretStr("")})
    assert create_app(unavailable, engine).test_client().get("/cabinet/login").status_code == 503


@pytest.mark.integration
def test_bot_cabinet_button_expert_only_without_real_api(cabinet_fixture):
    engine, config, actors, owners, _ = cabinet_fixture

    async def run():
        bot = Bot(config.bot_token.get_secret_value())
        bot_user = TelegramUser(id=bot.id, is_bot=True, first_name="Synthetic bot")
        dispatcher = Dispatcher(storage=PostgresStorage(engine))
        dispatcher.include_router(build_router(engine, config))
        calls = []

        async def api(_bot, method, timeout=None):
            calls.append(method)
            if isinstance(method, SendMessage) and method.reply_markup:
                for row in method.reply_markup.inline_keyboard:
                    for button in row:
                        if button.url and urlsplit(button.url).hostname == "localhost":
                            raise TelegramBadRequest(method=method, message="Wrong HTTP URL")
            if isinstance(method, GetMe):
                return bot_user
            if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
                return True
            return Message(
                message_id=101,
                date=datetime.now(UTC),
                chat=Chat(id=actors[0], type="private"),
                from_user=bot_user,
            )

        async def feed(actor, number):
            user = TelegramUser(id=actor, is_bot=False, first_name="Synthetic expert")
            callback = CallbackQuery(
                id=str(number),
                chat_instance="test",
                from_user=user,
                data="section:audience",
                message=Message(
                    message_id=number,
                    date=datetime.now(UTC),
                    chat=Chat(id=actor, type="private"),
                    from_user=bot_user,
                ),
            )
            await dispatcher.feed_update(bot, Update(update_id=number, callback_query=callback))

        try:
            with (
                patch.object(bot.session, "make_request", api),
                patch("leedbox.telegram.safety.allow", return_value=True),
            ):
                state = dispatcher.fsm.get_context(bot=bot, chat_id=actors[0], user_id=actors[0])
                await state.update_data(mode="expert")
                await feed(actors[0], 1)
                panels = [call for call in calls if isinstance(call, SendMessage)]
                assert len(panels) == 1 and "Моя аудитория" in panels[0].text
                button = panels[0].reply_markup.inline_keyboard[0][0]
                assert button.url is None and button.copy_text is not None
                url = button.copy_text.text
                assert urlsplit(url).path == "/cabinet/login" and urlsplit(url).fragment.startswith(
                    "token="
                )
                recipient_state = dispatcher.fsm.get_context(
                    bot=bot, chat_id=actors[1], user_id=actors[1]
                )
                await recipient_state.update_data(mode="recipient")
                await feed(actors[1], 2)
                assert isinstance(calls[-1], AnswerCallbackQuery) and calls[-1].show_alert
                with Session(engine) as db:
                    assert (
                        db.scalar(
                            select(func.count())
                            .select_from(CabinetCredential)
                            .where(CabinetCredential.owner_user_id == owners[1])
                        )
                        == 0
                    )
        finally:
            await bot.session.close()

    asyncio.run(run())


@pytest.mark.parametrize("host", ["localhost:5000", "127.0.0.1:5000", "[::1]:5000"])
def test_local_cabinet_uses_copy_not_invalid_telegram_url(host):
    url = f"http://{host}/cabinet/login#token=" + "x" * 43
    button = cabinet_button(url)
    assert button.url is None and button.copy_text.text == url


def test_public_cabinet_remains_clickable():
    url = "https://example.com/cabinet/login#token=" + "x" * 43
    button = cabinet_button(url)
    assert button.url == url and button.copy_text is None
