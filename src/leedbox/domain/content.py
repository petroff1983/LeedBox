"""Data-driven text assembly; arbitrary expressions and invented facts are not supported."""

import re
from string import Formatter
from typing import Any

from leedbox.domain.brief import BRIEF_FIELDS

FACTS = {
    "previous_attempts",
    "urgency",
    "advantage_clause",
    "proof_clause",
    "risk_clause",
    "urgency_clause",
    "attempts_clause",
    "time_clause",
    *BRIEF_FIELDS,
    "timeframe",
    "risk_reversal",
    "action",
    "client",
    "task",
    "baseline",
    "work",
    "result",
    "result_number",
    "testimonial",
    "constraints",
    "scope",
    "process",
    "conditions",
    "contact",
}
GOALS = {"subscribers", "requests", "warming"}
FORMATS = {"checklist", "guide", "template", "video", "table"}
PLACEHOLDER = "[впишите]"


def text(value: Any, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Текст: от 1 до {limit} символов")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("Управляющие символы запрещены")
    return value.strip()


def pattern(value: Any) -> str:
    value = text(value)
    try:
        for _, field, spec, conversion in Formatter().parse(value):
            if field is not None and (field not in FACTS or spec or conversion):
                raise ValueError("Разрешены только простые поля фактов: {product}, {audience}…")
    except ValueError:
        raise ValueError("Некорректные поля шаблона") from None
    return str(value)


def assemble(value: str, facts: dict[str, Any]) -> str:
    pattern(value)
    return value.format_map({key: str(facts.get(key) or PLACEHOLDER) for key in FACTS})


def validate_catalog(kind: str, values: dict[str, Any]) -> dict[str, Any]:
    if set(values) != {"slug", "title", "payload", "enabled"}:
        raise ValueError("Некорректные поля каталога")
    slug, title, payload = text(values["slug"], 80), text(values["title"], 100), values["payload"]
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", slug) or type(values["enabled"]) is not bool:
        raise ValueError("Код: латинские буквы, цифры, _ и -; публикация — переключателем")
    if not isinstance(payload, dict):
        raise ValueError("Содержимое должно быть объектом")
    if kind == "niche":
        if set(payload) != {"audiences"} or not isinstance(payload["audiences"], list):
            raise ValueError("Укажите список аудиторий")
        if not 1 <= len(payload["audiences"]) <= 5:
            raise ValueError("От 1 до 5 вариантов аудитории")
        for value in payload["audiences"]:
            text(value, 200)
    elif kind == "idea":
        if set(payload) != {"niche", "format", "goals", "structure", "fields"}:
            raise ValueError("Поля идеи: niche, format, goals, structure, fields")
        text(title, 60)
        if payload["niche"] is not None:
            text(payload["niche"], 80)
        if payload["format"] not in FORMATS or not isinstance(payload["goals"], list):
            raise ValueError("Выберите формат и цели идеи")
        if not payload["goals"] or any(value not in GOALS for value in payload["goals"]):
            raise ValueError("Некорректная цель")
        for key, low, high in (("structure", 4, 6), ("fields", 1, 10)):
            items = payload[key]
            if not isinstance(items, list) or not low <= len(items) <= high:
                raise ValueError(f"{key}: от {low} до {high} пунктов")
            for item in items:
                text(item, 400)
    elif kind == "objection":
        if set(payload) != {"reason", "answers", "question"}:
            raise ValueError("Поля возражения: причина, ответы, вопрос")
        text(payload["reason"], 1000)
        text(payload["question"], 500)
        if not isinstance(payload["answers"], list) or not 2 <= len(payload["answers"]) <= 3:
            raise ValueError("Нужно 2–3 ответа")
        for answer in payload["answers"]:
            text(answer, 1000)
    elif kind == "topic":
        if (
            set(payload) != {"stage", "condition"}
            or payload["stage"] not in {"warming", "sales", "closing"}
            or payload["condition"]
            not in {"always", "bonuses", "price", "promo", "checklist", "access"}
        ):
            raise ValueError("Проверьте этап и условие темы")
    elif kind == "category":
        if set(payload) != {"description"}:
            raise ValueError("Укажите описание категории")
        text(payload["description"], 1000)
    elif kind == "recommendation":
        allowed = {
            "ideas",
            "offer",
            "landing",
            "case",
            "proposal",
            "sequence",
            "funnel",
            "campaign",
            "breakeven",
            "budget_cpl",
            "budget_cpc",
            "utm",
            "expert",
            "material",
            "invite",
        }
        if (
            set(payload) != {"tools", "partner_ids"}
            or not isinstance(payload["tools"], list)
            or not payload["tools"]
            or any(tool not in allowed for tool in payload["tools"])
        ):
            raise ValueError("Укажите инструменты рекомендации")
        ids = payload["partner_ids"]
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 5
            or any(type(id) is not int or not 0 < id <= 2**63 - 1 for id in ids)
        ):
            raise ValueError("Укажите от одного до пяти ID партнёров")
    elif kind == "article":
        from leedbox.domain.materials import http_url

        if set(payload) != {"category", "description", "url", "partner_ids"}:
            raise ValueError("Поля статьи: категория, описание, ссылка, партнёры")
        text(payload["category"], 80)
        text(payload["description"], 1000)
        http_url(payload["url"])
        if (
            not isinstance(payload["partner_ids"], list)
            or len(payload["partner_ids"]) > 5
            or any(type(id) is not int or not 0 < id <= 2**63 - 1 for id in payload["partner_ids"])
        ):
            raise ValueError("Укажите не более пяти ID партнёров")
    elif kind == "template":
        if payload.get("tool") == "funnel_example":
            from leedbox.domain.funnel import FIELDS

            if (
                set(payload) != {"tool", "model", "facts"}
                or payload["model"] != "main"
                or not isinstance(payload["facts"], dict)
                or set(payload["facts"]) != set(FIELDS)
            ):
                raise ValueError("Пример: нужны все 12 полей фактов")
            for value in payload["facts"].values():
                text(value, 1000)
            return {"slug": slug, "title": title, "payload": payload, "enabled": values["enabled"]}
        if payload.get("tool") == "funnel":
            if set(payload) != {"tool", "model", "rules"} or payload["model"] != "main":
                raise ValueError("Поля промта: tool, model, rules")
            text(payload["rules"], 4000)
            return {"slug": slug, "title": title, "payload": payload, "enabled": values["enabled"]}
        if payload.get("tool") in {"offer", "case", "proposal"}:
            if set(payload) != {"tool", "model", "explanation", "formats"}:
                raise ValueError("Поля оффера: tool, model, explanation, formats")
            allowed_models = {
                "offer": {"AIDA", "PAS", "4U", "BAB", "RESULT"},
                "case": {"main"},
                "proposal": {"service", "consultation", "product"},
            }
            if payload["model"] not in allowed_models[payload["tool"]]:
                raise ValueError("Неизвестная модель оффера")
            text(payload["explanation"], 500)
            formats = {
                "offer": {"page", "post", "ad", "button"},
                "case": {"post", "page"},
                "proposal": {"text"},
            }
            if (
                not isinstance(payload["formats"], dict)
                or set(payload["formats"]) != formats[payload["tool"]]
            ):
                raise ValueError("Проверьте форматы шаблона")
            for value in payload["formats"].values():
                pattern(value)
        elif payload.get("tool") == "landing":
            blocks = payload.get("blocks")
            if set(payload) != {"tool", "blocks", "faq"} or not isinstance(blocks, list):
                raise ValueError("Поля структуры: tool, blocks, faq")
            required = {
                "hero",
                "benefit",
                "process",
                "proof",
                "price",
                "cta",
                "pain",
                "fit",
                "objections",
                "faq",
                "program",
                "author",
                "first_step",
            }
            if {block.get("key") for block in blocks if isinstance(block, dict)} != required:
                raise ValueError("Нужны все 13 именованных блоков структуры")
            if len(blocks) != 13:
                raise ValueError("Блоки не должны повторяться")
            for block in blocks:
                if set(block) != {"key", "title", "purpose", "content", "headline"}:
                    raise ValueError("Поля блока: key, title, purpose, content, headline")
                for key in ("title", "purpose", "content", "headline"):
                    pattern(block[key])
            if not isinstance(payload["faq"], list) or not 3 <= len(payload["faq"]) <= 10:
                raise ValueError("FAQ: от 3 до 10 вопросов")
            for question in payload["faq"]:
                text(question, 400)
        else:
            raise ValueError("Неизвестный инструмент шаблона")
    else:
        raise ValueError("Неизвестный каталог")
    return {"slug": slug, "title": title, "payload": payload, "enabled": values["enabled"]}


