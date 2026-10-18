"""Owner editor with protected input slots and the existing version/audit machinery."""

from typing import Any

from flask import abort, flash, g, redirect, request
from flask_admin import expose  # type: ignore[import-untyped]
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application.settings import set_setting, setting
from leedbox.application.versions import fingerprint
from leedbox.domain import ai_prompts
from leedbox.web.admin import OwnerView, confirmed, input_field


class PromptAgentsView(OwnerView):
    @expose("/")
    def index(self) -> str:
        agents: list[dict[str, Any]] = [
            {
                "name": "Дзен",
                "description": "Пять заголовков и промпт для статей",
                "switch": "ai_dzen_enabled",
                "prompts": [
                    ("Заголовки", "ai_dzen_prompt"),
                    ("Промпт для статей", "ai_dzen_article_template"),
                ],
            },
            {
                "name": "Картинки",
                "description": "Статья → промпты изображений",
                "switch": "ai_images_enabled",
                "prompts": [("Инструкция", "ai_images_prompt")],
            },
            {
                "name": "Контент-аналитик",
                "description": (
                    "Паттерны и посты для Telegram; ссылки и метрики Telegram-конкурента"
                ),
                "switch": "ai_content_enabled",
                "prompts": [
                    ("Паттерны и план", "ai_content_plan_prompt"),
                    ("Черновики", "ai_content_draft_prompt"),
                    ("Редактура", "ai_content_edit_prompt"),
                ],
            },
            {
                "name": "Лид-магниты",
                "description": "5 тем и описаний → одобрение или критик и доработка",
                "switch": "ai_lead_magnet_enabled",
                "prompts": [
                    ("Создание", "ai_lead_magnet_generate_prompt"),
                    ("Критик", "ai_lead_magnet_critic_prompt"),
                    ("Доработка", "ai_lead_magnet_revise_prompt"),
                ],
            },
            {
                "name": "YouTube",
                "description": "Публичная статистика видео; без ИИ и промпта",
                "switch": "section_youtube_visible",
                "prompts": [],
            },
        ]
        with Session(self.engine) as db:
            for agent in agents:
                agent["enabled"] = setting(db, str(agent["switch"]))
        return self.page(
            "prompt-agents.html",
            title="Промпты и агенты",
            agents=agents,
            prompt_tools=list(ai_prompts.LABELS.items()),
        )


class PromptView(OwnerView):
    @expose("/")
    def index(self) -> str:
        with Session(self.engine) as db:
            rows = [
                [
                    label,
                    "Без переменных" if tool == "sequence" else "{{inputs}} — ответы опроса",
                    fingerprint(setting(db, "prompt_template_" + tool))[:12],
                    {"label": "Редактировать", "url": self.url + "/edit?tool=" + tool},
                ]
                for tool, label in ai_prompts.LABELS.items()
            ]
        return self.page(
            "table.html",
            title="Шаблоны промптов",
            columns=["Инструмент", "Переменные", "Версия", "Действие"],
            rows=rows,
            links=[
                ("Промпты и агенты", self.config.admin_path + "/ai/"),
                ("История и откат", self.config.admin_path + "/history/"),
            ],
        )

    @expose("/edit", methods=["GET", "POST"])
    def edit(self) -> str | Response:
        tool = request.args.get("tool", "")
        if tool not in ai_prompts.FIELDS:
            abort(404)
        key = "prompt_template_" + tool
        with Session(self.engine) as db:
            original = setting(db, key)
        body = request.form.get("body", original)
        expected = (
            request.form.get("expected", "") if request.method == "POST" else fingerprint(original)
        )
        error, preview = "", ""
        if request.method == "POST":
            try:
                confirmed()
                if request.form.get("action") not in {"preview", "save"}:
                    raise ValueError("Выберите предпросмотр или сохранение")
                ai_prompts.validate_template(tool, body)
                if request.form.get("action") == "preview":
                    preview = ai_prompts.build_prompt(
                        tool,
                        {
                            field: "Демонстрационные данные: " + ai_prompts.NAMES[field]
                            for field in ai_prompts.FIELDS[tool]
                        },
                        body,
                    )
                else:
                    with Session(self.engine) as db, db.begin():
                        set_setting(db, self.config, g.owner_id, key, body, expected)
                    flash("Шаблон сохранён. Новые опросы используют эту версию.", "success")
                    return redirect(self.url + "/")
            except ValueError as problem:
                error = str(problem)
        return self.editor(
            "Промпт: " + ai_prompts.LABELS[tool],
            [{**input_field("body", "Текст шаблона", body, "textarea"), "rows": 24}],
            {"expected": expected},
            error=error,
            preview=preview,
            previewable=True,
            help_text=(
                "Воронка выдаётся как есть: переменные не нужны."
                if tool == "sequence"
                else "Сохраните ровно один {{inputs}} — сюда бот подставляет все ответы в JSON. "
                "Названия полей внутри блока менять не нужно. Другие маркеры и выражения запрещены."
            )
            + " Изменения действуют без перезапуска. "
            "Начатые опросы и история сохраняют свою версию. "
            "Предпросмотр использует только демонстрационные данные.",
        )
