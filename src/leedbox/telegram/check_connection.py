"""Read-only Bot API check; never prints credentials."""

import argparse
import asyncio

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from leedbox.infrastructure.config import Settings


async def check(get_me_only: bool = False, system_proxy: bool = False) -> None:
    settings = Settings()
    if not settings.bot_token.get_secret_value():
        raise SystemExit("Test bot token is not configured")
    if system_proxy:
        # Diagnostic only: honor the environment/system proxy just like curl.
        # Never change the bot runtime, OS proxy, DNS or TLS verification.
        async with (
            aiohttp.ClientSession(
                trust_env=True, timeout=aiohttp.ClientTimeout(total=15)
            ) as client,
            client.get(
                "https://api.telegram.org/bot" + settings.bot_token.get_secret_value() + "/getMe"
            ) as response,
        ):
            if response.status != 200:
                raise SystemExit(f"getMe HTTP error: {response.status}")
            payload = await response.json()
            if not payload.get("ok"):
                raise SystemExit("getMe: Telegram rejected the request")
            me = payload["result"]
            print(f"getMe OK (system proxy): @{me.get('username')}; id={me['id']}")
            return
    async with Bot(settings.bot_token.get_secret_value()) as bot:
        me = await bot.get_me(request_timeout=10)
        if get_me_only:
            print(f"getMe OK: @{me.username}; id={me.id}")
            return
        webhook = await bot.get_webhook_info(request_timeout=10)
        print(f"Bot: @{me.username}; id={me.id}; webhook_enabled={bool(webhook.url)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read-only Telegram API check; never starts polling"
    )
    parser.add_argument("--get-me-only", action="store_true")
    parser.add_argument(
        "--system-proxy",
        action="store_true",
        help="Diagnostic getMe only using system/environment proxy",
    )
    args = parser.parse_args()
    try:
        asyncio.run(check(args.get_me_only, args.system_proxy))
    except TelegramNetworkError as error:
        token = Settings().bot_token.get_secret_value()
        safe_message = error.message.replace(token, "[redacted]") if token else error.message
        raise SystemExit(f"TelegramNetworkError: {safe_message}") from None
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram rejected the configured token") from None
    except (aiohttp.ClientError, TimeoutError) as error:
        token = Settings().bot_token.get_secret_value()
        safe_message = str(error).replace(token, "[redacted]") if token else str(error)
        raise SystemExit(f"{type(error).__name__}: {safe_message or 'Request timeout'}") from None