def offer(
    facts: dict[str, Any], templates: list[dict[str, Any]], placement: str, limits: dict[str, int]
) -> str:
    templates = [row for row in templates if not row.get("slug", "").startswith("offer-v2-")]
    if placement not in {"page", "post", "ad", "button"} or not 3 <= len(templates) <= 5:
        raise ValueError("Нужно от 3 до 5 опубликованных моделей и корректная площадка")
    result = ["Оффер — шаблонный черновик. Только введённые факты; поля [впишите] заполните сами."]
    for entry in templates:
        data = entry["payload"]
        rendered = assemble(data["formats"][placement], facts)
        result += [f"\n{data['model']} · {entry['title']}\n{data['explanation']}\n{rendered}"]
        if placement in limits:
            maximum = limits[placement]
            result += [
                f"Длина: {len(rendered)} / {maximum}. "
                + (
                    "Нужно сократить вручную; факты автоматически не обрезаются."
                    if len(rendered) > maximum
                    else "В пределах настроенного лимита."
                )
            ]
    if placement == "ad":
        result += [
            "Лимит задаётся владельцем для выбранного формата. Модерация не гарантируется. "
            "Интернет-реклама требует маркировки; за неё отвечает рекламодатель."
        ]
    return "\n".join(result)


def landing(facts: dict[str, Any], template: dict[str, Any]) -> str:
    kind, price, temperature = (
        facts.get("page_type"),
        facts.get("price_band"),
        facts.get("temperature"),
    )
    if kind not in {"service", "consultation", "course", "webinar", "product", "channel"}:
        raise ValueError("Неизвестный тип страницы")
    if price not in {"low", "medium", "high"} or temperature not in {"cold", "warm"}:
        raise ValueError("Выберите цену и температуру аудитории")
    keys = ["hero", "benefit", "process", "proof", "price", "cta"]
    if kind != "webinar":
        if temperature == "cold":
            keys[1:1] = ["pain", "fit"]
        if price == "high":
            keys[-1:-1] = ["objections", "faq"]
        if kind == "course":
            keys[3:3] = ["program", "author"]
    if temperature == "warm":
        keys = [key for key in keys if key not in {"pain", "fit", "objections", "faq"}]
        if kind == "course":
            keys.remove("benefit")
            keys.remove("process")
    if not facts.get("evidence") or facts.get("evidence") == "none":
        keys[keys.index("proof")] = "first_step"
    blocks = {block["key"]: block for block in template["payload"]["blocks"]}
    result = [f"ТЗ на лендинг: {len(keys)} экранов. Это структура, не готовая страница."]
    for index, key in enumerate(keys, 1):
        block = blocks[key]
        result += [
            f"\n## {index}. {block['title']}\nЦель: {block['purpose']}\n"
            f"Разместить: {assemble(block['content'], facts)}\n"
            f"Заголовок / пример: {assemble(block['headline'], facts)}"
        ]
    result += [
        "\nFAQ — выберите актуальные вопросы; ответы: [впишите].",
        *template["payload"]["faq"],
    ]
    missing = [
        key
        for key in (
            "product",
            "audience",
            "desired_result",
            "price_conditions",
            "evidence",
            "action",
        )
        if not facts.get(key) or facts.get(key) == "none"
    ]
    result += ["\nНезаполненные факты: " + (", ".join(missing) or "нет")]
    return "\n".join(result)
