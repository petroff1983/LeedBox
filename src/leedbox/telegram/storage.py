import asyncio
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import Engine, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.infrastructure.models import WizardDraft


class PostgresStorage(BaseStorage):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def identity(key: StorageKey) -> str:
        return ":".join(
            str(value)
            for value in (
                key.bot_id,
                key.chat_id,
                key.user_id,
                key.thread_id,
                key.business_connection_id,
                key.destiny,
            )
        )

    def _write(self, key: StorageKey, values: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            statement = insert(WizardDraft).values(key=self.identity(key), **values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[WizardDraft.key], set_={**values, "updated_at": func.now()}
                )
            )

    def _read(self, key: StorageKey) -> tuple[str | None, dict[str, Any]]:
        with Session(self.engine) as session:
            row = session.get(WizardDraft, self.identity(key))
            return (row.state, dict(row.data)) if row else (None, {})

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        await asyncio.to_thread(
            self._write, key, {"state": state.state if isinstance(state, State) else state}
        )

    async def get_state(self, key: StorageKey) -> str | None:
        return (await asyncio.to_thread(self._read, key))[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await asyncio.to_thread(self._write, key, {"data": dict(data)})

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return (await asyncio.to_thread(self._read, key))[1]

    async def close(self) -> None:
        # Engine belongs to the runtime and is disposed there.
        pass
