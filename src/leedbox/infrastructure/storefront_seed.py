"""Explicit local installation of owner-approved storefront content."""

from sqlalchemy.orm import Session

from leedbox.application.storefront import install
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine


def main() -> None:
    config = Settings()
    if config.environment != "development" or len(config.owner_ids) != 1:
        raise SystemExit("Requires development environment and exactly one configured owner")
    engine = create_database_engine(config)
    try:
        with Session(engine) as db, db.begin():
            install(db, config, next(iter(config.owner_ids)))
        print("Storefront installed; product links remain unpublished drafts pending marking.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
