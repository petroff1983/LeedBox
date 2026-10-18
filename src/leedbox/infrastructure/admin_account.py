"""Initialize/recover owner credentials at the trusted server console."""

import argparse
import getpass
import os
import secrets
from pathlib import Path

from sqlalchemy.orm import Session

from leedbox.application.admin_credentials import bootstrap, reset
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Initial administrator / server-console recovery")
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--login", default="admin")
    parser.add_argument(
        "--reset", action="store_true", help="Explicit recovery; revoke all sessions"
    )
    parser.add_argument(
        "--generate-local",
        action="store_true",
        help="Development only: one-time secret in ignored .runtime/admin-initial-password.txt",
    )
    args = parser.parse_args()
    config = Settings()
    path: Path | None = None
    if args.generate_local:
        if config.environment != "development":
            raise SystemExit("Generated secret file is allowed only in development")
        path = Path(".runtime/admin-initial-password.txt")
        path.parent.mkdir(exist_ok=True)
        secret = secrets.token_urlsafe(24)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("Initial local administrator password (change at first login):\n" + secret)
    else:
        secret = getpass.getpass("Password (15–128 characters): ")
        if secret != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match")
    engine = create_database_engine(config)
    try:
        with Session(engine) as db, db.begin():
            if args.reset:
                reset(db, config, args.owner_id, args.login, secret)
            else:
                bootstrap(db, config, args.owner_id, args.login, secret)
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        raise SystemExit(
            "Credentials not saved; verify owner ID, schema and account state"
        ) from None
    finally:
        engine.dispose()
    print("Administrator credentials saved. Password change required at next login.")
    if path is not None:
        print("Initial password file: .runtime/admin-initial-password.txt (not printed)")


if __name__ == "__main__":
    main()
