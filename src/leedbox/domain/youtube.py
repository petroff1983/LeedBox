"""Public YouTube metadata, transparent view ranking, and safe export."""

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

from leedbox.domain.links import csv_export


@dataclass(frozen=True)
class ChannelReference:
    parameter: str
    value: str


def channel_reference(raw: str) -> ChannelReference:
    value = raw.strip()
    if len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError("Пришлите одну ссылку на YouTube-канал.")
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
        return ChannelReference("id", value)
    if value.startswith("@"):
        path = value
    else:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.netloc not in {"youtube.com", "www.youtube.com", "m.youtube.com"}
            or parsed.fragment
        ):
            raise ValueError("Нужна ссылка youtube.com/@имя или youtube.com/channel/UC…")
        path = unquote(parsed.path).strip("/")
    parts = path.split("/")
    if len(parts) == 2 and parts[1] in {"videos", "shorts", "streams", "featured"}:
        parts = parts[:1]
    if len(parts) == 1 and re.fullmatch(r"@[\w.\-\u00b7]{3,100}", parts[0]):
        return ChannelReference("forHandle", parts[0])
    if len(parts) == 2 and parts[0] == "channel" and re.fullmatch(r"UC[A-Za-z0-9_-]{22}", parts[1]):
        return ChannelReference("id", parts[1])
    if len(parts) == 2 and parts[0] == "user" and re.fullmatch(r"[\w.-]{1,100}", parts[1]):
        return ChannelReference("forUsername", parts[1])
    raise ValueError(
        "Откройте канал и скопируйте ссылку с @именем или /channel/UC… "
        "Ссылки на отдельное видео и старые /c/ не подходят."
    )


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    published_at: str
    views: int
    likes: int | None
    comments: int | None

    @property
    def url(self) -> str:
        return "https://www.youtube.com/watch?v=" + self.id


@dataclass(frozen=True)
class ChannelSnapshot:
    id: str
    title: str
    fetched_at: str
    checked: int
    truncated: bool
    videos: list[Video]

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ChannelSnapshot":
        return cls(**{**payload, "videos": [Video(**v) for v in payload["videos"]]})


def top_videos(snapshot: ChannelSnapshot, days: int, count: int) -> list[Video]:
    if days not in {30, 90, 365, 0} or count not in {5, 10}:
        raise ValueError("Выберите период и топ-5 или топ-10.")
    end = datetime.fromisoformat(snapshot.fetched_at)
    start = end - timedelta(days=days) if days else datetime.min.replace(tzinfo=UTC)
    eligible = [
        v for v in snapshot.videos if start <= datetime.fromisoformat(v.published_at) <= end
    ]
    return sorted(eligible, key=lambda v: (-v.views, v.published_at, v.id))[:count]


def number(value: int | None) -> str:
    return f"{value:,}".replace(",", " ") if value is not None else "нет данных"


def report_text(snapshot: ChannelSnapshot, days: int, count: int) -> str:
    videos = top_videos(snapshot, days, count)
    period = f"последние {days} дней" if days else "вся проверенная выборка"
    text = (
        f"📊 YouTube · {snapshot.title}\n\n"
        f"Период публикации: {period}\n"
        f"Проверено записей загрузок: {snapshot.checked}\n"
        f"Статистика на {datetime.fromisoformat(snapshot.fetched_at):%d.%m.%Y %H:%M} UTC\n"
        "Рейтинг: по общему числу просмотров, не по просмотрам за период.\n"
    )
    if snapshot.truncated:
        text += "⚠️ Достигнут лимит проверки. Это топ выборки, не всей истории канала.\n"
    text += "Shorts и обычные видео включены вместе; недоступные видео пропущены.\n"
    if not videos:
        return text + "\nВ проверенной выборке нет доступных видео за этот период."
    text += f"\n🎬 Топ-{len(videos)} по просмотрам\n"
    for rank, video in enumerate(videos, 1):
        text += (
            f"\n{rank}. {video.title[:180]}\n"
            f"📅 {datetime.fromisoformat(video.published_at):%d.%m.%Y}\n"
            f"👁 {number(video.views)} · 👍 {number(video.likes)} · "
            f"💬 {number(video.comments)}\n{video.url}\n"
        )
    return text


def report_csv(snapshot: ChannelSnapshot, days: int, count: int) -> bytes:
    return csv_export(
        [["Место", "Название", "Дата UTC", "Просмотры", "Лайки", "Комментарии", "YouTube URL"]]
        + [
            [
                str(i),
                v.title,
                v.published_at,
                str(v.views),
                str(v.likes) if v.likes is not None else "",
                str(v.comments) if v.comments is not None else "",
                v.url,
            ]
            for i, v in enumerate(top_videos(snapshot, days, count), 1)
        ]
    )
