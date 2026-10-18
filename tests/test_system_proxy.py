import asyncio

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from pydantic import SecretStr

from leedbox.infrastructure.config import Settings
from leedbox.telegram.runtime import create_bot
from leedbox.telegram.system_proxy import SystemProxySession


def config(**updates):
    return Settings(
        environment=updates.pop("environment", "development"),
        database_url=SecretStr("postgresql+psycopg://fixture/fixture_test"),
        bot_token=SecretStr("123456:TEST-TOKEN-ONLY"),
        **updates,
    )


def test_proxy_is_opt_in_and_local_only():
    assert type(create_bot(config()).session) is AiohttpSession
    assert isinstance(create_bot(config(telegram_system_proxy=True)).session, SystemProxySession)
    with pytest.raises(ValueError, match="local development"):
        create_bot(config(environment="production", telegram_system_proxy=True))


def test_owned_proxy_client_lifecycle_without_network():
    async def run():
        transport = SystemProxySession()
        first = await transport.create_session()
        assert first.trust_env
        assert await transport.create_session() is first
        await transport.close()
        assert first.closed
        second = await transport.create_session()
        assert second is not first and second.trust_env
        await transport.close()
        assert second.closed

    asyncio.run(run())
