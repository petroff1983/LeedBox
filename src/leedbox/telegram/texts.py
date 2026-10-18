"""Base UI texts and versioned database overrides shared by Telegram handlers."""

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application.ui_texts import read_text


def ui_text(engine: Engine, key: str) -> str:
    with Session(engine) as session:
        return read_text(session, key)
