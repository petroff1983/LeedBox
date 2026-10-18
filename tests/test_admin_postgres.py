from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_admin import TEST_SECRET, TEST_TOKEN
from test_services_postgres import (
    service_fixture,  # noqa: F401
    submit,
)
from werkzeug.datastructures import MultiDict

from leedbox.application.admin_credentials import bootstrap
from leedbox.application.entitlements import access_for, policy, policy_data
from leedbox.application.services import catalog
from leedbox.application.settings import setting
from leedbox.application.versions import fingerprint
from leedbox.infrastructure.models import (
    AccessGrant,
    AccessPolicy,
    AdminAccount,
    AdminSession,
    AuditLog,
    ContentVersion,
    Partner,
    RateBucket,
    Service,
    Setting,
)
from leedbox.telegram.texts import ui_text
from leedbox.web.app import create_app
from leedbox.web.auth import token_hash

TEST_PASSWORD = "Synthetic admin password only 123!"


def password_login(client, engine, config, actor):
    with Session(engine) as db, db.begin():
        db.execute(delete(AdminAccount))
        db.execute(delete(RateBucket).where(RateBucket.key.startswith("admin:")))
        bootstrap(db, config, actor, "testadmin", TEST_PASSWORD, must_change=False)
    assert client.get("/owner/login").status_code == 200
    with client.session_transaction(path="/owner/") as browser:
        csrf = browser["csrf"]
    data = {"csrf_token": csrf, "username": "testadmin", "password": TEST_PASSWORD}
    assert client.post("/owner/login", data=data).status_code == 302
    return data


@pytest.fixture
def admin_fixture(request):
    service_context = request.getfixturevalue("service_fixture")
    engine, config, actors, owners, projects, ids, fields = service_context
    config = config.model_copy(
        update={
            "admin_enabled": True,
            "bot_token": SecretStr(TEST_TOKEN),
            "web_secret_key": SecretStr(TEST_SECRET),
        }
    )
    with Session(engine) as db:
        saved_settings = {row.key: row.value for row in db.scalars(select(Setting))}
        existing_policy = db.get(AccessPolicy, 1)
        saved_policy = (
            {col.name: getattr(existing_policy, col.name) for col in AccessPolicy.__table__.columns}
            if existing_policy
            else None
        )
        partner_ids = list(db.scalars(select(Partner.id)))
    app = create_app(config, engine)
    app.testing = True
    client = app.test_client()
    assert client.get("/owner/").status_code == 302
    data = password_login(client, engine, config, actors[0])
    with client.session_transaction(path="/owner/") as browser:
        csrf = browser["csrf"]
    try:
        yield client, csrf, config, service_context, data
    finally:
        with Session(engine) as db, db.begin():
            db.execute(delete(Setting))
            db.add_all(Setting(key=key, value=value) for key, value in saved_settings.items())
            db.execute(delete(AccessPolicy))
            if saved_policy:
                db.add(AccessPolicy(**saved_policy))
            db.execute(delete(Partner).where(Partner.id.not_in(partner_ids)))
            db.execute(delete(AdminSession).where(AdminSession.telegram_id.in_(actors)))


