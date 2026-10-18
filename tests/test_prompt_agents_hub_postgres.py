# ruff: noqa: F811 -- pytest fixture injection.
import re

import pytest
from test_admin_postgres import admin_fixture  # noqa: F401
from test_services_postgres import service_fixture  # noqa: F401

RENDER_SNAPSHOTS = {}
RENDER_ASSETS = {}


@pytest.mark.integration
def test_unified_hub_links_and_legacy_routes_remain_valid(admin_fixture):
    client, _, _, _, _ = admin_fixture
    page = client.get("/owner/ai/")
    assert page.status_code == 200
    assert "Промпты и агенты" in page.text
    for label in (
        "Дзен",
        "Картинки",
        "Контент-аналитик",
        "Лид-магниты",
        "YouTube",
        "Анализ ЦА",
        "Контент-стратегия",
    ):
        assert label in page.text
    for path in set(
        re.findall(
            r'href="(/owner/(?:prompts|settings|agents|sections|content-|history)[^"]*)"', page.text
        )
    ):
        assert client.get(path.replace("&amp;", "&")).status_code == 200, path
    assert client.get("/owner/agents/").status_code == 200
    assert client.get("/owner/prompts/").status_code == 200
    assert "ai_content_plan_prompt" not in client.get("/owner/settings/").text
    views = {view.endpoint: view for view in client.application.extensions["leedbox_admin"]._views}
    assert views["owner_agents"].hidden_from_navigation
    assert views["owner_prompts"].hidden_from_navigation
    assert not views["owner_prompt_agents"].hidden_from_navigation
    RENDER_SNAPSHOTS["prompt-agents"] = page.get_data()
    for path in re.findall(rb'(?:src|href)="(/(?:owner/)?static/[^"?]+)', page.get_data()):
        asset = client.get(path.decode())
        RENDER_ASSETS[path.decode()] = (asset.get_data(), asset.content_type)
