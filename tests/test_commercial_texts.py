from copy import deepcopy

import pytest

from leedbox.domain.commercial_texts import commercial_proposal, package_case


@pytest.mark.parametrize("placement", ["post", "page"])
def test_case_contains_only_supplied_facts_and_keeps_original(placement):
    facts = {
        "client": "Проект автора",
        "result": "Рост заявок с 2 до 4",
        "work": "Настроили {client.__class__}",
    }
    before = deepcopy(facts)
    output = package_case(facts, placement)
    assert "Рост заявок с 2 до 4" in output
    assert "Настроили {client.__class__}" in output
    assert output.count("[впишите]") == 4
    assert "гарантируем" not in output and "окупаемость" not in output
    assert facts == before


@pytest.mark.parametrize("kind", ["service", "consultation", "product"])
def test_proposals_have_distinct_composition_and_no_fabricated_terms(kind):
    facts = {
        "product": "Мой продукт",
        "price_conditions": "Цена по запросу",
        "contact": "@author_name",
    }
    output = commercial_proposal(facts, kind)
    assert "Мой продукт" in output and "Цена по запросу" in output and "@author_name" in output
    assert not any(character.isdecimal() for character in output)
    assert output.count("[впишите]") == 8
    expected = {
        "service": "Состав работ",
        "consultation": "длительность",
        "product": "Комплектация",
    }
    assert expected[kind] in output


@pytest.mark.parametrize("function,kind", [(package_case, "ad"), (commercial_proposal, "unknown")])
def test_unsupported_commercial_text_formats_are_rejected(function, kind):
    with pytest.raises(ValueError):
        function({}, kind)


@pytest.mark.parametrize("value", [123, True, "x" * 4001, "control\x00"])
def test_facts_are_validated_not_silently_replaced(value):
    with pytest.raises(ValueError):
        package_case({"client": value}, "post")
