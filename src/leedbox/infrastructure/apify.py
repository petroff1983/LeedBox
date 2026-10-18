"""Fixed-origin Apify adapter, bounded calls and complete text, no paid retries."""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from leedbox.domain.content_agent import MAX_BYTES, byte_size
from leedbox.infrastructure.agents import AgentError
from leedbox.infrastructure.clck import NoRedirect

ACTORS = {
    "telegram": "tri_angle~telegram-scraper",
}


def request(key: str, path: str, body: dict[str, Any] | None = None) -> Any:
    req = Request(
        "https://api.apify.com/v2/" + path,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with build_opener(NoRedirect()).open(req, timeout=30) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError
        return json.loads(raw)
    except HTTPError as error:
        raise AgentError(f"Apify отклонил запрос (HTTP {error.code}).", error.code) from None
    except (OSError, URLError, TimeoutError, ValueError):
        raise AgentError(
            "Нет подтверждённого ответа Apify; платный запуск не повторяется."
        ) from None


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{5,64}", value):
        raise AgentError("Некорректный идентификатор Apify")
    return value


def start(
    key: str,
    platform: str,
    url: str,
    days: int,
    budget: str,
    roots: list[str] | None = None,
    *,
    competitor: bool = False,
) -> dict[str, Any]:
    actor = ACTORS.get(platform)
    if platform == "telegram" and competitor:
        actor = "automation-lab~telegram-scraper"
        body: dict[str, Any] = {
            "channels": [urlsplit(url).path.strip("/")],
            "maxMessages": 20,
            "oldestMessageDate": (datetime.now(UTC) - timedelta(days=days)).date().isoformat(),
            "includeChannelInfo": False,
            "profileOnly": False,
        }
    elif platform == "telegram":
        body = {"profiles": [url], "collectMessages": True, "scrapeLastNDays": days}
    else:
        raise ValueError("Неизвестная площадка")
    params = urlencode(
        {
            "timeout": 180,
            "maxItems": 20 if competitor else 55,
            "maxTotalChargeUsd": budget,
            "restartOnError": "false",
        }
    )
    value = request(key, f"acts/{actor}/runs?{params}", body)
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, dict):
        raise AgentError("Нет подтверждения запуска Apify")
    identifier(data.get("id"))
    return data


def run_cost(data: dict[str, Any]) -> Decimal | None:
    """Apify may publish usageTotalUsd before final per-event charges arrive.

    These fixed actors include platform usage. Event totals and usageTotalUsd overlap;
    use the larger confirmed value, never add them and double-count the run.
    """
    values = []
    raw = data.get("usageTotalUsd")
    if raw is not None:
        value = Decimal(str(raw))
        if value.is_finite() and value >= 0:
            values.append(value)
    info = data.get("pricingInfo", {})
    prices = info.get("pricingPerEvent", {}).get("actorChargeEvents", {})
    counts = data.get("chargedEventCounts", {})
    if info.get("pricingModel") == "PAY_PER_EVENT" and prices:
        total = Decimal(0)
        for name, count in counts.items():
            if type(count) is not int or count < 0 or name not in prices:
                raise ValueError("Invalid Apify event charge")
            rate = Decimal(str(prices[name]["eventPriceUsd"]))
            if not rate.is_finite() or rate < 0:
                raise ValueError("Invalid Apify event rate")
            total += count * rate
        if counts:
            values.append(total)
    return max(values) if values else None


def poll(key: str, run_id: str) -> dict[str, Any]:
    value = request(key, "actor-runs/" + identifier(run_id))
    data = value.get("data") if isinstance(value, dict) else None
    if not isinstance(data, dict):
        raise AgentError("Нет статуса Apify")
    return data


def items(key: str, dataset: str, *, limit: int = 55) -> list[dict[str, Any]]:
    if limit not in {20, 55}:
        raise ValueError("Unsupported collection limit")
    value = request(
        key, "datasets/" + identifier(dataset) + f"/items?format=json&clean=true&limit={limit}"
    )
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise AgentError("Неверный формат подборки Apify")
    return value[:limit]


def metric(raw: Any) -> int | None:
    if type(raw) is int:
        return raw if raw >= 0 else None
    if isinstance(raw, str):
        value = raw.strip().lower().replace(" ", "").replace(",", ".")
        if re.fullmatch(r"\d+(?:\.\d+)?[kmкм]?", value):
            suffix = value[-1:]
            factor = 1000 if suffix in {"k", "к"} else 1000000 if suffix in {"m", "м"} else 1
            return int(Decimal(value[:-1] if factor != 1 else value) * factor)
    return None


