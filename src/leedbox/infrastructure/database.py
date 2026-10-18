from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase

from leedbox.infrastructure.config import Settings


class Base(DeclarativeBase):
    pass


def create_database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 3},
    )
