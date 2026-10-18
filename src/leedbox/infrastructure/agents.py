"""Fixed-origin providers. One attempt; no secret/body logging or automatic paid retries."""

import json
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

from leedbox.infrastructure.clck import NoRedirect


class AgentError(Exception):
    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


# Also caught by application boundaries: test adapters and interrupted streams may raise
# these directly. No message/body from these exceptions is shown or logged.
TRANSPORT_ERRORS = (AgentError, HTTPException, OSError)
PROVIDER_ERRORS = (*TRANSPORT_ERRORS, ValueError)


def rejected_request(error: BaseException) -> bool:
    """Known pre-generation rejection, unlike a missing or truncated paid response."""
    return isinstance(error, AgentError) and error.status in {
        400,
        401,
        402,
        403,
        404,
        405,
        413,
        415,
        422,
        429,
    }


def request_json(
    url: str, headers: dict[str, str], body: dict[str, Any] | None = None
) -> dict[str, Any]:
    request = Request(
        url,
        headers={**headers, "Accept": "application/json", "Content-Type": "application/json"},
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=45) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except HTTPError as error:
        raise AgentError(
            f"Сервис отклонил запрос (HTTP {error.code}). Повтор не выполнялся.", error.code
        ) from None
    except (URLError, OSError, HTTPException):
        raise AgentError(
            "Нет подтверждённого ответа сервиса. Автоматического повтора нет."
        ) from None
    except (ValueError, TypeError):
        raise AgentError("Сервис вернул некорректный ответ. Повтор не выполнялся.") from None


def dzen_posts(key: str, slug: str, start: str, end: str) -> list[dict[str, Any]]:
    params = urlencode(
        {
            "date_publish_min": start,
            "date_publish_max": end,
            "type_post": "card",
            "sort": "views_till_end",
            "order": "desc",
            "limit": 5,
        }
    )
    value = request_json(
        "https://partner.zenstat.ru/api/v1/channels/" + quote(slug, safe="") + "/posts/?" + params,
        {"Authorization": key},
    )
    try:
        if value["status"] is not True:
            raise ValueError
        rows = value["response"]["posts"] or []
        if not isinstance(rows, list) or len(rows) > 5:
            raise ValueError
        result = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("channel_slug") != slug
                or row.get("type_post") != "card"
            ):
                raise ValueError
            if not isinstance(row.get("title"), str) or not 1 <= len(row["title"]) <= 500:
                raise ValueError
            url = row.get("url", "")
            from urllib.parse import urlsplit

            parsed = urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "dzen.ru":
                raise ValueError
            published = row["date_publish"]
            from datetime import datetime

            stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if stamp.tzinfo is None or not start <= stamp.date().isoformat() <= end:
                raise ValueError
            clean = {
                name: row.get(name)
                for name in (
                    "publication_id",
                    "title",
                    "url",
                    "date_publish",
                    "channel_slug",
                    "views",
                    "views_till_end",
                    "views_till_end_percent",
                    "views_time",
                    "avg_views_time",
                )
            }
            for name in ("views", "views_till_end"):
                count = clean[name]
                if count is not None and (type(count) is not int or count < 0):
                    raise ValueError
            result.append(clean)
        # Unknown read-through counts stay at the bottom, never substituted by views.
        return sorted(
            result,
            key=lambda p: p["views_till_end"] if p["views_till_end"] is not None else -1,
            reverse=True,
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        raise AgentError("Данные канала неполны или не соответствуют запросу.") from None


def deepseek_generate(
    key: str, model: str, system: str, inputs: dict[str, Any], output_limit: int
) -> dict[str, Any]:
    return request_json(
        "https://api.deepseek.com/chat/completions",
        {"Authorization": "Bearer " + key},
        {
            "model": model,
            "thinking": {"type": "disabled"},
            "max_tokens": output_limit,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(inputs, ensure_ascii=False)},
            ],
        },
    )
