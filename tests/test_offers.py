import re
from copy import deepcopy

import pytest

from leedbox.domain import offers
from leedbox.domain.content import validate_catalog


def facts():
    return {
        "product": "Курс Excel",
        "audience": "Бухгалтеры малого бизнеса",
        "problem": "Отчёты занимают весь вечер",
        "desired_result": "Отчёт за 30 минут",
        "advantages": "Разбор собственных отчётов с наставником",
        "action": "Оставьте заявку",
    }


def test_three_models_use_actual_facts_and_not_question_examples():
    templates = offers.starter_templates()
    for row in templates:
        assert validate_catalog("template", row) == row
    values = facts()
    parts = offers.variants(values, templates)
    assert len(parts) == 3 and len(set(parts)) == 3
    for body in parts:
        for key in ("product", "audience", "desired_result", "advantages", "action"):
            assert values[key] in body
        assert set(re.findall(r"\d+", body)) == {"30"}
        assert "возврат" not in body and "срочно" not in body and "[впишите]" not in body
        assert "срок:" not in body and "Условия предложения" not in body
    assert values["problem"] in parts[1]
    assert "Ваше УТП" in offers.report(values, templates)


def test_optional_conditions_and_attempts_only_when_supplied():
    values = {
        **facts(),
        "previous_attempts": "Видеоуроки без обратной связи не помогли",
        "timeframe": "за 4 недели",
        "evidence": "Отзыв на странице курса",
        "risk_reversal": "Возврат в течение 7 дней до второго урока",
        "urgency": "Бонус: разбор таблицы при оплате до 15 октября",
    }
    parts = offers.variants(values, offers.starter_templates())
    assert values["previous_attempts"] in parts[1]
    assert values["evidence"] in parts[0]
    for body in parts:
        assert values["risk_reversal"] in body and values["urgency"] in body
    assert "Скидка" not in " ".join(parts)


def test_direct_style_does_not_add_numbers_or_overwrite_answers():
    values = facts()
    original = deepcopy(values)
    plain = offers.variants(values, offers.starter_templates())
    direct = offers.variants({**values, "offer_tone": "direct"}, offers.starter_templates())
    assert values == original
    for normal, revised in zip(plain, direct, strict=True):
        assert normal in revised and revised.startswith("Ваша задача:")


def test_owner_template_is_used_missing_model_is_rejected_and_old_inputs_have_placeholders():
    templates = offers.starter_templates()
    templates[0]["payload"]["formats"]["post"] = "Специальный шаблон: {product} — {desired_result}"
    assert offers.variants(facts(), templates)[0].startswith("Специальный шаблон:")
    with pytest.raises(ValueError):
        offers.variants(facts(), templates[:2])
    assert "[впишите]" in offers.report({}, templates)
