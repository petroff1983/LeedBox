"""Local, explicit schema/credit rollout; no production targets or paid AI calls."""

import argparse
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from leedbox.application import credits, provider_balance, provider_limits
from leedbox.application.settings import set_setting
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import AIAccount, Setting


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.development")
    parser.add_argument("--apply-schema", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--refresh-providers", action="store_true")
    parser.add_argument("--processes", action="store_true")
    parser.add_argument("--sandbox-schema", action="store_true")
    args = parser.parse_args()
    os.environ.update(
        {k: v for k, v in dotenv_values(args.env_file, interpolate=False).items() if v is not None}
    )
    config = Settings()
    url = make_url(config.database_url.get_secret_value())
    if (
        config.environment != "development"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.port != 5433
        or url.database != "leedbox_development"
        or url.username != "leedbox"
    ):
        raise SystemExit(
            "Only the local development database leedbox_development on port 5433 is allowed"
        )
    if args.processes:
        script = r"""Get-CimInstance Win32_Process | Where-Object {
            $_.Name -eq 'python.exe' -and $_.CommandLine -match (
                'leedbox-bot|leedbox-web|leedbox\.telegram\.runtime|' +
                'leedbox\.web\.runtime|run-telegram-payment-sandbox.py')
        } | ForEach-Object {
            $kind = if ($_.CommandLine -match 'leedbox-web|leedbox\.web\.runtime') {
                'web'
            } elseif ($_.CommandLine -match 'run-telegram-payment-sandbox.py') {
                'bot-with-sandbox'
            } else {'bot'}
            [pscustomobject]@{Pid=$_.ProcessId;Parent=$_.ParentProcessId;Role=$kind}
        } | ConvertTo-Json -Compress"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        print(result.stdout)
    engine = create_database_engine(config)
    bot = int(config.bot_token.get_secret_value().split(":")[0])
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        pending = dict(
            connection.execute(
                text(
                    "SELECT status, count(*) FROM ai_operations WHERE owner_user_id IS NOT NULL "
                    "AND status IN ('reserved','sent','uncertain') GROUP BY status"
                )
            ).all()
        )
    print(json.dumps({"revision": revision, "pending": pending, "environment": config.environment}))
    if args.apply_schema:
        root = Path(__file__).resolve().parents[1]
        target = root / ".runtime" / "backups"
        target.mkdir(parents=True, exist_ok=True)
        name = "before-credits-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".dump"
        container_file = "/tmp/" + name
        for cmd in (
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "-U",
                "leedbox",
                "-d",
                "leedbox_development",
                "-Fc",
                "-f",
                container_file,
            ],
            ["docker", "compose", "cp", "postgres:" + container_file, str(target / name)],
        ):
            result = subprocess.run(cmd, capture_output=True, check=False)
            if result.returncode:
                raise SystemExit("Backup failed; migration was NOT started")
        print("Backup: " + str(target / name))
        command.upgrade(Config("alembic.ini"), "head")
        print("Schema upgraded")
    if args.sandbox_schema:
        # This old, separately created acceptance DB has no Alembic baseline.
        # Add only the five credit columns; never rebuild it or change its funds.
        spec = importlib.util.spec_from_file_location(
            "credit_payment_sandbox", Path(__file__).with_name("robokassa-sandbox.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sandbox_config, _ = module.config_and_state(False)
        sandbox = create_database_engine(sandbox_config)
        try:
            with sandbox.begin() as connection:
                for statement in (
                    "ALTER TABLE ai_accounts ADD COLUMN IF NOT EXISTS "
                    "credit_balance BIGINT NOT NULL DEFAULT 0",
                    "ALTER TABLE ai_accounts ADD COLUMN IF NOT EXISTS "
                    "credit_reserved BIGINT NOT NULL DEFAULT 0",
                    "ALTER TABLE ai_accounts ADD COLUMN IF NOT EXISTS "
                    "credit_value_rub NUMERIC(20,6) NOT NULL DEFAULT 0",
                    "ALTER TABLE ai_operations ADD COLUMN IF NOT EXISTS "
                    "charged_credits BIGINT NOT NULL DEFAULT 0",
                    "ALTER TABLE ai_operations ADD COLUMN IF NOT EXISTS "
                    "revenue_rub NUMERIC(20,6) NOT NULL DEFAULT 0",
                ):
                    connection.execute(text(statement))
            print("Isolated payment sandbox: additive columns ready; balances unchanged")
        finally:
            sandbox.dispose()
    if args.activate:
        with Session(engine) as db, db.begin():
            if not config.owner_ids:
                raise SystemExit("No owner configured")
            set_setting(db, config, min(config.owner_ids), "ai_credits_enabled", True)
        print("Credit mode activated")
    if args.refresh_providers:
        provider_limits.refresh(engine, config, bot, manual=True)
        provider_balance.check(engine, config, bot)
        with Session(engine) as db:
            for provider in ("zenstat", "apify"):
                row = db.get(Setting, f"provider_limits:{bot}:{provider}")
                print(json.dumps({provider: row.value if row else "unknown"}, ensure_ascii=True))
    if revision == "f492e317ca80" or args.apply_schema:
        with Session(engine) as db:
            print(
                json.dumps(
                    {
                        "credits_enabled": credits.enabled(db),
                        "legacy_tokens": db.scalar(
                            select(func.coalesce(func.sum(AIAccount.balance), 0))
                        ),
                        "credits": db.scalar(
                            select(func.coalesce(func.sum(AIAccount.credit_balance), 0))
                        ),
                    },
                    default=str,
                )
            )
    engine.dispose()


if __name__ == "__main__":
    main()
