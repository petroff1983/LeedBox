from datetime import UTC, datetime, timedelta

import pytest
import test_admin_postgres
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_admin import admin_settings
from test_admin_postgres import (
    RENDER_ASSETS,
    RENDER_SNAPSHOTS,
    TEST_PASSWORD,
)
from test_services_postgres import service_fixture  # noqa: F401
from werkzeug.security import check_password_hash

from leedbox.application.admin_credentials import bootstrap, reset
from leedbox.infrastructure.models import AdminAccount, AdminSession, AuditLog
from leedbox.web.app import create_app

admin_fixture = test_admin_postgres.admin_fixture


def csrf(client):
    with client.session_transaction(path="/owner/") as browser:
        return browser["csrf"]


def login(client, name="testadmin", password=TEST_PASSWORD):
    client.get("/owner/login")
    return client.post(
        "/owner/login", data={"csrf_token": csrf(client), "username": name, "password": password}
    )


@pytest.mark.integration
def test_password_change_revokes_sessions_and_never_echoes_secrets(admin_fixture):
    client, token, config, fixture, _ = admin_fixture
    engine, _, actors, *_ = fixture
    peer = create_app(config, engine).test_client()
    assert login(peer).status_code == 302
    page = client.get("/owner/security")
    assert page.status_code == 200
    data = dict(
        csrf_token=token,
        revision="1",
        username="newadmin",
        current_password="wrong",
        new_password="A new synthetic password phrase!",
        confirmation="A new synthetic password phrase!",
        confirmed="1",
    )
    wrong = client.post("/owner/security", data=data)
    assert "Текущий пароль неверен" in wrong.text
    assert data["new_password"] not in wrong.text
    assert client.post("/owner/security", data={**data, "csrf_token": "bad"}).status_code == 403
    data["current_password"] = TEST_PASSWORD
    assert (
        "не совпадают"
        in client.post("/owner/security", data={**data, "confirmation": "different"}).text
    )
    assert (
        "15 до 128"
        in client.post(
            "/owner/security", data={**data, "new_password": "short", "confirmation": "short"}
        ).text
    )
    result = client.post("/owner/security", data=data)
    assert result.status_code == 302 and result.location.endswith("login?changed=1")
    assert peer.get("/owner/").status_code == 302
    assert client.get("/owner/").status_code == 302
    assert login(client).status_code == 401
    assert login(client, "newadmin", data["new_password"]).status_code == 302
    with Session(engine) as db:
        account = db.get(AdminAccount, 1)
        assert account.revision == 2 and not account.must_change
        assert account.password_hash.startswith("scrypt:131072:8:1$")
        assert check_password_hash(account.password_hash, data["new_password"])
        audit = list(db.scalars(select(AuditLog).where(AuditLog.actor_telegram_id == actors[0])))
        assert any(row.action == "admin_credentials_changed" for row in audit)
        assert all(data["new_password"] not in str(row.details) for row in audit)


@pytest.mark.integration
def test_password_login_throttling_and_unknown_account_generic_error(admin_fixture):
    client, _, _, _, _ = admin_fixture
    for index in range(9):
        response = login(client, "unknown" if index % 2 else "testadmin", "bad")
        assert response.status_code == 401 and "Неверный логин или пароль" in response.text
    blocked = login(client)
    assert blocked.status_code == 429 and "15 минут" in blocked.text
    assert login(client).status_code == 429


@pytest.mark.integration
def test_initial_password_reset_legacy_session_and_allowlist(admin_fixture):
    client, _, config, fixture, _ = admin_fixture
    engine, _, actors, *_ = fixture
    with Session(engine) as db, db.begin():
        with pytest.raises(ValueError):
            bootstrap(db, config, actors[0], "testadmin", TEST_PASSWORD)
        with pytest.raises(ValueError):
            reset(db, config, actors[1], "testadmin", TEST_PASSWORD)
        reset(db, config, actors[0], "recovered", "Recovered synthetic phrase 123!")
    assert client.get("/owner/").status_code == 302
    assert login(client, "recovered", "Recovered synthetic phrase 123!").location.endswith(
        "/security"
    )
    assert client.get("/owner/").location.endswith("/security")
    with client.session_transaction(path="/owner/") as browser:
        del browser["credential_revision"]
    assert client.get("/owner/").status_code == 302
    denied = create_app(
        config.model_copy(update={"owner_ids": frozenset({actors[1]})}), engine
    ).test_client()
    assert login(denied, "recovered", "Recovered synthetic phrase 123!").status_code == 401
    with Session(engine) as db, db.begin():
        db.execute(delete(AdminSession))
        db.get(AdminAccount, 1).must_change = False
    assert login(client, "recovered", "Recovered synthetic phrase 123!").status_code == 302
    with Session(engine) as db, db.begin():
        for row in db.scalars(select(AdminSession)):
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/owner/").status_code == 302


@pytest.mark.integration
def test_production_password_login_requires_https_and_secure_cookie(admin_fixture):
    _, _, config, fixture, _ = admin_fixture
    production = config.model_copy(
        update={"environment": "production", "public_base_url": "https://example.com"}
    )
    client = create_app(production, fixture[0]).test_client()
    assert client.get("/owner/login", base_url="http://example.com").status_code == 403
    response = client.get("/owner/login", base_url="https://example.com")
    assert response.status_code == 200
    cookie = response.headers["Set-Cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "max-age" in response.headers["Strict-Transport-Security"]
    assert client.get("/owner/callback", base_url="https://example.com").status_code == 410


def test_web_credentials_do_not_require_telegram_token():
    assert admin_settings(bot_token="").admin_enabled


@pytest.mark.integration
def test_password_auth_render_snapshots(admin_fixture):
    client, token, _, _, _ = admin_fixture
    RENDER_SNAPSHOTS["security"] = client.get("/owner/security").get_data()
    client.post("/owner/logout", data={"csrf_token": token})
    RENDER_SNAPSHOTS["password-login"] = client.get("/owner/login").get_data()
    RENDER_SNAPSHOTS["password-error"] = client.post(
        "/owner/login",
        data={"csrf_token": csrf(client), "username": "testadmin", "password": "bad"},
    ).get_data()
    response = client.get("/static/owner.css")
    RENDER_ASSETS["/static/owner.css"] = (response.data, response.content_type)
