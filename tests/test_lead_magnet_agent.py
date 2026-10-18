import pytest

from leedbox.domain import lead_magnet_agent as domain
from leedbox.telegram.agents import agent_rows


def magnets(prefix="Первый"):
    return {
        "lead_magnets": [
            {
                "id": i,
                "title": f"{prefix}: что проверить перед запуском рекламы {i}",
                "format": "Короткая текстовая инструкция",
                "description": "Поможет найти типичные ошибки до расходования рекламного бюджета.",
            }
            for i in range(1, 6)
        ]
    }


def critique():
    return {
        "critiques": [
            {
                "id": i,
                "issues": ["Недостаточно конкретный пример"],
                "fix": "Добавьте применимый пример для начинающих",
            }
            for i in range(1, 6)
        ]
    }


def legacy_magnets():
    result = magnets()
    for item in result["lead_magnets"]:
        item["benefit"] = item.pop("description")
        item["content"] = [f"Пункт {i}: проверьте настройки" for i in range(3)]
        item["announcement"] = "Заберите материал: [ссылка]"
        item["bridge"] = "Запишитесь на консультацию"
    return result


def test_five_concise_topics_and_critique_contracts():
    assert len(domain.validate(magnets())["lead_magnets"]) == 5
    assert len(domain.validate_critique(critique())["critiques"]) == 5
    output = domain.markdown(domain.validate(magnets()))
    assert "Пять тем" in output and "Формат:" in output
    assert "Анонс получения" not in output and "Пункт" not in output
    assert "lm:begin" in str(agent_rows({"agents"}))
    assert "не реальные отзывы" in domain.CRITIC
    assert "Обязательный контракт" in domain.instruction(0, "Своя инструкция владельца")
    assert set(domain.validate(magnets())["lead_magnets"][0]) == {
        "id",
        "title",
        "description",
        "format",
    }


def test_historical_materials_still_export_and_critic_uses_only_the_idea():
    old = domain.validate(legacy_magnets(), ideas=False)
    assert "Пункт 0" in domain.markdown(old)
    assert "Анонс получения" in domain.markdown(old)
    summary = domain.idea_input(old)
    assert "description" in summary["lead_magnets"][0]
    assert "content" not in str(summary) and "announcement" not in str(summary)


@pytest.mark.parametrize("case", ["count", "order", "empty", "duplicate"])
def test_invalid_magnets(case):
    value = magnets()
    if case == "count":
        value["lead_magnets"].pop()
    elif case == "order":
        value["lead_magnets"][0]["id"] = 2
    elif case == "empty":
        value["lead_magnets"][0]["description"] = ""
    else:
        value["lead_magnets"][1]["title"] = value["lead_magnets"][0]["title"]
    with pytest.raises(ValueError):
        domain.validate(value)
