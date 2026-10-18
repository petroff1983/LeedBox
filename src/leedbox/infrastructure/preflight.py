"""Read-only deployment checks. Does not call Telegram or start polling."""

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine


@dataclass(frozen=True)
class Check:
    code: str
    passed: bool
    instruction: str


def configuration_checks(settings: Settings, *, production: bool) -> list[Check]:
    token = settings.bot_token.get_secret_value()
    checks = [
        Check(
            "bot_token",
            bool(re.fullmatch(r"[1-9][0-9]{0,18}:[A-Za-z0-9_-]{20,}", token))
            and "TEST-TOKEN-ONLY" not in token
            and "replace" not in token.lower(),
            "Set the new bot BOT_TOKEN outside the repository; no API call is made.",
        )
    ]
    if production:
        url = make_url(settings.database_url.get_secret_value())
        checks.extend(
            [
                Check(
                    "production_environment",
                    settings.environment == "production",
                    "Use a separate production configuration.",
                ),
                Check(
                    "production_database",
                    not str(url.database).endswith(("_test", "_development")),
                    "Use a separate production database, not the local test/development database.",
                ),
                Check(
                    "owner_admin",
                    settings.admin_enabled and bool(settings.owner_ids),
                    "Enable owner admin with a non-empty Telegram ID allowlist.",
                ),
                Check(
                    "https_base",
                    settings.public_base_url is not None
                    and settings.public_base_url.startswith("https://"),
                    "Configure the HTTPS base URL for the owner panel and redirects.",
                ),
                Check(
                    "loopback_web",
                    settings.web_host in {"127.0.0.1", "::1", "localhost"},
                    "Bind Waitress to loopback behind the HTTPS reverse proxy.",
                ),
            ]
        )
    return checks


def database_checks(engine: Engine, project_root: Path) -> list[Check]:
    try:
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "migrations"))
        expected = set(ScriptDirectory.from_config(config).get_heads())
    except Exception:
        return [Check("migration_files", False, "Run from the complete project checkout.")]
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            actual = set(MigrationContext.configure(connection).get_current_heads())
    except Exception:
        # Drivers may include passwords, hosts or parameters in exception text.
        return [
            Check("database", False, "Check PostgreSQL connectivity and credentials privately.")
        ]
    return [
        Check("database", True, "PostgreSQL read-only connection succeeded."),
        Check(
            "schema_head",
            bool(expected) and actual == expected,
            "Back up the database and apply reviewed migrations before starting the new release.",
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        settings = Settings()
    except (ValidationError, ValueError):
        checks = [Check("configuration", False, "Correct private environment configuration.")]
    else:
        checks = configuration_checks(settings, production=args.production)
        engine = create_database_engine(settings)
        try:
            checks.extend(database_checks(engine, args.project_root))
        finally:
            engine.dispose()
    passed = all(check.passed for check in checks)
    print(
        json.dumps(
            {
                "status": "passed" if passed else "failed",
                "checks": [asdict(check) for check in checks],
                "scope": "Configuration and schema only; NOT legal, Telegram or launch acceptance.",
            }
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
