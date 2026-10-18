"""Fixed-origin clck.su client. Never follow redirects or expose response bodies."""

import json
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from leedbox.domain.materials import http_url
from leedbox.domain.smart_links import app_target


class ShortenerError(Exception):
    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


@dataclass(frozen=True)
class ShortenedLink:
    url: str
    id: str


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_lock = threading.Lock()
_last_request = 0.0


def shorten(destination: str, api_key: str) -> ShortenedLink:
    global _last_request
    destination = app_target(destination).destination
    if not api_key:
        raise ShortenerError("Сервис коротких ссылок пока не настроен владельцем.")
    with _lock:
        now = time.monotonic()
        if now - _last_request < 2.1:
            raise ShortenerError("Подождите несколько секунд и отправьте ссылку ещё раз.")
        _last_request = now
    request = Request(
        "https://clck.su/api/url/add",
        data=json.dumps({"url": destination, "type": "direct", "status": "private"}).encode(),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        if str(result.get("error")) != "0":
            raise ShortenerError("Сервис отклонил создание ссылки. Проверьте тариф и ключ API.")
        url, provider_id = result.get("shorturl"), str(result.get("id", ""))
        if not isinstance(url, str) or not provider_id or len(provider_id) > 100:
            raise ValueError
        if len(url) > 256 or http_url(url) != url:
            raise ValueError
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "clck.su"
            or not parsed.path.strip("/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        return ShortenedLink(url, provider_id)
    except HTTPError as error:
        if error.code == 429:
            raise ShortenerError("Лимит сервиса исчерпан. Попробуйте через минуту.") from None
        raise ShortenerError(
            "Сервис коротких ссылок недоступен. Проверьте ключ API и тариф.",
            uncertain=error.code >= 500 or 300 <= error.code < 400,
        ) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise ShortenerError(
            "Ответ сервиса не подтверждён. Повторная отправка автоматически не выполняется.",
            uncertain=True,
        ) from None
