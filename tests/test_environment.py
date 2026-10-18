import json
import logging
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import OperationalError

from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.logging import JsonFormatter
from leedbox.web.app import create_app

URL = "postgresql+psycopg://user:private-password@localhost/leedbox_test"


def settings(**kwargs: object) -> Settings:
    return Settings(database_url=SecretStr(URL), **kwargs)


def test_empty_owner_allowlist_fails_closed() -> None:
    assert not settings().is_owner(123)
    assert settings(owner_ids=[123]).is_owner(123)
    assert not settings(owner_ids=[123]).is_owner(456)


def test_configuration_rejects_non_postgres() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite:///local.db")
    assert "private-password" not in repr(settings())


def test_public_redirect_base_requires_https_except_localhost() -> None:
    assert settings(public_base_url="https://example.com/").public_base_url == "https://example.com"
    assert settings(public_base_url="http://127.0.0.1:5000").public_base_url
    for value in (
        "http://example.com",
        "https://example.com/?q=x",
        "https://user:pw@example.com",
        "javascript:x",
    ):
        with pytest.raises(ValidationError):
            settings(public_base_url=value)


def test_readiness_failure_does_not_leak_database_details() -> None:
    engine = MagicMock()
    engine.connect.side_effect = OperationalError(URL, {}, Exception("private-password"))
    client = create_app(settings(), engine).test_client()
    assert client.get("/health").status_code == 200
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json["event_id"]
    assert "private-password" not in response.text
    assert client.get("/missing").status_code == 404


def test_library_log_arguments_are_not_serialized() -> None:
    record = logging.LogRecord("library", logging.ERROR, "", 0, "token=%s", ("secret",), None)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "library_event"
    assert "secret" not in json.dumps(payload)
