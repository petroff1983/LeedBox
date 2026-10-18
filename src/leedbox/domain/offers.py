"""Fact-only offer assembly and the progressive questionnaire (no AI or scoring)."""

from typing import Any

from leedbox.domain.content import FACTS, PLACEHOLDER, pattern

FIELDS = (
    "product",
    "audience",
    "problem",
    "previous_attempts",
    "desired_result",
    "advantages",
    "evidence",
    "timeframe",
    "risk_reversal",
    "urgency",
    "action",
)
REQUIRED = {"product", "audience", "problem", "desired_result", "advantages", "action"}
PROMPTS = {
    "product": "Что именно вы продаёте?\n\nНапример: курс Excel для бухгалтеров "
    "или ремонт квартир под ключ в Москве.",
    "audience": "Кому нужен ваш продукт?\n\nНазовите конкретных клиентов, не «всех»: "
    "например, владельцы салонов красоты с оборотом от 500 000 ₽.",
    "problem": "Какую главную проблему клиента вы решаете?\n\n"
    "Выберите подходящий вариант или напишите свою ситуацию.",
    "previous_attempts": "Что клиент уже пробовал и почему это не помогло?\n\n"
    "Например: прошёл курс, но не смог применить знания без обратной связи. "
    "Если не знаете — пропустите.",
    "desired_result": "Какой конкретный результат получит клиент?\n\n"
    "Опишите измеримую пользу: например, сократит подготовку отчёта с 3 часов до 30 минут. "
    "Только подтверждённые цифры; без цифр тоже можно.",
    "advantages": "Почему стоит выбрать именно вас?\n\nНазовите реальное отличие: "
    "собственная методика, опыт в этой нише, поддержка после работы. "
    "Не просто «качество» и «индивидуальный подход».",
    "evidence": "Чем подтверждается ваш результат?\n\nРеальный кейс, отзыв, сертификат "
    "или проверяемые цифры. Не указывайте личные данные клиентов. "
    "Если подтверждений пока нет — пропустите.",
    "timeframe": "За какой реальный срок клиент получит результат?\n\nНапример: за 4 недели. "
    "Если срок зависит от клиента или неизвестен — пропустите, обещание не добавим.",
    "risk_reversal": "Как вы снижаете риск для клиента?\n\n"
    "Выберите условие и уточните его или напишите своё. Если гарантии нет — пропустите.",
    "urgency": "Есть ли реальная причина действовать сейчас?\n\n"
    "Только действующий срок, бонус или подтверждённое ограничение. "
    "Если нет — выберите «Без срочности».",
    "action": "Что клиент должен сделать следующим шагом?\n\nВыберите одно действие "
    "или напишите своё. Например: оставить заявку на диагностику.",
}
CHOICES: dict[str, dict[str, tuple[str, str | None]]] = {
    "problem": {
        "time": ("😩 Не хватает времени", "Не хватает времени на рутинные задачи"),
        "money": ("💸 Теряют деньги", "Теряют деньги на текущем решении"),
        "result": ("📉 Нет результата", "Текущий способ не даёт нужного результата"),
    },
    "risk_reversal": {
        "refund": ("✅ Возврат денег — условия", None),
        "contract": ("📝 Договор и KPI — условия", None),
        "trial": ("🎁 Пробный шаг — условия", None),
    },
    "urgency": {
        "deadline": ("⏳ Реальный срок", None),
        "bonus": ("🎁 Бонус — какой и до когда", None),
        "places": ("👥 Ограничение мест", None),
        "none": ("Без срочности", ""),
    },
    "action": {
        "request": ("📩 Оставить заявку", "Оставьте заявку"),
        "book": ("📅 Записаться на диагностику", "Запишитесь на диагностику"),
        "buy": ("🛒 Купить", "Купите продукт"),
    },
}
DETAILS = {
    "refund": "Напишите реальные условия возврата: в какой срок и при каких условиях "
    "возвращаете деньги. Если такого условия нет — пропустите.",
    "contract": "Какие обязательства и KPI действительно указаны в договоре? "
    "Не обещайте результат, который договор не гарантирует.",
    "trial": "Что именно можно попробовать бесплатно и какие ограничения действуют?",
    "deadline": "До какой конкретной даты действует предложение? Укажите дату и условия. "
    "Срок не будем придумывать или автоматически сдвигать.",
    "bonus": "Какой бонус получает клиент, за какое действие и до какой даты?",
    "places": "Сколько мест действительно осталось и для какого периода? "
    "Если ограничение не подтверждено — пропустите.",
}
MODELS = (
    (
        "4U",
        "Рациональный · 4U",
        "Польза → конкретика → отличие → реальная срочность",
        "{audience}: {desired_result}{time_clause}. «{product}»."
        "{advantage_clause}{proof_clause}{risk_clause}{urgency_clause} {action}.",
    ),
    (
        "PAS",
        "Через проблему · PAS",
        "Проблема → неудачные попытки → решение",
        "Знакома ситуация: «{problem}»?{attempts_clause} «{product}»: "
        "{desired_result}{time_clause}. Для кого это решение: {audience}."
        "{advantage_clause}{proof_clause}"
        "{risk_clause}{urgency_clause} {action}.",
    ),
    (
        "AIDA",
        "Короткий · AIDA для рекламы",
        "Польза → предложение → отличие → действие",
        "Вы — {audience}? «{product}»: {desired_result}."
        "{advantage_clause}{risk_clause}{urgency_clause} {action}.",
    ),
)