@pytest.mark.integration
def test_admin_login_replay_allowlist_logout_and_headers(admin_fixture):
    client, csrf, config, fixture, data = admin_fixture
    engine, _, actors, *_ = fixture
    response = client.get("/owner/")
    assert response.status_code == 200 and "Панель владельца" in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert client.post("/owner/logout").status_code == 403
    assert client.post("/owner/logout", data={"csrf_token": csrf}).status_code == 302
    assert client.get("/owner/").status_code == 302
    assert client.get("/owner/callback").status_code == 410
    client.get("/owner/login")
    with client.session_transaction(path="/owner/") as browser:
        csrf = browser["csrf"]
    assert client.post("/owner/login", data=data).status_code == 403
    assert (
        client.post(
            "/owner/login", data={**data, "csrf_token": csrf, "password": "bad"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/owner/login",
            data=MultiDict(list({**data, "csrf_token": csrf}.items()) + [("username", "other")]),
        ).status_code
        == 403
    )
    assert client.get("/health").status_code == 200
    with Session(engine) as db:
        assert db.scalar(
            select(AuditLog).where(
                AuditLog.actor_telegram_id == actors[0], AuditLog.action == "admin_logged_in"
            )
        )


@pytest.mark.integration
def test_admin_csrf_conflicts_live_text_and_rollback(admin_fixture):
    client, csrf, _, fixture, _ = admin_fixture
    engine = fixture[0]
    with Session(engine) as db:
        previous = ui_text(engine, "expert_start")
    url = "/owner/texts/edit?key=expert_start"
    data = {
        "value": "Тестовый новый текст <script>alert(1)</script>",
        "expected": fingerprint(previous),
        "confirmed": "1",
        "action_key": "b" * 32,
    }
    assert client.post(url, data=data).status_code == 403
    data["csrf_token"] = csrf
    preview = client.post(url, data={**data, "action": "preview"})
    assert preview.status_code == 200 and "&lt;script&gt;" in preview.text
    assert ui_text(engine, "expert_start") == previous
    assert client.post(url, data=data).status_code == 302
    assert ui_text(engine, "expert_start") == data["value"]
    stale = client.post(url, data={**data, "value": "Не должно сохраниться"})
    assert stale.status_code == 200 and "Текст изменился" in stale.text
    assert ui_text(engine, "expert_start") == data["value"]
    with Session(engine) as db:
        version = db.scalar(
            select(ContentVersion)
            .where(
                ContentVersion.entity == "ui",
                ContentVersion.object_key == "expert_start",
                ContentVersion.actor_telegram_id.is_(None),
            )
            .order_by(ContentVersion.id.desc())
        )
        id = version.id
    response = client.post(
        f"/owner/history/restore?id={id}",
        data={"csrf_token": csrf, "confirmed": "1", "expected": fingerprint(data["value"])},
    )
    assert response.status_code == 302
    assert ui_text(engine, "expert_start") == previous
    # Current server session expiry is checked, not just the signed browser cookie.
    with client.session_transaction(path="/owner/") as browser:
        sid = browser["sid"]
    with Session(engine) as db, db.begin():
        db.get(AdminSession, token_hash(sid)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get(url).status_code == 302


@pytest.mark.integration
def test_admin_catalog_settings_and_partner_gate(admin_fixture):
    client, csrf, _, fixture, _ = admin_fixture
    engine, _, _, _, _, ids, fields = fixture
    with Session(engine) as db:
        original = asdict(fields)
        original["title"] = db.get(Service, ids[0]).title
    data = {key: "1" if value is True else str(value) for key, value in original.items()}
    data.update(csrf_token=csrf, confirmed="1", expected="1", title="Услуга после админки")
    url = f"/owner/services/edit?id={ids[0]}"
    assert client.post(url, data=data).status_code == 302
    with Session(engine) as db:
        assert any(row.title == data["title"] for row in catalog(db))
    assert "Услуга изменилась" in client.post(url, data=data).text
    partner = {
        "name": "Тестовый партнёр",
        "description": "Тест",
        "url": "https://example.com",
        "enabled": "1",
        "action_key": "b" * 32,
        "expected": "0",
        "csrf_token": csrf,
        "confirmed": "1",
    }
    assert "маркировку" in client.post("/owner/partners/edit", data=partner).text
    partner["exception_reason"] = "Тестовое документированное основание"
    assert client.post("/owner/partners/edit", data=partner).status_code == 302
    assert client.post("/owner/partners/edit", data=partner).status_code == 302
    with Session(engine) as db:
        assert len(list(db.scalars(select(Partner).where(Partner.name == partner["name"])))) == 1
    with Session(engine) as db:
        before = setting(db, "active_material_limit")
    response = client.post(
        "/owner/settings/edit?key=active_material_limit",
        data={"value": "17", "expected": fingerprint(before), "csrf_token": csrf, "confirmed": "1"},
    )
    assert response.status_code == 302
    with Session(engine) as db:
        assert setting(db, "active_material_limit") == 17
    missing = client.post(
        "/owner/settings/edit?key=active_material_limit",
        data={"value": "123", "expected": fingerprint(17), "csrf_token": csrf},
    )
    assert missing.status_code == 200 and 'value="123"' in missing.text
    invalid = client.post(
        "/owner/settings/edit?key=active_material_limit",
        data={
            "value": "invalid",
            "expected": fingerprint(17),
            "csrf_token": csrf,
            "confirmed": "1",
        },
    )
    assert 'value="invalid"' in invalid.text
    assert "По выбранным условиям ничего не найдено" in client.get("/owner/services/?q=absent").text
    for path in (
        "services/",
        "partners/",
        "settings/",
        "texts/",
        "access/",
        "access/users",
        "history/",
        "history/audit",
        "requests/",
    ):
        assert client.get("/owner/" + path).status_code == 200


@pytest.mark.integration
def test_admin_access_policy_and_individual_dates(admin_fixture):
    client, csrf, _, fixture, _ = admin_fixture
    engine, _, _, owners, *_ = fixture
    with Session(engine) as db:
        values = policy_data(policy(db))
    expected = fingerprint(values)
    values.update(mode="paid", price="1234.50", trial_days=60, activation_grace_days=7)
    data = {key: str(value) for key, value in values.items() if key != "hide_for_paid"}
    data.update(csrf_token=csrf, confirmed="1", expected=expected)
    missing = client.post(
        "/owner/access/", data={key: value for key, value in data.items() if key != "confirmed"}
    )
    assert 'value="1234.50"' in missing.text and 'value="60"' in missing.text
    assert client.post("/owner/access/", data=data).status_code == 302
    with Session(engine) as db:
        assert policy(db).trial_days == 60 and str(policy(db).price) == "1234.5000"
        assert access_for(db, owners[1]).allowed
    url = f"/owner/access/user?id={owners[1]}"
    assert client.get(url).status_code == 200
    extend = {
        "csrf_token": csrf,
        "confirmed": "1",
        "action": "extend",
        "action_key": "a" * 32,
        "days": "30",
    }
    assert client.post(url, data=extend).status_code == 200
    assert client.post(url, data=extend).status_code == 200
    with Session(engine) as db:
        grants = list(
            db.scalars(
                select(AccessGrant).where(
                    AccessGrant.user_id == owners[1], AccessGrant.kind == "gift"
                )
            )
        )
        assert len(grants) == 1
        grant_id, end = grants[0].id, grants[0].ends_at
    adjust = {
        "csrf_token": csrf,
        "confirmed": "1",
        "action": "adjust",
        "grant_id": str(grant_id),
        "expected_end": end.isoformat(),
        "end": (end + timedelta(days=2)).isoformat(),
    }
    assert client.post(url, data=adjust).status_code == 200
    assert client.post(url, data=adjust).status_code == 200  # same date is idempotent
    adjust["end"] = (end + timedelta(days=3)).isoformat()
    assert "изменился" in client.post(url, data=adjust).text
    # Restore mode and limits through validated rollback, not SQL mutation.
    with Session(engine) as db:
        version = db.scalar(
            select(ContentVersion)
            .where(
                ContentVersion.entity == "policy",
                ContentVersion.data["mode"].astext == values.get("previous", "free"),
            )
            .order_by(ContentVersion.id.desc())
        )
        current = fingerprint(policy_data(policy(db)))
        version_id = version.id
    assert (
        client.post(
            f"/owner/history/restore?id={version_id}",
            data={"csrf_token": csrf, "confirmed": "1", "expected": current},
        ).status_code
        == 302
    )


@pytest.mark.integration
def test_admin_service_request_status_cycle_and_revoked_contact(admin_fixture):
    client, csrf, _, fixture, _ = admin_fixture
    engine = fixture[0]
    with Session(engine) as db, db.begin():
        row = submit(db, fixture)
        id = row.id
    url = f"/owner/requests/detail?id={id}"
    assert "@test_client" in client.get(url).text
    assert (
        client.post(
            url, data={"csrf_token": csrf, "expected": "1", "target": "working"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            url, data={"csrf_token": csrf, "expected": "1", "target": "working", "confirmed": "1"}
        ).status_code
        == 302
    )
    assert (
        client.post(
            url, data={"csrf_token": csrf, "expected": "1", "target": "closed", "confirmed": "1"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            url, data={"csrf_token": csrf, "expected": "2", "target": "closed", "confirmed": "1"}
        ).status_code
        == 302
    )
    from leedbox.application.services import withdraw_consent

    with Session(engine) as db, db.begin():
        withdraw_consent(db, 123456, id, fixture[3][1])
    text = client.get(url).text
    assert "@test_client" not in text and "Согласие отозвано" in text


# Render-only evidence for the local preview script; no real credentials or shipping bypass route.
RENDER_SNAPSHOTS = {}
RENDER_ASSETS = {}


@pytest.mark.integration
def test_admin_render_snapshots(admin_fixture):
    client, csrf, _, fixture, _ = admin_fixture
    pages = {
        "home": "/owner/",
        "catalog": "/owner/services/",
        "service": f"/owner/services/edit?id={fixture[5][0]}",
        "access": "/owner/access/",
        "empty": "/owner/services/?q=absent",
        "expert": f"/owner/access/user?id={fixture[3][1]}",
        "payment-price": "/owner/settings/edit?key=stars_access_price",
        "payment-terms": "/owner/settings/edit?key=payment_terms",
    }
    for name, path in pages.items():
        response = client.get(path)
        assert response.status_code == 200
        RENDER_SNAPSHOTS[name] = response.get_data()
    error = client.post(
        "/owner/texts/edit?key=expert_start",
        data={
            "csrf_token": csrf,
            "value": "Текст не сохранится",
            "expected": "stale",
            "confirmed": "1",
        },
    )
    RENDER_SNAPSHOTS["error"] = error.get_data()
    # Copy only referenced packaged static responses while the test session is active.
    import re

    for body in RENDER_SNAPSHOTS.values():
        for path in re.findall(rb'(?:src|href)="(/(?:owner/)?static/[^"?]+)', body):
            url = path.decode()
            response = client.get(url)
            assert response.status_code == 200
            RENDER_ASSETS[url] = (response.get_data(), response.content_type)
