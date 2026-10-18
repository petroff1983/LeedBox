"""Fixed-origin read-only YouTube API. No scraping, redirects, OAuth, or URL logging."""

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from leedbox.domain.youtube import ChannelReference, ChannelSnapshot, Video
from leedbox.infrastructure.clck import NoRedirect


class YouTubeError(Exception):
    def __init__(self, message: str, *, quota: bool = False) -> None:
        super().__init__(message)
        self.quota = quota


class YouTubeClient:
    def __init__(self, key: str, reserve: Callable[[], None]) -> None:
        self.key = key
        self.reserve = reserve
        self.deadline = time.monotonic() + 90

    def get(self, resource: str, **params: str) -> dict[str, Any]:
        if resource not in {"channels", "playlistItems", "videos"}:
            raise ValueError("Read-only endpoint required")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise YouTubeError("Проверка заняла слишком много времени. Попробуйте позже.")
        self.reserve()
        request = Request(
            "https://www.googleapis.com/youtube/v3/" + resource + "?" + urlencode(params),
            headers={"X-Goog-Api-Key": self.key, "Accept": "application/json"},
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=min(15, remaining)) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("items"), list):
                raise ValueError
            return value
        except HTTPError as error:
            reason = ""
            try:
                body = json.loads(error.read(65536))
                reason = body["error"]["errors"][0]["reason"]
            except (ValueError, KeyError, TypeError, IndexError):
                pass
            if reason in {"quotaExceeded", "dailyLimitExceeded"}:
                raise YouTubeError(
                    "Дневная квота YouTube исчерпана. Попробуйте после её обновления.", quota=True
                ) from None
            if error.code == 429 or reason in {"rateLimitExceeded", "userRateLimitExceeded"}:
                raise YouTubeError(
                    "YouTube ограничил частоту запросов. Попробуйте позже."
                ) from None
            if error.code == 404:
                raise YouTubeError("Канал или его загрузки недоступны. Проверьте ссылку.") from None
            if error.code in {400, 401, 403}:
                raise YouTubeError(
                    "YouTube отклонил запрос. Владелец должен проверить ключ и включение API."
                ) from None
            raise YouTubeError("YouTube временно недоступен. Попробуйте позже.") from None
        except (URLError, TimeoutError, OSError):
            raise YouTubeError("Нет соединения с YouTube API. Попробуйте позже.") from None
        except (ValueError, TypeError):
            raise YouTubeError("YouTube вернул некорректный ответ. Попробуйте позже.") from None

    def scan(self, reference: ChannelReference, limit: int) -> ChannelSnapshot:
        try:
            response = self.get(
                "channels",
                part="snippet,contentDetails",
                **{
                    reference.parameter: reference.value,
                },
            )
            if not response["items"]:
                raise YouTubeError("Канал не найден. Проверьте @имя или ID канала.")
            channel = response["items"][0]
            channel_id = channel["id"]
            if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
                raise ValueError
            playlist = channel["contentDetails"]["relatedPlaylists"]["uploads"]
            title = str(channel["snippet"]["title"])[:200]
            ids: list[str] = []
            checked, token, pages = 0, "", 0
            seen_tokens: set[str] = set()
            while checked < limit and pages < (limit + 49) // 50:
                page = self.get(
                    "playlistItems",
                    part="contentDetails",
                    playlistId=playlist,
                    maxResults=str(min(50, limit - checked)),
                    pageToken=token,
                )
                pages += 1
                for item in page["items"]:
                    checked += 1
                    video_id = item["contentDetails"]["videoId"]
                    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) and video_id not in ids:
                        ids.append(video_id)
                token = page.get("nextPageToken", "")
                if not token:
                    break
                if token in seen_tokens:
                    raise ValueError
                seen_tokens.add(token)
            videos: list[Video] = []
            for offset in range(0, len(ids), 50):
                page = self.get(
                    "videos",
                    part="snippet,statistics,status",
                    id=",".join(ids[offset : offset + 50]),
                )
                for item in page["items"]:
                    snippet, stats = item["snippet"], item.get("statistics", {})
                    if (
                        item["id"] not in ids
                        or snippet["channelId"] != channel_id
                        or item.get("status", {}).get("privacyStatus") != "public"
                        or snippet.get("liveBroadcastContent") == "upcoming"
                        or "viewCount" not in stats
                    ):
                        continue
                    published = datetime.fromisoformat(
                        snippet["publishedAt"].replace("Z", "+00:00")
                    )
                    if published.tzinfo is None:
                        raise ValueError
                    views = int(stats["viewCount"])
                    likes = int(stats["likeCount"]) if "likeCount" in stats else None
                    comments = int(stats["commentCount"]) if "commentCount" in stats else None
                    if any(n is not None and n < 0 for n in (views, likes, comments)):
                        raise ValueError
                    if any(video.id == item["id"] for video in videos):
                        continue
                    videos.append(
                        Video(
                            item["id"],
                            str(snippet["title"])[:200],
                            published.astimezone(UTC).isoformat(),
                            views,
                            likes,
                            comments,
                        )
                    )
            return ChannelSnapshot(
                channel_id, title, datetime.now(UTC).isoformat(), checked, bool(token), videos
            )
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            raise YouTubeError("YouTube вернул неполные данные. Попробуйте позже.") from None
