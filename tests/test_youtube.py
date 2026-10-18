import io
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import pytest

from leedbox.application.youtube import quota_day
from leedbox.domain.youtube import (
    ChannelSnapshot,
    Video,
    channel_reference,
    report_csv,
    report_text,
    top_videos,
)
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.youtube import YouTubeClient, YouTubeError
from leedbox.telegram.agents import agent_rows
from leedbox.telegram.handlers import menu

CHANNEL = "UC" + "a" * 22


@pytest.mark.parametrize(
    "value,parameter,expected",
    [
        ("@GoogleDevelopers", "forHandle", "@GoogleDevelopers"),
        (
            "https://www.youtube.com/@GoogleDevelopers/videos?view=0",
            "forHandle",
            "@GoogleDevelopers",
        ),
        ("youtube.com/@пример", "forHandle", "@пример"),
        (f"https://youtube.com/channel/{CHANNEL}", "id", CHANNEL),
        (CHANNEL, "id", CHANNEL),
        ("https://youtube.com/user/GoogleDevelopers", "forUsername", "GoogleDevelopers"),
    ],
)
def test_channel_reference(value, parameter, expected):
    result = channel_reference(value)
    assert (result.parameter, result.value) == (parameter, expected)


@pytest.mark.parametrize(
    "raw",
    [
        "https://youtube.com.evil/@name",
        "https://youtube.com@evil/@name",
        "https://youtube.com:443/@name",
        "http://127.0.0.1",
        "file:///etc/passwd",
        "https://youtu.be/abcdefghijk",
        "https://youtube.com/watch?v=abcdefghijk",
        "https://youtube.com/c/oldname",
        "@name\n@other",
        "@abc/../../channels",
        "@a",
    ],
)
def test_invalid_urls_never_fetch_user_origin(raw):
    with pytest.raises(ValueError):
        channel_reference(raw)


def sample():
    now = datetime.now(UTC)
    return ChannelSnapshot(
        CHANNEL,
        "Синтетический канал",
        now.isoformat(),
        51,
        True,
        [
            Video("abcdefghijk", "=1+1", (now - timedelta(days=2)).isoformat(), 100, None, 2),
            Video(
                "lmnopqrstuv", "Новый ролик", (now - timedelta(days=1)).isoformat(), 200, 0, None
            ),
            Video(
                "12345678901", "Старый хит", (now - timedelta(days=100)).isoformat(), 1000, 10, 5
            ),
        ],
    )


def test_raw_view_ranking_date_filter_missing_metrics_and_safe_export():
    snapshot = sample()
    assert [v.views for v in top_videos(snapshot, 30, 5)] == [200, 100]
    assert top_videos(snapshot, 0, 10)[0].views == 1000
    assert ChannelSnapshot.from_payload(snapshot.payload()) == snapshot
    text = report_text(snapshot, 30, 5)
    assert "не всей истории" in text and "нет данных" in text and "Старый хит" not in text
    csv = report_csv(snapshot, 30, 5).decode("utf-8-sig")
    assert "'=1+1" in csv and "abcdefghijk" in csv
    with pytest.raises(ValueError):
        top_videos(snapshot, 1, 50)


def test_empty_period_is_not_success_claim():
    snapshot = sample()
    snapshot.videos.clear()
    assert "нет доступных видео" in report_text(snapshot, 30, 5)


def test_quota_day_uses_pacific_reset_and_dst():
    assert quota_day(datetime(2026, 10, 12, 6, tzinfo=UTC)) == datetime(2026, 10, 11, 7, tzinfo=UTC)
    assert quota_day(datetime(2026, 12, 12, 9, tzinfo=UTC)) == datetime(2026, 12, 12, 8, tzinfo=UTC)


def test_fixed_origin_secret_header_and_quota_reserved_before_network():
    reserve = Mock()
    response = Mock()
    response.read.return_value = b'{"items": []}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    with patch("leedbox.infrastructure.youtube.build_opener", return_value=opener):
        assert YouTubeClient("private-api-key", reserve).get("channels", part="snippet") == {
            "items": []
        }
    request = opener.open.call_args.args[0]
    assert request.full_url.startswith("https://www.googleapis.com/youtube/v3/channels?")
    assert "private-api-key" not in request.full_url
    assert request.get_header("X-goog-api-key") == "private-api-key"
    reserve.assert_called_once()


@pytest.mark.parametrize("reason,quota", [("quotaExceeded", True), ("keyInvalid", False)])
def test_api_error_is_sanitized(reason, quota):
    body = io.BytesIO(
        json.dumps(
            {"error": {"errors": [{"reason": reason}], "message": "private-api-key"}}
        ).encode()
    )
    error = HTTPError("https://secret/", 403, "private-api-key", {}, body)
    with patch("leedbox.infrastructure.youtube.build_opener") as opener:
        opener.return_value.open.side_effect = error
        with pytest.raises(YouTubeError) as caught:
            YouTubeClient("private-api-key", lambda: None).get("channels")
        assert caught.value.quota is quota
        assert "private-api-key" not in str(caught.value)


def test_network_error_has_no_secret():
    with patch("leedbox.infrastructure.youtube.build_opener") as opener:
        opener.return_value.open.side_effect = URLError("private-api-key")
        with pytest.raises(YouTubeError, match="Нет соединения"):
            YouTubeClient("private-api-key", lambda: None).get("channels")


def test_paginated_scan_batches_and_preserves_zero_metrics():
    pages = [
        {
            "items": [
                {
                    "id": CHANNEL,
                    "snippet": {"title": "Тест"},
                    "contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}},
                }
            ]
        },
        {"items": [{"contentDetails": {"videoId": "abcdefghijk"}}], "nextPageToken": "p2"},
        {"items": [{"contentDetails": {"videoId": "lmnopqrstuv"}}]},
        {
            "items": [
                {
                    "id": "abcdefghijk",
                    "snippet": {
                        "title": "Тест",
                        "channelId": CHANNEL,
                        "publishedAt": "2026-10-01T00:00:00Z",
                    },
                    "status": {"privacyStatus": "public"},
                    "statistics": {"viewCount": "0", "likeCount": "0"},
                }
            ]
        },
    ]
    client = YouTubeClient("test", lambda: None)
    with patch.object(client, "get", side_effect=pages) as get:
        report = client.scan(channel_reference("@test"), 100)
    assert report.checked == 2 and not report.truncated
    assert report.videos[0].views == 0 and report.videos[0].likes == 0
    assert report.videos[0].comments is None
    assert get.call_args_list[-1].kwargs["id"] == "abcdefghijk,lmnopqrstuv"


def test_menu_addition_and_secret_settings():
    config = Settings(
        database_url="postgresql+psycopg://a:b@localhost/test",
        youtube_api_key="private-youtube-key",
    )
    assert "private-youtube-key" not in repr(config)
    assert not any(
        b.callback_data == "section:youtube" for row in menu(config, 1).inline_keyboard for b in row
    )
    assert ("📊 Анализ YouTube", "section:youtube") in [
        button for row in agent_rows({"agents", "youtube"}) for button in row
    ]


def test_youtube_agent_obeys_own_and_parent_visibility():
    assert "section:youtube" not in str(agent_rows({"agents"}))
    assert agent_rows({"youtube"}) == []
    assert "az:begin" in str(agent_rows({"agents"}))
    assert "ip:begin" in str(agent_rows({"agents"}))
