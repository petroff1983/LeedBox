import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr

from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.preflight import configuration_checks, database_checks, main

ROOT = Path(__file__).resolve().parents[1]


def config(**kwargs):
    return Settings(
        database_url=SecretStr("postgresql+psycopg://user:private-password@localhost/leedbox_test"),
        bot_token=SecretStr("123456:TEST-TOKEN-ONLY"),
        **kwargs,
    )


def test_preflight_rejects_example_and_nonproduction_configuration():
    checks = configuration_checks(config(), production=True)
    assert all(not item.passed for item in checks if item.code != "loopback_web")
    assert "private-password" not in json.dumps([item.__dict__ for item in checks])
    assert "TEST-TOKEN-ONLY" not in json.dumps([item.__dict__ for item in checks])


def test_preflight_configuration_does_not_call_telegram():
    checks = configuration_checks(config(), production=False)
    assert [(item.code, item.passed) for item in checks] == [("bot_token", False)]


def test_preflight_redacts_driver_errors():
    engine = MagicMock()
    engine.connect.side_effect = RuntimeError("private-password secret-token private-host")
    checks = database_checks(engine, ROOT)
    assert checks[0].code == "database" and not checks[0].passed
    result = json.dumps([item.__dict__ for item in checks])
    for secret in ("private-password", "secret-token", "private-host"):
        assert secret not in result


def test_preflight_requires_exact_migration_heads():
    engine = MagicMock()
    with patch("leedbox.infrastructure.preflight.MigrationContext") as context:
        context.configure.return_value.get_current_heads.return_value = ("old-head",)
        checks = database_checks(engine, ROOT)
        assert checks[0].passed and not checks[1].passed
        context.configure.return_value.get_current_heads.return_value = tuple(
            ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads()
        )
        assert all(item.passed for item in database_checks(engine, ROOT))


def test_preflight_missing_checkout_is_safe():
    engine = MagicMock()
    checks = database_checks(engine, ROOT / "tests")
    assert not checks[0].passed and checks[0].code == "migration_files"
    engine.connect.assert_not_called()


def test_preflight_valid_production_configuration():
    settings = Settings(
        database_url="postgresql+psycopg://user:fixture-password@localhost/leedbox_production",
        bot_token="123456:" + "a" * 30,
        environment="production",
        admin_enabled=True,
        owner_ids=[123456],
        web_secret_key="0123456789abcdefghijklmnopqrstuvwxyzABCD",
        public_base_url="https://example.com",
    )
    assert all(item.passed for item in configuration_checks(settings, production=True))
    settings.web_host = "0.0.0.0"
    assert not configuration_checks(settings, production=True)[-1].passed


def test_preflight_configuration_error_cli_is_safe(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["preflight", "--production"])
    with patch("leedbox.infrastructure.preflight.Settings") as settings:
        settings.side_effect = ValueError("private-password secret-token")
        with pytest.raises(SystemExit) as exit_info:
            main()
    output = capsys.readouterr().out
    assert exit_info.value.code == 1 and json.loads(output)["status"] == "failed"
    assert "private-password" not in output and "secret-token" not in output