def normalized(
    rows: list[dict[str, Any]], platform: str, url: str, days: int, *, links_only: bool = False
) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    username = urlsplit(url).path.strip("/@").casefold()
    posts: list[dict[str, Any]] = []
    seen = set()
    for row in rows:
        p = row.get("message") or row.get("post") or row
        if not isinstance(p, dict):
            continue
        if platform == "threads" and (p.get("is_reply") or p.get("is_repost")):
            continue  # Replies are attached only via the dedicated source_post_url adapter.
        author = p.get("username")
        if (
            platform == "threads"
            and isinstance(author, str)
            and author.casefold().lstrip("@") != username
        ):
            continue
        raw = p.get("description") or p.get("text_content") or p.get("text") or ""
        if isinstance(raw, dict):
            raw = raw.get("text") or raw.get("caption") or ""
        if not links_only and (not isinstance(raw, str) or len(raw.strip()) < 20):
            continue
        stamp = (
            p.get("fulldate")
            or p.get("created_at")
            or p.get("date")
            or p.get("created_at_timestamp")
        )
        try:
            date = (
                datetime.fromtimestamp(float(stamp), UTC)
                if isinstance(stamp, (float, int))
                else datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            )
            if date.tzinfo is None or date < cutoff or date > datetime.now(UTC) + timedelta(days=1):
                continue
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        link = p.get("post_url") or p.get("link") or p.get("url") or ""
        if not isinstance(link, str):
            continue
        parsed = urlsplit(link)
        if parsed.scheme != "https" or parsed.netloc not in {
            "t.me",
            "telegram.me",
            "threads.com",
            "www.threads.com",
            "threads.net",
            "www.threads.net",
        }:
            continue
        path = parsed.path.strip("/").removeprefix("s/")
        if platform == "telegram" and path.split("/")[0].casefold() != username:
            continue
        if platform == "threads" and path.split("/")[0].lstrip("@").casefold() != username:
            continue
        if link in seen:
            continue
        seen.add(link)
        metrics = {
            name: metric(p.get(key))
            for name, key in (
                (("views", "views"), ("reactions", "reactions_count"))
                if platform == "telegram"
                else (
                    ("likes", "like_count"),
                    ("replies", "reply_count"),
                    ("reposts", "repost_count"),
                    ("quotes", "quote_count"),
                    ("views", "view_count"),
                )
            )
        }
        known = [v for k, v in metrics.items() if k != "views" and v is not None]
        if platform == "telegram" and metrics.get("reactions") is None:
            reactions = p.get("reactions")
            if isinstance(reactions, list):
                counts = [
                    metric(item.get("count")) if isinstance(item, dict) else None
                    for item in reactions
                ]
                if all(count is not None for count in counts):
                    metrics["reactions"] = sum(count for count in counts if count is not None)
        rank = metrics.get("views") if platform == "telegram" else sum(known) if known else None
        posts.append(
            {
                "id": link,
                **({} if links_only else {"text": raw.strip()}),
                "url": link,
                "published": date.isoformat(),
                "metrics": metrics,
                "rank": rank,
                "thread_incomplete": platform == "threads",
            }
        )
    posts.sort(key=lambda p: p["rank"] if p["rank"] is not None else -1, reverse=True)
    selected: list[dict[str, Any]] = []
    for post in posts:
        if len(selected) == 10:
            break
        if links_only or byte_size(selected + [post]) <= MAX_BYTES:
            selected.append(post)
    return selected


def attach_replies(posts: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only author-to-self replies mapped by an explicit source URL."""
    result: list[dict[str, Any]] = []
    for index, original in enumerate(posts):
        post = dict(original)
        username = urlsplit(post["url"]).path.split("/")[1].lstrip("@").casefold()
        replies, seen = [], set()
        for row in rows:
            if row.get("source_post_url") != post["url"]:
                continue
            if row.get("item_type") == "original_post":
                post["thread_incomplete"] = row.get("is_public_subset") is not False
                continue
            if (
                row.get("item_type") != "reply"
                or str(row.get("author_username", "")).casefold().lstrip("@") != username
                or str(row.get("reply_to_username", "")).casefold().lstrip("@") != username
            ):
                continue
            text = row.get("text_content")
            identity = row.get("reply_id") or row.get("reply_url")
            if isinstance(text, str) and text.strip() and identity and identity not in seen:
                replies.append((str(row.get("created_at", "")), text.strip()))
                seen.add(identity)
        post["text"] = "\n\n".join([post["text"]] + [text for _, text in sorted(replies)])
        # Keep room for every remaining root; augmentation must not evict source posts.
        if byte_size(result + [post] + posts[index + 1 :]) <= MAX_BYTES:
            result.append(post)
        else:
            post = dict(original)
            post["thread_incomplete"] = True
            result.append(post)
    return result
