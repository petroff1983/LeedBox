"""App launches are user-activated; original destinations are never fetched."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import test_links_postgres
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leedbox.application.links import create_smart_link, get_link
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.smart_links import app_target
from leedbox.infrastructure.models import LinkEvent, TrackedLink
from leedbox.web.app import create_app

link_fixture = test_links_postgres.link_fixture


@pytest.mark.parametrize(
    "address,name,package",
    [
        ("www.instagram.com/example/", "Instagram", "com.instagram.android"),
        ("ozon.ru/product/123/?from=ad#reviews", "Ozon", "ru.ozon.app.android"),
        ("www.wildberries.ru/catalog/123/detail.aspx", "Wildberries", "com.wildberries.ru"),
    ],
)
def test_verified_android_app_links_keep_web_fallback(address, name, package):
    target = app_target(address)
    assert target.name == name
    assert f"package={package};" in target.android
    assert "S.browser_fallback_url=https%3A%2F%2F" in target.android
    assert target.destination == "https://" + address


@pytest.mark.parametrize(
    "address,scheme,params",
    [
        (
            "t.me/expert_bot?start=source_1",
            "resolve",
            {"domain": ["expert_bot"], "start": ["source_1"]},
        ),
        ("t.me/example/42", "resolve", {"domain": ["example"], "post": ["42"]}),
        ("t.me/+aB_12-x", "join", {"invite": ["aB_12-x"]}),
        ("t.me/joinchat/aB_12-x", "join", {"invite": ["aB_12-x"]}),
    ],
)
def test_documented_telegram_launch(address, scheme, params):
    target = app_target(address)
    parts = urlsplit(target.native)
    assert parts.scheme == "tg" and parts.netloc == scheme
    assert parse_qs(parts.query) == params


@pytest.mark.parametrize(
    "address",
    [
        "https://instagram.com.evil.example/a",
        "https://shop.ozon.ru/a",
        "http://ozon.ru/a",
        "https://ozon.ru:444/a",
        "https://example.com/a",
        "https://t.me/c/123/45",
        "https://t.me/addstickers/Pack",
        "https://t.me/expert_bot?startapp=test",
        "https://t.me:444/example",
    ],
)
def test_unhandled_apps_and_origins_use_exact_web_address(address):
    target = app_target(address)
    assert target.native is None and target.android is None
    assert target.destination == address


def test_intent_input_cannot_inject_package_or_directives():
    target = app_target("https://ozon.ru/path;package=evil?q=1;end#Intent;package=evil")
    assert target.android.count(";package=") == 1
    assert ";package=ru.ozon.app.android;" in target.android
    assert "path%3Bpackage" in target.android


@pytest.mark.parametrize(
    "address", ["javascript:alert(1)", "ftp://ozon.ru/a", "https://x:y@ozon.ru/a"]
)
def test_unsafe_destinations_are_rejected(address):
    with pytest.raises(ValueError):
        app_target(address)


@pytest.mark.integration
def test_short_and_deeplink_ownership_idempotence_redirect_and_launch_page(link_fixture):
    engine, config, _, owners, projects = link_fixture
    client = create_app(config, engine).test_client()
    with Session(engine) as session, session.begin():
        short = create_smart_link(
            session,
            owners[0],
            projects[0],
            123456,
            "example.com/a?x=1#top",
            "short",
            f"short:{owners[0]}",
        )
        deep = create_smart_link(
            session,
            owners[0],
            projects[0],
            123456,
            "ozon.ru/product/123/?x=1",
            "deeplink",
            f"deep:{owners[0]}",
        )
        generic = create_smart_link(
            session,
            owners[0],
            projects[0],
            123456,
            "example.com/?x=%22%3E",
            "deeplink",
            f"generic:{owners[0]}",
        )
        assert (
            create_smart_link(
                session,
                owners[0],
                projects[0],
                123456,
                "example.com",
                "short",
                f"short:{owners[0]}",
            ).id
            == short.id
        )
        for foreign in owners[1:]:
            with pytest.raises(ObjectUnavailable):
                get_link(session, foreign, 123456, short.id)
            with pytest.raises(ObjectUnavailable):
                create_smart_link(
                    session, foreign, projects[0], 123456, "example.com", "short", f"bad:{foreign}"
                )
        with pytest.raises(ObjectUnavailable):
            get_link(session, owners[0], 789, deep.id)
        ids, codes = [short.id, deep.id, generic.id], [short.code, deep.code, generic.code]
    assert client.head("/go/" + codes[0]).status_code == 302
    assert client.head("/d/" + codes[1]).status_code == 200
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(LinkEvent).where(LinkEvent.link_id.in_(ids))
            )
            == 0
        )
    response = client.get("/go/" + codes[0])
    assert response.status_code == 302
    assert response.location == "https://example.com/a?x=1#top"
    response = client.get("/d/" + codes[1])
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "data-android=" in html and "Открыть в приложении" in html
    assert "Открыть версию сайта" in html and "ozon.ru" in html
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    generic_html = client.get("/d/" + codes[2]).get_data(as_text=True)
    assert "Открыть в приложении" not in generic_html
    assert "Открыть страницу" in generic_html
    assert client.get("/d/" + codes[0]).status_code == 404
    assert client.get("/d/not-a-code").status_code == 404
    with Session(engine) as session, session.begin():
        for link_id in ids:
            assert (
                session.scalar(
                    select(func.count()).select_from(LinkEvent).where(LinkEvent.link_id == link_id)
                )
                == 1
            )
        session.get(TrackedLink, ids[0]).enabled = False
        session.get(TrackedLink, ids[1]).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.get(TrackedLink, ids[2]).status = "revoked"
    assert client.get("/go/" + codes[0]).status_code == 404
    assert client.get("/d/" + codes[1]).status_code == 404
    assert client.get("/d/" + codes[2]).status_code == 404
