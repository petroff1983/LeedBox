from copy import deepcopy

import pytest

from leedbox.domain.content import assemble, landing, offer, pattern, validate_catalog
from leedbox.infrastructure.content_seed import starter_entries
from leedbox.telegram.content_tools import render_result


def entries(kind, tool=None):
    return [
        values
        for category, values in starter_entries()
        if category == kind and (tool is None or values["payload"].get("tool") == tool)
    ]


def test_starter_content_is_diverse_valid_and_has_no_removed_niches():
    catalogs = starter_entries()
    assert len(entries("niche")) == 12
    assert len(entries("idea")) == 140
    assert len(entries("template")) == 15
    niches = {row["slug"] for row in entries("niche")}
    assert {"marketer", "targetologist", "psychologist", "marketplace"} <= niches
    assert not {"photographer", "consultant", "vibecoding", "direct"} & niches
    for kind, value in catalogs:
        assert validate_catalog(kind, value) == value
    for niche in niches:
        assert len([row for row in entries("idea") if row["payload"]["niche"] == niche]) == 10
    assert (
        len(
            {
                tuple(row["payload"]["structure"])
                for row in entries("idea")
                if row["payload"]["niche"]
            }
        )
        == 120
    )


@pytest.mark.parametrize(
    "template",
    [
        "{product.__class__}",
        "{product[0]}",
        "{product!r}",
        "{product:100}",
        "{unknown}",
        "{product",
    ],
)
def test_templates_reject_expressions_and_unknown_fields(template):
    with pytest.raises(ValueError):
        pattern(template)


def test_offer_never_invents_facts_and_checks_configured_limits():
    templates = entries("template", "offer")
    result = offer({"product": "Мой продукт"}, templates, "ad", {"ad": 20})
    assert result.count("[впишите]") >= 10
    assert "Нужно сократить вручную" in result
    assert "маркировки" in result and "Модерация не гарантируется" in result
    assert "Мой продукт" in result
    assert "миллион" not in result.lower() and "гарантированный" not in result.lower()
    assert (
        assemble("Факт: {evidence}", {"evidence": "{product.__class__}"})
        == "Факт: {product.__class__}"
    )


@pytest.mark.parametrize(
    "kind", ["service", "consultation", "course", "webinar", "product", "channel"]
)
@pytest.mark.parametrize("price", ["low", "medium", "high"])
@pytest.mark.parametrize("temperature", ["cold", "warm"])
def test_landing_screen_rules_and_absence_of_fabricated_evidence(kind, price, temperature):
    result = landing(
        {"page_type": kind, "price_band": price, "temperature": temperature},
        entries("template", "landing")[0],
    )
    count = result.count("\n## ")
    assert 5 <= count <= 12
    if temperature == "warm" or kind == "webinar":
        assert count <= 6 and "Исходная задача" not in result
    if temperature == "cold" and price == "high" and kind != "webinar":
        assert count >= 10 and "Работа с возражениями" in result
    assert "Реальные подтверждения:" not in result
    assert "[впишите]" in result and "Процесс и первый шаг" in result


def test_unknown_niche_uses_universal_ideas_and_historical_catalog_is_frozen():
    catalog = entries("idea")
    draft = {
        "tool": "ideas",
        "catalog": deepcopy(catalog),
        "niches": entries("niche"),
        "values": {
            "niche": "Другое направление",
            "audience": "Моя аудитория",
            "goal": "requests",
            "format": "video",
        },
    }
    text, selected = render_result(draft)
    assert len(selected) == 5 and all(item["payload"]["niche"] is None for item in selected)
    assert "других форматов" in text and "универсальные" in text
    original = text
    catalog[0]["title"] = "Новая версия"
    assert render_result(draft)[0] == original
    draft["offset"] = 5
    assert render_result(draft)[0] != original
