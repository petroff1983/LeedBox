"""Authored starter catalogs. Loaded explicitly into DB; never overwrites owner edits."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.content import CATALOGS
from leedbox.application.versions import record_version
from leedbox.domain.content import GOALS, validate_catalog
from leedbox.infrastructure.starter_ideas import EXPERTS as DEEP

UNIVERSAL = [
    ("Чек-лист первого шага", "checklist"),
    ("Карта типичных ошибок", "guide"),
    ("Шаблон плана на одну задачу", "template"),
    ("Таблица выбора решения", "table"),
    ("Короткий разбор одного примера", "video"),
    ("Чек-лист самопроверки", "checklist"),
    ("Памятка подготовки к консультации", "guide"),
    ("Шаблон личного брифа", "template"),
    ("Таблица вопросов перед покупкой", "table"),
    ("Демонстрация рабочего процесса", "video"),
    ("Чек-лист проверки подрядчика", "checklist"),
    ("Карта этапов работы", "guide"),
    ("Шаблон запроса обратной связи", "template"),
    ("Таблица исходных данных", "table"),
    ("Ответ на один частый вопрос", "video"),
    ("Чек-лист подготовки материалов", "checklist"),
    ("Памятка о границах результата", "guide"),
    ("Шаблон критериев приёмки", "template"),
    ("Таблица приоритетов", "table"),
    ("Пошаговый показ небольшой задачи", "video"),
]
MODELS = {
    "AIDA": (
        "Внимание → интерес → желание → действие",
        "Задача: {problem}. Продукт: «{product}». "
        "Для кого: «{audience}». Желаемый результат: «{desired_result}». Следующий шаг: {action}.",
    ),
    "PAS": (
        "Проблема → последствия → решение",
        "Проблема: «{problem}». Последствия: [впишите]. "
        "Решение: «{product}». Для аудитории: «{audience}». Подтверждения: {evidence}.",
    ),
    "4U": (
        "Польза, конкретика, отличие и реальный срок",
        "Результат: «{desired_result}». "
        "Продукт: «{product}». Отличие: {advantages}. Срок из ваших данных: {timeframe}.",
    ),
    "BAB": (
        "До → после → мост",
        "До: «{problem}». После: «{desired_result}». "
        "Мост — продукт: «{product}». Подтверждения: {evidence}. Условия: {price_conditions}.",
    ),
    "RESULT": (
        "Результат + срок + снятие риска без придуманной гарантии",
        "Желаемый результат: "
        "«{desired_result}». Срок: {timeframe}. Продукт: «{product}». "
        "Реальное условие снятия риска: {risk_reversal}.",
    ),
}
BLOCKS = [
    (
        "hero",
        "Первый экран",
        "Объяснить предложение",
        "Продукт: {product}. Аудитория: {audience}. Действие: {action}.",
        "Продукт: «{product}» — результат: «{desired_result}»",
    ),
    (
        "benefit",
        "Результат",
        "Показать ожидаемую пользу",
        "Желаемый результат: {desired_result}. Границы: [впишите].",
        "Желаемый результат: «{desired_result}»",
    ),
    (
        "process",
        "Как мы работаем",
        "Раскрыть процесс",
        "Этапы, состав и условия: [впишите].",
        "Этапы работы: [впишите]",
    ),
    (
        "proof",
        "Доказательства",
        "Дать проверяемые основания",
        "Только реальные подтверждения: {evidence}.",
        "Подтверждения: [впишите]",
    ),
    (
        "price",
        "Цена и условия",
        "Помочь принять решение",
        "Цена и условия: {price_conditions}.",
        "Условия: [впишите]",
    ),
    (
        "cta",
        "Следующий шаг",
        "Показать целевое действие",
        "Действие: {action}. Что будет дальше: [впишите].",
        "Следующий шаг: {action}",
    ),
    (
        "pain",
        "Исходная задача",
        "Уточнить проблему",
        "Проблема из брифа: {problem}.",
        "Задача: «{problem}»",
    ),
    (
        "fit",
        "Для кого и для кого нет",
        "Согласовать ожидания",
        "Кому подходит: {audience}. Ограничения: [впишите].",
        "Кому подходит продукт: «{product}»",
    ),
    (
        "objections",
        "Работа с возражениями",
        "Разобрать сомнения без обещаний",
        "Реальные вопросы клиентов и фактические ответы: [впишите].",
        "Как проверить до решения: [впишите]",
    ),
    (
        "faq",
        "Вопросы перед решением",
        "Уточнить детали",
        "Ответы на выбранные FAQ: [впишите].",
        "Что важно знать до покупки",
    ),
    (
        "program",
        "Программа по модулям",
        "Показать состав курса",
        "Модули, практика, ограничения: [впишите].",
        "Программа: [впишите]",
    ),
    (
        "author",
        "Автор",
        "Представить подтверждённый опыт",
        "Имя и реальный опыт: [впишите].",
        "Автор: [впишите]",
    ),
    (
        "first_step",
        "Процесс и первый шаг",
        "Дать основу вместо отсутствующих отзывов",
        "Как проходит работа: [впишите]. Бесплатный первый шаг — "
        "только если вы готовы его предоставить: [впишите].",
        "Как познакомиться с подходом: [впишите]",
    ),
]


def starter_entries() -> list[tuple[str, dict[str, Any]]]:
    entries = []
    for slug, (title, ideas) in DEEP.items():
        entries.append(
            (
                "niche",
                {
                    "slug": slug,
                    "title": title,
                    "payload": {"audiences": ["Начинающие", "Практикующие", "Заказчики"]},
                    "enabled": True,
                },
            )
        )
        for index, (name, format, structure) in enumerate(ideas):
            entries.append(
                (
                    "idea",
                    {
                        "slug": f"{slug}-{index + 1}",
                        "title": name,
                        "enabled": True,
                        "payload": {
                            "niche": slug,
                            "format": format,
                            "goals": sorted(GOALS),
                            "structure": structure.split(";"),
                            "fields": ["Ваши примеры", "Ваши исходные данные", "Контакт автора"],
                        },
                    },
                )
            )
    for index, (name, format) in enumerate(UNIVERSAL):
        entries.append(
            (
                "idea",
                {
                    "slug": f"universal-{index + 1}",
                    "title": name,
                    "enabled": True,
                    "payload": {
                        "niche": None,
                        "format": format,
                        "goals": sorted(GOALS),
                        "structure": [
                            "Задача и границы",
                            "Исходные данные автора",
                            "Шаги или критерии",
                            "Самопроверка и следующий шаг",
                        ],
                        "fields": ["Ваши факты", "Пример автора", "Контакт"],
                    },
                },
            )
        )
    for model, (explanation, body) in MODELS.items():
        entries.append(
            (
                "template",
                {
                    "slug": "offer-" + model.lower(),
                    "title": model,
                    "enabled": True,
                    "payload": {
                        "tool": "offer",
                        "model": model,
                        "explanation": explanation,
                        "formats": {
                            "page": "Заголовок: {product}\nПодзаголовок: " + body,
                            "post": body,
                            "ad": body,
                            "button": "Подробнее: {product}",
                        },
                    },
                },
            )
        )
    entries.append(
        (
            "template",
            {
                "slug": "landing-main",
                "title": "Структура лендинга",
                "enabled": True,
                "payload": {
                    "tool": "landing",
                    "blocks": [
                        dict(
                            zip(
                                ("key", "title", "purpose", "content", "headline"),
                                block,
                                strict=True,
                            )
                        )
                        for block in BLOCKS
                    ],
                    "faq": [
                        "Что входит и что не входит?",
                        "Как проходит работа?",
                        "Какие ограничения результата?",
                        "Каковы условия и следующий шаг?",
                    ],
                },
            },
        )
    )
    from leedbox.domain.offers import starter_templates
    from leedbox.infrastructure.commercial_seed import entries as commercial_entries
    from leedbox.infrastructure.funnel_seed import entries as funnel_entries

    return (
        entries
        + [("template", row) for row in starter_templates()]
        + commercial_entries()
        + funnel_entries()
    )


def seed(session: Session) -> int:
    from leedbox.application.versions import lock_content

    lock_content(session, "starter_catalog", "v1")
    count = 0
    for kind, values in starter_entries():
        model = CATALOGS[kind]
        if session.scalar(select(model.id).where(model.slug == values["slug"])) is not None:
            continue
        values = validate_catalog(kind, values)
        row = model(**values)
        session.add(row)
        session.flush()
        record_version(session, kind, str(row.id), None, values)
        count += 1
    return count


def main() -> None:
    from leedbox.infrastructure.config import Settings
    from leedbox.infrastructure.database import create_database_engine

    engine = create_database_engine(Settings())
    try:
        with Session(engine) as session, session.begin():
            print(f"Добавлено стартовых карточек: {seed(session)}. Существующие не изменены.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
