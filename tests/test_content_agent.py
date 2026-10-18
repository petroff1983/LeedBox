import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from leedbox.domain import content_agent as domain
from leedbox.infrastructure import apify
from leedbox.infrastructure.agents import AgentError
from leedbox.infrastructure.config import Settings
from leedbox.telegram.agents import agent_rows
from leedbox.telegram.sections import ACTIVE_SECTIONS, PREFIX_SECTIONS


def plan(ids=None):
    return {
        "patterns": [{"name": "Вопрос и понятный пример", "source_ids": ids or ["1"]}],
        "topics": [
            {
                "title": f"Новая тема {i}",
                "format": "история",
                "hook": "Неожиданная мысль",
                "goal": "обучение",
                "cta": "Поделитесь мнением",
            }
            for i in range(3)
        ],
    }


def posts(prefix="Итог", long=False):
    return {
        "posts": [
            {
                "title": f"Тема {i}",
                "parts": [
                    (
                        f"{prefix} {i}. "
                        + "Понятный пример помогает разобраться в теме. " * (18 if long else 3)
                    ).strip()
                ],
            }
            for i in range(3)
        ]
    }


@pytest.mark.parametrize(
    "raw,platform",
    [
        ("https://t.me/valid_name", "telegram"),
        ("threads.net/@expert", "threads"),
        ("https://www.threads.com/@expert", "threads"),
    ],
)
def test_public_source_urls(raw, platform):
    # Historical result readers still accept the previously supported URLs.
    assert domain.source_url(raw, platform).startswith("https://")


@pytest.mark.parametrize("url", ["https://threads.net/@expert", "https://www.threads.com/@expert"])
def test_threads_cannot_be_selected_or_started(url):
    assert domain.ACTIVE_PLATFORMS == {"telegram": "Telegram"}
    with pytest.raises(ValueError, match="Telegram"):
        domain.detect_source(url)
    with patch("leedbox.infrastructure.apify.request") as client:
        with pytest.raises(ValueError):
            apify.start("dummy", "threads", url, 30, "0.04", competitor=True)
        client.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "http://t.me/valid_name",
        "https://t.me.evil/valid_name",
        "https://t.me@evil/valid_name",
        "https://t.me:443/valid_name",
        "https://t.me/+private",
        "https://t.me/c/123/1",
        "https://t.me/valid_name/123",
        "https://t.me/valid_name?x=1",
        "https://evil.test/valid_name",
    ],
)
def test_invalid_or_private_urls(url):
    with pytest.raises(ValueError):
        domain.source_url(url, "telegram")


