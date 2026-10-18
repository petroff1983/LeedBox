"""Owner-editable fact-only case and proposal starter templates."""

from typing import Any


def entries() -> list[tuple[str, dict[str, Any]]]:
    case = (
        "Задача клиента: {task}\nИсходные данные: {baseline}\n"
        "Что сделали: {work}\nРезультат — введённое число и единица: {result_number}\n"
        "Срок результата: {timeframe}\nОтзыв клиента: {testimonial}\nПредложение: {product}"
    )
    values = [
        (
            "template",
            {
                "slug": "case-main",
                "title": "Упаковка кейса",
                "enabled": True,
                "payload": {
                    "tool": "case",
                    "model": "main",
                    "explanation": "Факты автора, без обогащения",
                    "formats": {
                        "post": "Кейс — пост\n\n" + case,
                        "page": "Кейс — блок страницы\n\n" + case,
                    },
                },
            },
        )
    ]
    for kind, label, scope in (
        ("service", "Услуга", "Состав работ"),
        ("consultation", "Консультация", "Формат, подготовка и состав консультации"),
        ("product", "Продукт", "Комплектация и ограничения"),
    ):
        values.append(
            (
                "template",
                {
                    "slug": "proposal-" + kind,
                    "title": "КП — " + label,
                    "enabled": True,
                    "payload": {
                        "tool": "proposal",
                        "model": kind,
                        "explanation": "Проблема, решение и условия из данных автора",
                        "formats": {
                            "text": "Коммерческое предложение\n\nПроблема клиента: {problem}\n"
                            "Решение: {product}\n" + scope + ": {scope}\nЭтапы и сроки: {process}\n"
                            "Стоимость: {price_conditions}\nУсловия: {conditions}\n"
                            "Следующий шаг: {action}"
                        },
                    },
                },
            )
        )
    return values
