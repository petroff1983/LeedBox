from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from leedbox.application.partners import PartnerInput
from leedbox.infrastructure.config import Settings
from leedbox.web.app import create_app

# Synthetic credentials only; no environment token is read or printed.
TEST_TOKEN = "123456:synthetic_test_token_without_real_bot"
TEST_SECRET = "synthetic-session-secret-0123456789-abcdef"


def admin_settings(**extra):
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/leedbox_test",
        **{
            "environment": "test",
            "admin_enabled": True,
            "owner_ids": [123],
            "bot_token": TEST_TOKEN,
            "web_secret_key": TEST_SECRET,
            **extra,
        },
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"owner_ids": []},
        {"web_secret_key": "replace-me"},
        {"admin_path": "/health"},
        {"environment": "production", "public_base_url": "http://localhost"},
    ],
)
def test_admin_configuration_fails_closed(extra):
    with pytest.raises(ValidationError):
        admin_settings(**extra)


def test_disabled_admin_and_health_without_auth():
    config = admin_settings(admin_enabled=False, owner_ids=[])
    client = create_app(config, MagicMock()).test_client()
    assert client.get("/owner/").status_code == 404
    assert client.get("/health").status_code == 200


def test_partner_publication_gate():
    fields = PartnerInput("Тест", "Тестовое описание", "https://example.com")
    assert not fields.validated()["enabled"]
    with pytest.raises(ValueError, match="маркировку"):
        replace(fields, enabled=True).validated()
    assert replace(
        fields,
        enabled=True,
        advertiser="Тест",
        inn="1234567890",
        erid="test_1",
        marking_text="Реклама",
    ).validated()["enabled"]
    assert replace(
        fields, enabled=True, exception_reason="Документированное тестовое основание"
    ).validated()["enabled"]
    for values in (
        replace(fields, url="javascript:alert(1)"),
        replace(fields, inn="bad"),
        replace(fields, erid="bad token"),
    ):
        with pytest.raises(ValueError):
            values.validated()
