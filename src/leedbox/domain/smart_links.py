"""Verified app identifiers and conservative, user-activated launch URLs.

Chrome intents: https://developer.chrome.com/docs/android/intents
Telegram URIs: https://core.telegram.org/api/links
Android packages are the applications' official Google Play identifiers.
No destination is fetched; unknown destinations use ordinary universal HTTP links.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from leedbox.domain.links import website_url


@dataclass(frozen=True)
class AppTarget:
    name: str
    destination: str
    native: str | None
    android: str | None

    @property
    def host(self) -> str:
        return urlsplit(self.destination).netloc


APPS = (
    ("Instagram", "instagram.com", "com.instagram.android"),
    ("Ozon", "ozon.ru", "ru.ozon.app.android"),
    ("Wildberries", "wildberries.ru", "com.wildberries.ru"),
)


def telegram_uri(destination: str) -> str | None:
    parts = urlsplit(destination)
    if parts.scheme != "https" or parts.port not in {None, 443}:
        return None
    path = parts.path.strip("/")
    invite = re.fullmatch(r"(?:\+|joinchat/)([A-Za-z0-9_-]+)", path)
    if invite:
        return "tg://join?" + urlencode({"invite": invite[1]})
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]{3,31})(?:/([0-9]+))?", path)
    if not match or match[1].lower() in {
        "share",
        "login",
        "proxy",
        "socks",
        "joinchat",
        "addstickers",
        "addemoji",
        "boost",
    }:
        return None
    tags = dict(parse_qsl(parts.query, keep_blank_values=True))
    # Unhandled Mini Apps / service actions use the exact HTTPS link instead of guessing.
    if set(tags) - {"start", "text", "profile", "single", "thread", "comment", "t"}:
        return None
    if "start" in tags and not re.fullmatch(r"[A-Za-z0-9_-]{0,64}", tags["start"]):
        return None
    args = {"domain": match[1], **tags}
    if match[2]:
        args["post"] = match[2]
    return "tg://resolve?" + urlencode(args)


def app_target(value: str) -> AppTarget:
    destination = website_url(value)
    if len(destination.encode()) > 4096:
        raise ValueError("Ссылка должна быть не длиннее 4096 байт")
    parts = urlsplit(destination)
    host = (parts.hostname or "").lower().rstrip(".")
    if host in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        return AppTarget("Telegram", destination, telegram_uri(destination), None)
    for name, domain, package in APPS:
        if host == domain or host == "www." + domain:
            if parts.port not in {None, 443} or parts.scheme != "https":
                return AppTarget(name, destination, None, None)
            # Encode semicolons and fragments so input cannot inject Intent directives.
            path = quote(parts.path, safe="/%-._~")
            query = "?" + quote(parts.query, safe="=&%/-._~") if parts.query else ""
            android = (
                f"intent://{parts.netloc}{path}{query}#Intent;scheme=https;package={package};"
                f"S.browser_fallback_url={quote(destination, safe='')};end"
            )
            return AppTarget(name, destination, None, android)
    return AppTarget("Сайт / другое приложение", destination, None, None)
