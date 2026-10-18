"""Facts supplied by the expert, never enrichment or generated claims."""

from collections.abc import Iterable, Mapping

from leedbox.domain.materials import http_url

BRIEF_FIELDS = {
    "niche": ("Ниша", 100),
    "product": ("Продукт", 200),
    "audience": ("Аудитория", 200),
    "problem": ("Проблема", 1000),
    "desired_result": ("Желаемый результат", 1000),
    "advantages": ("Реальные преимущества", 1500),
    "evidence": ("Подтверждения", 1500),
    "price_conditions": ("Цена и условия", 1000),
    "tone": ("Тон", 500),
    "links": ("Ссылки", 2000),
}


def validate_answer(field: str, value: str) -> str:
    if field not in BRIEF_FIELDS:
        raise ValueError("Неизвестное поле брифа")
    value = value.strip()
    if (
        not value
        or len(value) > BRIEF_FIELDS[field][1]
        or any(ord(char) < 32 and char not in "\n\t" for char in value)
    ):
        raise ValueError(f"{BRIEF_FIELDS[field][0]}: от 1 до {BRIEF_FIELDS[field][1]} символов")
    if field == "links":
        urls = [line.strip() for line in value.splitlines() if line.strip()]
        if len(urls) > 10:
            raise ValueError("Не более 10 ссылок, каждая с новой строки")
        return "\n".join(http_url(url) for url in urls)
    return value


def missing_fields(brief: Mapping[str, object], fields: Iterable[str]) -> tuple[str, ...]:
    names = tuple(fields)
    if any(name not in BRIEF_FIELDS for name in names):
        raise ValueError("Неизвестное поле брифа")
    return tuple(
        name
        for name in names
        if not isinstance(brief.get(name), str) or not str(brief[name]).strip()
    )
