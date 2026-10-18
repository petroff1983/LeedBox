"""Explicit local transport using existing proxy configuration, with TLS verification."""

import asyncio

from aiogram.client.session.aiohttp import AiohttpSession
from aiohttp import ClientSession


class SystemProxySession(AiohttpSession):
    """Reuse aiogram's API/form/file handling with an owned proxy-aware client."""

    client: ClientSession | None = None

    async def create_session(self) -> ClientSession:
        if self.client is None or self.client.closed:
            self.client = ClientSession(trust_env=True)
        return self.client

    async def close(self) -> None:
        if self.client is not None and not self.client.closed:
            await self.client.close()
            await asyncio.sleep(0.25)
