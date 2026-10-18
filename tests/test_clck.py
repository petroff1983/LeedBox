import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest
import test_links_postgres
from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.short_links import create_short_link
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure import clck
from leedbox.infrastructure.clck import ShortenedLink, ShortenerError
from leedbox.infrastructure.models import TrackedLink

link_fixture = test_links_postgres.link_fixture


def response(body):
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(body).encode()
    return opener


def test_api_contract_and_fixed_origin():
    opener = response({"error": 0, "id": 42, "shorturl": "https://clck.su/abcd"})
    with (
        patch.object(clck, "_last_request", 0),
        patch.object(clck, "build_opener", return_value=opener),
    ):
        assert clck.shorten("example.com/page?x=1#top", "private-key") == ShortenedLink(
            "https://clck.su/abcd", "42"
        )
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://clck.su/api/url/add"
    assert request.get_header("Authorization") == "Bearer private-key"
    assert json.loads(request.data) == {
        "url": "https://example.com/page?x=1#top",
        "type": "direct",
        "status": "private",
    }
    assert opener.open.call_args.kwargs["timeout"] == 15
    assert clck.NoRedirect().redirect_request() is None


@pytest.mark.parametrize(
    "url", ["https://evil.example/a", "http://clck.su/a", "https://clck.su@evil.example/a"]
)
def test_untrusted_provider_url_is_rejected(url):
    with (
        patch.object(clck, "_last_request", 0),
        patch.object(
            clck, "build_opener", return_value=response({"error": 0, "id": 1, "shorturl": url})
        ),
        pytest.raises(ShortenerError) as error,
    ):
        clck.shorten("example.com", "secret")
    assert error.value.uncertain
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("status,uncertain", [(401, False), (429, False), (503, True), (302, True)])
def test_http_errors_are_safe_and_not_retried(status, uncertain):
    opener = MagicMock()
    opener.open.side_effect = HTTPError("url", status, "secret raw error", {}, None)
    with (
        patch.object(clck, "_last_request", 0),
        patch.object(clck, "build_opener", return_value=opener),
        pytest.raises(ShortenerError) as error,
    ):
        clck.shorten("example.com", "secret")
    assert error.value.uncertain is uncertain
    assert "secret" not in str(error.value)
    assert opener.open.call_count == 1


def test_network_unknown_missing_key_and_rate_limit():
    with pytest.raises(ShortenerError):
        clck.shorten("example.com", "")
    opener = MagicMock()
    opener.open.side_effect = URLError("secret")
    with (
        patch.object(clck, "_last_request", 0),
        patch.object(clck, "build_opener", return_value=opener),
    ):
        with pytest.raises(ShortenerError) as error:
            clck.shorten("example.com", "secret")
        assert error.value.uncertain
        with pytest.raises(ShortenerError) as limited:
            clck.shorten("example.com", "secret")
        assert not limited.value.uncertain
        assert opener.open.call_count == 1


@pytest.mark.integration
def test_external_results_idempotence_ownership_and_uncertainty(link_fixture):
    engine, _, _, owners, projects = link_fixture
    args = (engine, owners[0], projects[0], 123456, "example.com", "external:ok", "secret")
    with patch(
        "leedbox.application.short_links.shorten",
        return_value=ShortenedLink("https://clck.su/ok", "1"),
    ) as api:
        assert create_short_link(*args) == "https://clck.su/ok"
        assert create_short_link(*args) == "https://clck.su/ok"
        assert api.call_count == 1
        with pytest.raises(ObjectUnavailable):
            create_short_link(
                engine, owners[1], projects[0], 123456, "example.com", "external:ok", "secret"
            )
        assert api.call_count == 1
    failed = (*args[:5], "external:unknown", "secret")
    with patch(
        "leedbox.application.short_links.shorten",
        side_effect=ShortenerError("unknown", uncertain=True),
    ) as api:
        with pytest.raises(ShortenerError):
            create_short_link(*failed)
        with pytest.raises(ShortenerError):
            create_short_link(*failed)
        assert api.call_count == 1
    with Session(engine) as db:
        link = db.scalar(select(TrackedLink).where(TrackedLink.action_key == "external:unknown"))
        assert link.status == "uncertain" and link.external_url is None
