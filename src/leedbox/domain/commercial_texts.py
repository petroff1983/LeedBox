"""Stage 4 fact-only composition; Telegram and managed catalogs are separate adapters."""

from collections.abc import Mapping
from typing import Any

from leedbox.domain.content import PLACEHOLDER, assemble, text

CASE_VERSION = "case-1"
PROPOSAL_VERSION = "proposal-1"
CASE_FIELDS = (
    ("client", "Клиент / проект"),
    ("task", "Исходная задача"),
    ("work", "Что сделали"),
    ("result", "Результат из данных автора"),
    ("evidence", "Подтверждения"),
    ("constraints", "Условия и ограничения"),
)
PROPOSAL_FIELDS = {
    "service": (
        ("client", "Для кого"),
        ("task", "Задача"),
        ("product", "Предлагаемая услуга"),
        ("scope", "Состав работ и границы"),
        ("process", "Этапы и результат каждого этапа"),
    ),
    "consultation": (
        ("client", "Для кого"),
        ("task", "Вопрос для консультации"),
        ("product", "Консультация"),
        ("scope", "Формат, длительность и подготовка"),
        ("process", "Что получает клиент после встречи"),
    ),
    "product": (
        ("client", "Для кого"),
        ("task", "Задача клиента"),
        ("product", "Продукт"),
        ("scope", "Комплектация и ограничения"),
        ("process", "Как предоставляется продукт"),
    ),
}
COMMON_PROPOSAL_FIELDS = (
    ("price_conditions", "Цена и условия оплаты"),
    ("timeframe", "Сроки из данных автора"),
    ("evidence", "Подтверждения"),
    ("risk_reversal", "Реальные условия снятия риска"),
    ("action", "Следующий шаг"),
    ("contact", "Контакт автора"),
)


def supplied(facts: Mapping[str, object], key: str) -> str:
    value = facts.get(key)
    if value is None or value == "":
        return PLACEHOLDER
    # No parsing, interpolation, enrichment or replacement of the supplied facts.
    return text(value, 4000)


def package_case(
    facts: Mapping[str, object], placement: str, template: dict[str, Any] | None = None
) -> str:
    if placement not in {"post", "page"}:
        raise ValueError("Выберите пост или блок страницы")
    if template is not None:
        if template["payload"]["tool"] != "case":
            raise ValueError("Неизвестный шаблон кейса")
        return (
            assemble(template["payload"]["formats"][placement], dict(facts))
            + "\n\nПеред публикацией получите согласие клиента. "
            "Не публикуйте персональные данные без согласия."
        )
    title = "Кейс — черновик поста" if placement == "post" else "Кейс — блок страницы"
    sections = [title, "Только введённые факты. Неизвестные данные: [впишите]."]
    for key, label in CASE_FIELDS:
        heading = label + ":" if placement == "post" else "## " + label
        sections.append(heading + "\n" + supplied(facts, key))
    return "\n\n".join(sections)


def commercial_proposal(
    facts: Mapping[str, object], kind: str, template: dict[str, Any] | None = None
) -> str:
    if kind not in PROPOSAL_FIELDS:
        raise ValueError("Выберите услугу, консультацию или продукт")
    if template is not None:
        if template["payload"]["tool"] != "proposal" or template["payload"]["model"] != kind:
            raise ValueError("Проверьте тип шаблона КП")
        return (
            assemble(template["payload"]["formats"]["text"], dict(facts))
            + "\n\nПроверьте состав, стоимость и условия перед отправкой."
        )
    sections = [
        "Коммерческое предложение — черновик",
        "Проверьте состав, цену и условия перед отправкой. "
        "Сроки, доказательства и гарантии автоматически не добавляются.",
    ]
    for key, label in (*PROPOSAL_FIELDS[kind], *COMMON_PROPOSAL_FIELDS):
        sections.append("## " + label + "\n" + supplied(facts, key))
    return "\n\n".join(sections)