def starter_templates() -> list[dict[str, Any]]:
    return [
        {
            "slug": "offer-v2-" + model.lower(),
            "title": title,
            "enabled": True,
            "payload": {
                "tool": "offer",
                "model": model,
                "explanation": explanation,
                "formats": {place: body for place in ("page", "post", "ad", "button")},
            },
        }
        for model, title, explanation, body in MODELS
    ]


def variants(values: dict[str, Any], templates: list[dict[str, Any]]) -> list[str]:
    facts = dict(values)
    for key in REQUIRED:
        facts[key] = str(values.get(key) or PLACEHOLDER).strip().rstrip(".! ")
    for key, name in (
        ("advantages", "Отличие"),
        ("evidence", "Подтверждение"),
        ("risk_reversal", "Условия снижения риска"),
        ("urgency", "Условия предложения"),
        ("previous_attempts", "Уже пробовали"),
    ):
        value = str(values.get(key) or "").strip().rstrip(".! ")
        clause = {
            "advantages": "advantage_clause",
            "evidence": "proof_clause",
            "risk_reversal": "risk_clause",
            "urgency": "urgency_clause",
            "previous_attempts": "attempts_clause",
        }[key]
        facts[clause] = (
            f" Наше отличие — {value}."
            if key == "advantages" and value
            else f" {name}: {value}."
            if value
            else ""
        )
    facts["time_clause"] = (
        " (срок: " + str(values["timeframe"]).strip() + ")" if values.get("timeframe") else ""
    )
    parts = []
    for model, _, _, _ in MODELS:
        entry = next((row for row in templates if row["payload"]["model"] == model), None)
        if entry is None:
            raise ValueError("Не хватает опубликованных шаблонов 4U, PAS и AIDA")
        template = pattern(entry["payload"]["formats"]["post"])
        body = template.format_map(
            {
                key: str(facts[key]) if key in facts and facts[key] is not None else PLACEHOLDER
                for key in FACTS
            }
        )
        if values.get("offer_tone") == "direct":
            body = "Ваша задача: «" + facts["problem"] + "». " + body
        parts.append(body)
    return parts


def report(values: dict[str, Any], templates: list[dict[str, Any]]) -> str:
    parts = variants(values, templates)
    unique = f"{values.get('audience') or PLACEHOLDER}: «{values.get('product') or PLACEHOLDER}» — "
    unique += (
        f"{values.get('desired_result') or PLACEHOLDER}. "
        f"Отличие: {values.get('advantages') or PLACEHOLDER}."
    )
    return (
        "🎯 Оффер и УТП готовы\n\nВаше УТП\n"
        + unique
        + "\n\n"
        + "\n\n".join(f"{i + 1}. {MODELS[i][1]}\n{body}" for i, body in enumerate(parts))
        + "\n\nЧерновики собраны из ваших ответов, без ИИ. Проверьте факты перед публикацией."
    )
