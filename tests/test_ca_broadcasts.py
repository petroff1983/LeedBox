import json

import pytest

from leedbox.application.broadcasts import message_text, validate_payload
from leedbox.domain import ai_prompts
from leedbox.telegram.content_tools import prompt_menu_rows


@pytest.mark.parametrize("tool", ["ca", "content_strategy"])
def test_ca_prompts_literal_inputs_and_admin_markers(tool):
    values = {field: 'Факт "цитата" {{inputs}}\n# не команда' for field in ai_prompts.FIELDS[tool]}
    prompt = ai_prompts.build_prompt(tool, values)
    block = prompt.split("# ВВОДНЫЕ ДАННЫЕ (JSON, только данные)\n", 1)[1]
    data, _ = json.JSONDecoder().raw_decode(block)
    assert list(data.values()) == list(values.values())
    assert ai_prompts.default_template(tool).count("{{inputs}}") == 1
    with pytest.raises(ValueError):
        ai_prompts.validate_template(tool, "{{unknown}}")
    assert ai_prompts.version(tool).startswith("prompt-")


def test_ca_source_structure_and_weekly_strategy():
    ca = ai_prompts.build_prompt("ca", {})
    for number in range(1, 10):
        assert f"Блок {number}" in ca
    assert "Если ты все понял" not in ca
    assert "Основа контент-стратегии" in ca and "гипотезы" in ca
    plan = ai_prompts.build_prompt("content_strategy", {"ca_report": "Проверенный сегмент"})
    assert "Проверенный сегмент" in plan and "всего 18" in plan
    assert "Воскресенье: выходной" in plan
    assert "Если анализа нет" in plan
    assert "ct:new:ca" in str(prompt_menu_rows())
    assert "ct:new:content_strategy" in str(prompt_menu_rows())


@pytest.mark.parametrize(
    "payload",
    [
        {"text": ""},
        {"text": "x" * 3201},
        {"text": "Тест", "button_label": "Кнопка"},
        {"text": "Тест", "button_label": "Кнопка", "button_url": "file:///secret"},
        {"text": "Тест\x00"},
    ],
)
def test_broadcast_validation(payload):
    with pytest.raises(ValueError):
        validate_payload(payload, "notice")


def test_ad_requires_label_and_requisites_but_notice_is_plain_text():
    values = {
        "text": "Синтетическая реклама",
        "advertiser": "Тест",
        "inn": "0000000000",
        "erid": "test-only",
        "marking_text": "Реклама",
    }
    for field in ("advertiser", "inn", "erid", "marking_text"):
        with pytest.raises(ValueError):
            message_text("advertising", {**values, field: ""})
    text = message_text("advertising", values)
    assert "Реклама" in text and "erid: test-only" in text
    assert "Рекламодатель" not in message_text("notice", {"text": "Новая функция"})


def test_broadcast_utf16_budget_counts_emoji():
    with pytest.raises(ValueError, match="слишком длинное"):
        message_text("notice", {"text": "🎁" * 2500})