@pytest.mark.parametrize("size", [499, 500, 501, 1800])
def test_threads_never_exceed_500_and_never_lose_words(size):
    raw = ("абв " * 600)[:size].rstrip()
    parts = domain.split_thread(raw)
    assert all(0 < domain.units(p) <= 500 for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(raw.split())


def test_emoji_and_long_unbroken_fragment():
    parts = domain.split_thread("🙂 привет " * 100)
    assert len(parts) > 1 and all(domain.units(p) <= 500 for p in parts)
    with pytest.raises(ValueError):
        domain.split_thread("x" * 501)


def test_post_validation_splits_long_threads_but_not_telegram():
    tg = domain.posts_result(posts(long=True), "telegram")
    threads = domain.posts_result(posts(long=True), "threads")
    assert len(tg["posts"][0]["parts"]) == 1
    assert len(threads["posts"][0]["parts"]) > 1
    assert all(domain.units(p) <= 500 for item in threads["posts"] for p in item["parts"])
    assert "# Готовые посты" in domain.markdown(threads)
    with pytest.raises(ValueError):
        domain.posts_result({"posts": [posts()["posts"][0]] * 3}, "telegram")
    with pytest.raises(ValueError):
        domain.plan_result(plan(["unknown"]), {"1"})


@pytest.mark.parametrize(
    "raw,expected",
    [(None, None), (0, 0), ("0", 0), ("1.2k", 1200), ("2,5к", 2500), (True, None), ("-5", None)],
)
def test_metrics_unknown_is_not_zero(raw, expected):
    assert apify.metric(raw) == expected


def source_row(platform="telegram", **overrides):
    return {
        "description": "Полный текст публикации\n\n" + "Содержимое " * 30,
        "fulldate": datetime.now(UTC).isoformat(),
        "link": "https://t.me/example/1"
        if platform == "telegram"
        else "https://www.threads.com/@example/post/ROOT",
        **overrides,
    }


def test_full_text_and_zero_views_preserved():
    rows = apify.normalized([source_row(views=0)], "telegram", "https://t.me/example", 30)
    assert len(rows) == 1 and len(rows[0]["text"]) > 110
    assert "\n\n" in rows[0]["text"] and rows[0]["metrics"]["views"] == 0
    assert not apify.normalized(
        [source_row(link="https://t.me/stranger/1")], "telegram", "https://t.me/example", 30
    )
    assert not apify.normalized(
        [source_row(fulldate="yesterday")], "telegram", "https://t.me/example", 30
    )


def test_threads_unknown_rank_and_author_replies_only():
    root = apify.normalized(
        [source_row("threads")], "threads", "https://www.threads.com/@example", 30
    )
    assert root[0]["rank"] is None
    rows = [
        {
            "item_type": "reply",
            "source_post_url": root[0]["url"],
            "author_username": "example",
            "reply_to_username": "example",
            "reply_id": "r1",
            "text_content": "Авторское продолжение",
        },
        {
            "item_type": "reply",
            "source_post_url": root[0]["url"],
            "author_username": "stranger",
            "reply_to_username": "example",
            "reply_id": "r2",
            "text_content": "Чужой комментарий",
        },
        {
            "item_type": "reply",
            "source_post_url": root[0]["url"],
            "author_username": "example",
            "reply_to_username": "stranger",
            "reply_id": "r3",
            "text_content": "Ответ чужому комментатору",
        },
    ]
    result = apify.attach_replies(root, rows + rows)
    assert result[0]["text"].count("Авторское продолжение") == 1
    assert "Чужой комментарий" not in result[0]["text"]
    assert "Ответ чужому" not in result[0]["text"]
    assert result[0]["thread_incomplete"]  # Not enough evidence to claim full coverage.


def test_apify_fixed_origin_headers_limits_and_no_retries():
    with patch(
        "leedbox.infrastructure.apify.request", return_value={"data": {"id": "syntheticRun123"}}
    ) as request:
        apify.start(
            "synthetic-key", "telegram", "https://t.me/example", 30, "0.04", competitor=True
        )
        args = request.call_args.args
        assert "maxTotalChargeUsd=0.04" in args[1] and "maxItems=20" in args[1]
        assert args[2]["maxMessages"] == 20
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value.read.return_value = json.dumps([]).encode()
    with patch("leedbox.infrastructure.apify.build_opener", return_value=opener):
        assert apify.items("secret", "syntheticDataset123") == []
    req = opener.open.call_args.args[0]
    assert req.full_url.startswith("https://api.apify.com/v2/datasets/")
    assert "secret" not in req.full_url
    assert req.get_header("Authorization") == "Bearer secret"
    with pytest.raises(AgentError):
        apify.identifier("../outside")


def test_author_replies_keep_space_for_all_remaining_roots():
    roots = [
        {"id": str(i), "url": f"https://www.threads.com/@example/post/R{i}", "text": "x" * 5000}
        for i in range(3)
    ]
    replies = [
        {
            "item_type": "reply",
            "source_post_url": roots[0]["url"],
            "author_username": "example",
            "reply_to_username": "example",
            "reply_id": "large-reply",
            "text_content": "y" * 8000,
        }
    ]
    result = apify.attach_replies(roots, replies)
    assert len(result) == 3 and domain.byte_size(result) <= domain.MAX_BYTES
    assert all(
        item["text"] == original["text"] for item, original in zip(result, roots, strict=True)
    )
    assert result[0]["thread_incomplete"]


def test_new_agent_menu_gate_and_secret():
    assert "ca:begin" in str(agent_rows({"agents"}))
    assert PREFIX_SECTIONS["ca"] == ACTIVE_SECTIONS["content_agent"] == "agents"
    config = Settings(
        database_url="postgresql+psycopg://x:y@localhost/test", apify_api_token="secret-token"
    )
    assert "secret-token" not in repr(config)


@pytest.mark.parametrize("text", ["Текст " * 1000, "🌿" * 3000, "Короткий текст"])
def test_message_chunks_preserve_text_and_telegram_limit(text):
    from leedbox.domain.content_agent import message_chunks, units

    pieces = message_chunks(text)
    assert "".join(pieces) == text
    assert all(units(piece) <= 3500 for piece in pieces)


def test_competitor_links_only_auto_platform_and_large_post_metadata():
    assert set(domain.PLATFORMS) == {"telegram", "threads"}
    assert domain.detect_source("https://t.me/example")[0] == "telegram"
    with pytest.raises(ValueError, match="Telegram"):
        domain.detect_source("https://www.threads.com/@example")
    roots = apify.normalized(
        [
            source_row(
                description="SECRET_SOURCE_TEXT" * 10000,
                views=500,
                reactions=[{"count": "12"}, {"count": 3}],
            )
        ],
        "telegram",
        "https://t.me/example",
        30,
        links_only=True,
    )
    assert len(roots) == 1 and "text" not in roots[0]
    assert roots[0]["metrics"] == {"views": 500, "reactions": 15}
    result = domain.competitor_markdown({"source_links": roots, "checked": 1})
    assert "SECRET_SOURCE_TEXT" not in result and "https://t.me/example/1" in result
    assert "Просмотры: 500" in result
