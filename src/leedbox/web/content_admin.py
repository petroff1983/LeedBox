"""Structured catalog editors and operational views inside the incumbent admin shell."""

import csv
import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from flask import Response, abort, flash, g, redirect, request
from flask_admin import expose  # type: ignore[import-untyped]
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response as WerkzeugResponse

from leedbox.application.admin_content import save_project_metadata
from leedbox.application.content import CATALOGS, save_catalog, snapshot
from leedbox.application.services import owner_required
from leedbox.application.versions import fingerprint
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.content import FORMATS, GOALS
from leedbox.domain.links import csv_export
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.content_seed import BLOCKS
from leedbox.infrastructure.models import (
    AuditLog,
    Niche,
    NicheSuggestion,
    Partner,
    ProductEvent,
    Project,
    RecommendationEvent,
    User,
)
from leedbox.web.admin import OwnerView, confirmed, input_field, numeric

TEMPLATE_MODELS = {
    "offer": [(key, key) for key in ("AIDA", "PAS", "4U", "BAB", "RESULT")],
    "case": [("main", "Пост и блок страницы")],
    "proposal": [("service", "Услуга"), ("consultation", "Консультация"), ("product", "Продукт")],
}
TEMPLATE_FORMATS = {
    "offer": [("page", "Страница"), ("post", "Пост"), ("ad", "Объявление"), ("button", "Кнопка")],
    "case": [("post", "Пост"), ("page", "Блок страницы")],
    "proposal": [("text", "Коммерческое предложение")],
}


class ContentView(OwnerView):
    def __init__(self, engine: Engine, config: Settings, kind: str, name: str) -> None:
        self.kind = kind
        super().__init__(engine, config, name, "owner_content_" + kind, "/content-" + kind)
        self.category = "Контент"

    @expose("/")
    def index(self) -> str:
        model = CATALOGS[self.kind]
        term = request.args.get("q", "")[:100]
        page = numeric(request.args.get("page", "0"), 10000)
        query = select(model)
        if term:
            query = query.where(
                model.title.icontains(term, autoescape=True)
                | model.slug.icontains(term, autoescape=True)
            )
        if request.args.get("published") in {"0", "1"}:
            query = query.where(model.enabled.is_(request.args["published"] == "1"))
        with Session(self.engine) as db:
            values = list(db.scalars(query.order_by(model.id).offset(page * 20).limit(21)))
            rows = [
                [
                    row.slug,
                    row.title,
                    "Опубликовано" if row.enabled else "Черновик",
                    str(row.revision),
                    {"label": "Изменить", "url": self.url + "/edit?id=" + str(row.id)},
                ]
                for row in values[:20]
            ]
        return self.page(
            "table.html",
            title=self.name,
            columns=["Код", "Название", "Статус", "Версия", "Действие"],
            rows=rows,
            page=page,
            more=len(values) > 20,
            search=True,
            published_filter=True,
            create_url=self.url + "/edit",
            links=[("Экспорт CSV", self.url + "/csv"), ("Импорт CSV", self.url + "/import")]
            + (
                [
                    (label, self.url + "/edit?tool=" + tool)
                    for tool, label in (
                        ("landing", "Структура лендинга"),
                        ("case", "Шаблон кейса"),
                        ("proposal", "Шаблон КП"),
                        ("funnel", "Промт воронки"),
                        ("funnel_example", "Пример воронки"),
                    )
                ]
                if self.kind == "template"
                else []
            ),
            empty="Ничего не найдено. Сбросьте условия или создайте карточку.",
        )

    def fields(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        payload = values["payload"]
        items = [
            input_field(
                "slug", "Постоянный код (латиница; не меняется после создания)", values["slug"]
            ),
            input_field("title", "Название", values["title"]),
            input_field("enabled", "Опубликовано", values["enabled"], "checkbox"),
        ]
        if self.kind == "niche":
            items.append(
                input_field(
                    "audiences",
                    "Варианты аудитории — каждый с новой строки",
                    "\n".join(payload.get("audiences", [])),
                    "textarea",
                )
            )
        elif self.kind == "idea":
            with Session(self.engine) as db:
                choices = [("", "Универсальная идея")] + [
                    (row.slug, row.title) for row in db.scalars(select(Niche).order_by(Niche.id))
                ]
            items += [
                input_field("niche", "Ниша", payload.get("niche") or "", "select", choices=choices),
                input_field(
                    "format",
                    "Формат",
                    payload.get("format", "checklist"),
                    "select",
                    choices=[
                        (
                            key,
                            {
                                "checklist": "Чек-лист",
                                "guide": "Гайд",
                                "template": "Шаблон",
                                "video": "Видео",
                                "table": "Таблица",
                            }[key],
                        )
                        for key in sorted(FORMATS)
                    ],
                ),
            ]
            for goal in sorted(GOALS):
                items.append(
                    input_field(
                        "goal_" + goal,
                        {
                            "requests": "Для заявок",
                            "subscribers": "Для подписчиков",
                            "warming": "Для прогрева",
                        }[goal],
                        goal in payload.get("goals", []),
                        "checkbox",
                    )
                )
            for key, label in (
                ("structure", "Структура: 4–6 пунктов"),
                ("fields", "Что заполняет автор: 1–10 полей"),
            ):
                items.append(
                    input_field(
                        key,
                        label + " — каждый с новой строки",
                        "\n".join(payload.get(key, [])),
                        "textarea",
                    )
                )
        elif self.kind in {"objection", "category", "article", "topic", "recommendation"}:
            definitions = {
                "objection": [
                    ("reason", "Возможная причина"),
                    ("answers", "Ответы: 2–3, каждый с новой строки"),
                    ("question", "Уточняющий вопрос"),
                ],
                "category": [("description", "Описание")],
                "article": [
                    ("category", "Код категории"),
                    ("description", "Описание"),
                    ("url", "Ссылка HTTP / HTTPS"),
                    ("partner_ids", "ID партнёров — каждый с новой строки (необязательно)"),
                ],
                "topic": [],
                "recommendation": [
                    ("tools", "Коды инструментов — каждый с новой строки"),
                    ("partner_ids", "ID партнёров — каждый с новой строки"),
                ],
            }
            for key, label in definitions[self.kind]:
                value = payload.get(key, "")
                if isinstance(value, list):
                    value = "\n".join(str(item) for item in value)
                items.append(input_field(key, label, value, "textarea"))
            if self.kind == "topic":
                for key, label, choices in (
                    (
                        "stage",
                        "Этап",
                        [
                            ("warming", "Прогрев"),
                            ("sales", "Продажа"),
                            ("closing", "Дожим без давления"),
                        ],
                    ),
                    (
                        "condition",
                        "Показывать при наличии факта",
                        [
                            ("always", "Всегда"),
                            ("bonuses", "Бонус"),
                            ("price", "Цена"),
                            ("promo", "Акция"),
                            ("checklist", "Бонусный чек-лист"),
                            ("access", "Условия доступа"),
                        ],
                    ),
                ):
                    items.append(
                        input_field(
                            key, label, payload.get(key, choices[0][0]), "select", choices=choices
                        )
                    )
        elif payload.get("tool") == "funnel_example":
            from leedbox.domain.funnel import LABELS

            items += [
                input_field(
                    "example_" + key, label, payload.get("facts", {}).get(key, ""), "textarea"
                )
                for key, label in LABELS.items()
            ]
        elif payload.get("tool") == "funnel":
            items.append(
                input_field(
                    "rules", "Дополнительные правила промта", payload.get("rules", ""), "textarea"
                )
            )
        elif payload.get("tool") in TEMPLATE_MODELS:
            tool = payload["tool"]
            items += [
                input_field(
                    "model",
                    "Модель",
                    payload.get("model", TEMPLATE_MODELS[tool][0][0]),
                    "select",
                    choices=TEMPLATE_MODELS[tool],
                ),
                input_field(
                    "explanation", "Пояснение модели", payload.get("explanation", ""), "textarea"
                ),
            ]
            for key, label in TEMPLATE_FORMATS[tool]:
                items.append(
                    input_field(
                        "format_" + key, label, payload.get("formats", {}).get(key, ""), "textarea"
                    )
                )
        else:
            blocks = {row["key"]: row for row in payload.get("blocks", [])}
            for key, title, *_ in BLOCKS:
                for field, label in (
                    ("title", "Название"),
                    ("purpose", "Цель"),
                    ("content", "Содержание"),
                    ("headline", "Формула заголовка"),
                ):
                    items.append(
                        input_field(
                            f"block_{key}_{field}",
                            f"{title} · {label}",
                            blocks.get(key, {}).get(field, ""),
                            "textarea",
                        )
                    )
            items.append(
                input_field(
                    "faq",
                    "FAQ — каждый вопрос с новой строки",
                    "\n".join(payload.get("faq", [])),
                    "textarea",
                )
            )
        return items

    def posted(self, tool: str) -> dict[str, Any]:
        def lines(key: str) -> list[str]:
            return [line.strip() for line in request.form.get(key, "").splitlines() if line.strip()]

        if self.kind == "niche":
            payload: dict[str, Any] = {"audiences": lines("audiences")}
        elif self.kind == "idea":
            payload = {
                "niche": request.form.get("niche") or None,
                "format": request.form.get("format", ""),
                "goals": [
                    goal for goal in sorted(GOALS) if request.form.get("goal_" + goal) == "1"
                ],
                "structure": lines("structure"),
                "fields": lines("fields"),
            }
        elif self.kind == "objection":
            payload = {
                "reason": request.form.get("reason", ""),
                "answers": lines("answers"),
                "question": request.form.get("question", ""),
            }
        elif self.kind == "topic":
            payload = {key: request.form.get(key, "") for key in ("stage", "condition")}
        elif self.kind == "category":
            payload = {"description": request.form.get("description", "")}
        elif self.kind in {"article", "recommendation"}:
            payload = (
                {key: request.form.get(key, "") for key in ("category", "description", "url")}
                if self.kind == "article"
                else {"tools": lines("tools")}
            )
            raw_ids = lines("partner_ids")
            payload["partner_ids"] = [
                int(value) if value.isdecimal() and len(value) <= 19 else value for value in raw_ids
            ]
        elif tool == "funnel_example":
            from leedbox.domain.funnel import FIELDS

            payload = {
                "tool": tool,
                "model": "main",
                "facts": {key: request.form.get("example_" + key, "") for key in FIELDS},
            }
        elif tool == "funnel":
            payload = {"tool": "funnel", "model": "main", "rules": request.form.get("rules", "")}
        elif tool in TEMPLATE_MODELS:
            payload = {
                "tool": tool,
                "model": request.form.get("model", ""),
                "explanation": request.form.get("explanation", ""),
                "formats": {
                    key: request.form.get("format_" + key, "") for key, _ in TEMPLATE_FORMATS[tool]
                },
            }
        else:
            payload = {
                "tool": "landing",
                "blocks": [
                    {
                        "key": key,
                        **{
                            field: request.form.get(f"block_{key}_{field}", "")
                            for field in ("title", "purpose", "content", "headline")
                        },
                    }
                    for key, *_ in BLOCKS
                ],
                "faq": lines("faq"),
            }
        return {
            "slug": request.form.get("slug", ""),
            "title": request.form.get("title", ""),
            "payload": payload,
            "enabled": request.form.get("enabled") == "1",
        }

    @expose("/edit", methods=["GET", "POST"])
    def edit(self) -> str | WerkzeugResponse:
        values: dict[str, Any]
        id = numeric(request.args["id"]) if request.args.get("id") else None
        with Session(self.engine) as db:
            row = db.get(CATALOGS[self.kind], id) if id else None
            if id and row is None:
                abort(404)
            values = (
                {key: snapshot(row)[key] for key in ("slug", "title", "payload", "enabled")}
                if row
                else {
                    "slug": "",
                    "title": "",
                    "enabled": False,
                    "payload": {
                        "tool": request.args.get("tool")
                        if request.args.get("tool")
                        in {"landing", "case", "proposal", "funnel", "funnel_example"}
                        else "offer"
                    },
                }
            )
            revision = row.revision if row else 0
        tool = str(values["payload"].get("tool", ""))
        expected = request.form.get("expected", "") if request.method == "POST" else str(revision)
        action_key = (
            request.form.get("action_key", "")
            if request.method == "POST"
            else secrets.token_hex(16)
        )
        error, preview = "", ""
        if request.method == "POST":
            try:
                values = self.posted(tool)
                from leedbox.domain.content import validate_catalog

                validate_catalog(self.kind, values)
                if request.form.get("action") == "preview":
                    from leedbox.domain.content import assemble, landing

                    payload = values["payload"]
                    if self.kind == "template" and tool in TEMPLATE_MODELS:
                        preview = "\n\n".join(
                            name + "\n" + assemble(value, {})
                            for name, value in payload["formats"].items()
                        )
                    elif self.kind == "template" and tool == "funnel_example":
                        preview = "Учебный пример (не реальный оффер)\n" + json.dumps(
                            payload["facts"], ensure_ascii=False, indent=2
                        )
                    elif self.kind == "template" and tool == "funnel":
                        preview = payload["rules"]
                    elif self.kind == "template":
                        preview = landing(
                            {"page_type": "service", "price_band": "high", "temperature": "cold"},
                            values,
                        )
                    elif self.kind == "idea":
                        preview = (
                            values["title"]
                            + "\n"
                            + "\n".join(payload["structure"])
                            + "\nАвтор заполняет: "
                            + "; ".join(payload["fields"])
                        )
                    elif self.kind == "niche":
                        preview = values["title"] + "\n" + "\n".join(payload["audiences"])
                    elif self.kind == "objection":
                        preview = (
                            values["title"]
                            + "\n\nВозможная причина: "
                            + payload["reason"]
                            + "\n\n"
                            + "\n\n".join(payload["answers"])
                            + "\n\nВопрос: "
                            + payload["question"]
                        )
                    elif self.kind in {"article", "category"}:
                        preview = (
                            values["title"]
                            + "\n\n"
                            + payload["description"]
                            + ("\n\n" + payload["url"] if self.kind == "article" else "")
                        )
                    else:
                        preview = (
                            values["title"]
                            + "\n"
                            + json.dumps(payload, ensure_ascii=False, indent=2)
                        )
                else:
                    confirmed()
                    with Session(self.engine) as db, db.begin():
                        save_catalog(
                            db,
                            self.config,
                            g.owner_id,
                            self.kind,
                            values,
                            id,
                            numeric(expected),
                            action_key,
                        )
                    flash(
                        "Каталог сохранён. Новые мастера используют эту версию; "
                        "старые результаты не меняются.",
                        "success",
                    )
                    return redirect(self.url + "/")
            except (ValueError, ObjectUnavailable) as problem:
                error = str(problem) or "Карточка недоступна"
        return self.editor(
            self.name,
            self.fields(values),
            {"expected": expected, "action_key": action_key},
            help_text=(
                "Статья ведёт на Telegra.ph или ваш WordPress. Проверьте адрес вручную: "
                "сервис не загружает страницы. Партнёры отображаются только "
                "при действующей маркировке или документированном основании её отсутствия."
                if self.kind == "article"
                else "Коды: ideas, offer, landing, case, proposal, sequence, funnel (расчёт), "
                "campaign, breakeven, budget_cpl, budget_cpc, utm, expert, material, invite. "
                "До одной рекомендации за сессию; показ выключен по умолчанию."
                if self.kind == "recommendation"
                else "Только демонстрационные сведения, не реальный оффер. "
                "Пример показывается отдельно и не заменяет бриф эксперта."
                if tool == "funnel_example"
                else "Это дополнительные правила. Обязательный запрет выдуманных фактов "
                "и предупреждение о персональных данных остаются в каждом промте."
                if tool == "funnel"
                else "Карточка каталога версионируется. Личные ответы экспертов хранятся отдельно."
                if self.kind in {"objection", "topic", "category"}
                else "Подстановки кейса: {task}, {baseline}, {work}, {result_number}, "
                "{timeframe}, {testimonial}, {product}. Получите согласие клиента на публикацию."
                if tool == "case"
                else "Подстановки КП: {problem}, {product}, {scope}, {process}, "
                "{price_conditions}, {conditions}, {action}. Цены и сроки вводит автор."
                if tool == "proposal"
                else "Подстановки: {product}, {audience}, {problem}, "
                "{desired_result}, {timeframe}, "
                "{price_conditions}, {evidence}, {advantages}, {risk_reversal}, {action}. "
                "Не добавляйте выдуманные числа, гарантии и отзывы. "
                "Пустые факты останутся [впишите]."
            ),
            error=error,
            preview=preview,
            previewable=True,
        )

    @expose("/csv")
    def export(self) -> Response:
        with Session(self.engine) as db:
            rows = list(
                db.scalars(select(CATALOGS[self.kind]).order_by(CATALOGS[self.kind].id).limit(1001))
            )
            if len(rows) > 1000:
                abort(413)
            binary = csv_export(
                [
                    ["slug", "title", "payload", "enabled", "revision"],
                    *[
                        [
                            row.slug,
                            row.title,
                            json.dumps(row.payload, ensure_ascii=False),
                            "1" if row.enabled else "0",
                            str(row.revision),
                        ]
                        for row in rows
                    ],
                ]
            )
        return Response(
            binary,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={self.kind}.csv"},
        )

    @expose("/import", methods=["GET", "POST"])
    def upload(self) -> str | WerkzeugResponse:
        error, raw = "", request.form.get("csv", "")
        if request.method == "POST":
            try:
                confirmed()
                if len(raw) > 200000:
                    raise ValueError("CSV: не более 200 000 символов")
                reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff")))
                if reader.fieldnames != ["slug", "title", "payload", "enabled", "revision"]:
                    raise ValueError(
                        "Заголовок CSV не совпадает с образцом. Первая строка должна быть "
                        "slug,title,payload,enabled,revision в этом порядке. "
                        "Экспортируйте каталог как образец и повторите импорт."
                    )
                entries = list(reader)
                if not 1 <= len(entries) <= 100 or len({row.get("slug") for row in entries}) != len(
                    entries
                ):
                    raise ValueError("От 1 до 100 строк с уникальными кодами")
                with Session(self.engine) as db, db.begin():
                    for item in entries:
                        if (
                            None in item
                            or any(value is None for value in item.values())
                            or item["enabled"] not in {"0", "1"}
                        ):
                            raise ValueError("Некорректная строка CSV")
                        title = item["title"]
                        if title.startswith("'") and title[1:].lstrip().startswith(
                            ("=", "+", "-", "@")
                        ):
                            title = title[1:]
                        values = {
                            "slug": item["slug"],
                            "title": title,
                            "payload": json.loads(item["payload"]),
                            "enabled": item["enabled"] == "1",
                        }
                        existing = db.scalar(
                            select(CATALOGS[self.kind]).where(
                                CATALOGS[self.kind].slug == item["slug"]
                            )
                        )
                        save_catalog(
                            db,
                            self.config,
                            g.owner_id,
                            self.kind,
                            values,
                            existing.id if existing else None,
                            numeric(item["revision"]),
                            fingerprint([request.form.get("action_key", ""), item["slug"]])[:32],
                        )
                flash("CSV импортирован одной транзакцией", "success")
                return redirect(self.url + "/")
            except (ValueError, ObjectUnavailable, csv.Error) as problem:
                error = str(problem) or "CSV не импортирован"
        return self.editor(
            "Импорт: " + self.name,
            [input_field("csv", "CSV — вставьте содержимое файла", raw, "textarea")],
            {"action_key": request.form.get("action_key") or secrets.token_hex(16)},
            help_text="Экспортируйте каталог как образец. "
            "Для существующей строки сохраняйте revision; "
            "для новой укажите 0. Импорт целиком отменяется при ошибке или конфликте.",
            error=error,
        )


class OperationsView(OwnerView):
    @expose("/")
    def index(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        term = request.args.get("q", "")[:100]
        query = select(User).where(User.expert_started_at.is_not(None))
        if term:
            query = query.where(
                User.username.icontains(term, autoescape=True)
                | (
                    User.telegram_id == numeric(term)
                    if term.isascii() and term.isdecimal()
                    else False
                )
            )
        with Session(self.engine) as db:
            entries = list(db.scalars(query.order_by(User.id).offset(page * 20).limit(21)))
            rows = [
                [
                    str(row.telegram_id),
                    row.username or "—",
                    row.expert_started_at.isoformat() if row.expert_started_at else "—",
                    {"label": "Проекты", "url": self.url + "/projects?owner=" + str(row.id)},
                    {
                        "label": "Доступ",
                        "url": self.config.admin_path + "/access/user?id=" + str(row.id),
                    },
                ]
                for row in entries[:20]
            ]
        return self.page(
            "table.html",
            title="Пользователи",
            columns=["Telegram ID", "Имя", "Первый вход UTC", "Проекты", "Доступ"],
            rows=rows,
            page=page,
            more=len(entries) > 20,
            search=True,
            links=[
                ("Проекты", self.url + "/projects"),
                ("Запросы ниш", self.url + "/suggestions"),
                ("Статистика", self.url + "/stats"),
                ("Партнёрские события", self.url + "/partners"),
                ("Подозрительная активность", self.url + "/suspicious"),
            ],
        )

    @expose("/projects")
    def projects(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        query = select(Project)
        if request.args.get("owner"):
            query = query.where(Project.owner_user_id == numeric(request.args["owner"]))
        term = request.args.get("q", "")[:100]
        if term:
            query = query.where(Project.name.icontains(term, autoescape=True))
        with Session(self.engine) as db:
            entries = list(db.scalars(query.order_by(Project.id).offset(page * 20).limit(21)))
            rows = [
                [
                    str(row.id),
                    str(row.owner_user_id),
                    row.name,
                    row.timezone,
                    "Архив" if row.archived else "Активный",
                    {"label": "Изменить", "url": self.url + "/project?id=" + str(row.id)},
                ]
                for row in entries[:20]
            ]
        return self.page(
            "table.html",
            title="Проекты",
            columns=["ID", "Владелец", "Название", "Часовой пояс", "Статус", "Действие"],
            rows=rows,
            page=page,
            more=len(entries) > 20,
            search=True,
        )

    @expose("/project", methods=["GET", "POST"])
    def project(self) -> str | WerkzeugResponse:
        values: dict[str, Any]
        id = numeric(request.args.get("id", ""))
        with Session(self.engine) as db:
            row = db.get(Project, id)
            if row is None:
                abort(404)
            values = {
                "name": row.name,
                "timezone": row.timezone,
                "contact": row.public_contact or "-",
                "archived": row.archived,
            }
            revision = row.revision
        error = ""
        expected = request.form.get("expected", "") if request.method == "POST" else str(revision)
        if request.method == "POST":
            values = {
                key: request.form.get(key) == "1"
                if key == "archived"
                else request.form.get(key, "")
                for key in values
            }
            try:
                confirmed()
                owner_required(self.config, g.owner_id)
                with Session(self.engine) as db, db.begin():
                    save_project_metadata(
                        db, self.config, g.owner_id, id, values, numeric(expected)
                    )
                flash("Метаданные проекта изменены; бриф и результаты сохранены", "success")
                return redirect(self.url + "/projects")
            except (ValueError, ObjectUnavailable) as problem:
                error = str(problem) or "Проект недоступен"
        return self.editor(
            f"Проект №{id}",
            [
                input_field(key, label, values[key], "checkbox" if key == "archived" else "text")
                for key, label in (
                    ("name", "Название"),
                    ("timezone", "Часовой пояс IANA"),
                    ("contact", "Telegram контакт или -"),
                    ("archived", "Архив"),
                )
            ],
            {"expected": expected},
            help_text="Владелец меняет только метаданные. Бриф не показывается в журнале; "
            "материалы не отключаются автоматически.",
            error=error,
        )

    @expose("/suggestions", methods=["GET", "POST"])
    def suggestions(self) -> str:
        if request.method == "POST":
            confirmed()
            status = request.form.get("status", "")
            if status not in {"new", "reviewed", "closed"}:
                abort(400)
            with Session(self.engine) as db, db.begin():
                owner_required(self.config, g.owner_id)
                row = db.get(NicheSuggestion, numeric(request.form.get("id", "")))
                if row is None:
                    abort(404)
                row.status = status
                db.add(
                    AuditLog(
                        actor_telegram_id=g.owner_id,
                        action="niche_suggestion_changed",
                        details={"id": row.id, "status": status},
                    )
                )
        with Session(self.engine) as db:
            rows = [
                [
                    str(row.id),
                    row.name,
                    row.status,
                    {"label": "Изменить статус", "url": self.url + "/suggestion?id=" + str(row.id)},
                ]
                for row in db.scalars(
                    select(NicheSuggestion).order_by(NicheSuggestion.id.desc()).limit(100)
                )
            ]
        return self.page(
            "table.html",
            title="Запросы новых ниш",
            columns=["ID", "Название", "Статус", "Действие"],
            rows=rows,
        )

    @expose("/suggestion", methods=["GET", "POST"])
    def suggestion(self) -> str | WerkzeugResponse:
        id = numeric(request.args.get("id", ""))
        with Session(self.engine) as db, db.begin():
            row = db.scalar(
                select(NicheSuggestion).where(NicheSuggestion.id == id).with_for_update()
            )
            if row is None:
                abort(404)
            error = ""
            if request.method == "POST":
                status = request.form.get("status", "")
                try:
                    confirmed()
                    if status not in {"new", "reviewed", "closed"}:
                        raise ValueError("Выберите статус из списка")
                    if request.form.get("expected") != row.status:
                        raise ValueError("Статус изменился. Откройте новую форму")
                    row.status = status
                    db.add(
                        AuditLog(
                            actor_telegram_id=g.owner_id,
                            action="niche_suggestion_changed",
                            details={"id": id, "status": status},
                        )
                    )
                    return redirect(self.url + "/suggestions")
                except ValueError as problem:
                    error = str(problem)
            return self.editor(
                "Запрос ниши: " + row.name,
                [
                    input_field(
                        "status",
                        "Статус",
                        request.form.get("status", row.status),
                        "select",
                        choices=[
                            ("new", "Новый"),
                            ("reviewed", "Рассмотрен"),
                            ("closed", "Закрыт"),
                        ],
                    )
                ],
                {"expected": row.status},
                error=error,
                help_text="Изменение статуса не публикует нишу автоматически.",
            )

    @expose("/stats")
    def stats(self) -> str:
        days = numeric(request.args.get("days", "30"), 365)
        bot_id = int(self.config.bot_token.get_secret_value().split(":")[0])
        tool_label = ProductEvent.details["tool"].astext
        with Session(self.engine) as db:
            events = list(
                db.execute(
                    select(ProductEvent.name, tool_label, func.count())
                    .where(
                        ProductEvent.bot_id == bot_id,
                        ProductEvent.created_at >= datetime.now(UTC) - timedelta(days=days),
                    )
                    .group_by(ProductEvent.name, tool_label)
                )
            )
            rows = [[str(name), str(tool or "—"), str(count)] for name, tool, count in events]
        return self.page(
            "table.html",
            title=f"События за {days} дней",
            columns=["Событие", "Инструмент", "Количество"],
            rows=rows,
            empty="За этот период событий нет. Пользовательский ввод в аналитику не включается.",
        )

    @expose("/partners")
    def partner_stats(self) -> str:
        bot_id = int(self.config.bot_token.get_secret_value().split(":")[0])
        with Session(self.engine) as db:
            counts = {
                (id, kind): count
                for id, kind, count in db.execute(
                    select(RecommendationEvent.partner_id, RecommendationEvent.kind, func.count())
                    .where(RecommendationEvent.bot_id == bot_id)
                    .group_by(RecommendationEvent.partner_id, RecommendationEvent.kind)
                )
            }
            rows = [
                [
                    partner.name,
                    str(counts.get((partner.id, "shown"), 0)),
                    str(counts.get((partner.id, "clicked"), 0)),
                    str(counts.get((partner.id, "conversion"), 0)),
                    {
                        "label": "Внести конверсию",
                        "url": self.url + "/conversion?id=" + str(partner.id),
                    },
                ]
                for partner in db.scalars(select(Partner).order_by(Partner.id).limit(100))
            ]
        return self.page(
            "table.html",
            title="Партнёрские события",
            columns=["Партнёр", "Показы", "Клики", "Конверсии", "Действие"],
            rows=rows,
            empty="Партнёры пока не добавлены. Конверсии вносятся вручную, "
            "без передачи данных эксперта.",
        )

    @expose("/conversion", methods=["GET", "POST"])
    def conversion(self) -> str | WerkzeugResponse:
        from leedbox.application.monetization import manual_conversion

        id = numeric(request.args.get("id", ""))
        with Session(self.engine) as db:
            partner = db.get(Partner, id)
            if partner is None:
                abort(404)
            name = partner.name
        key = (
            request.form.get("action_key", "")
            if request.method == "POST"
            else secrets.token_hex(16)
        )
        error = ""
        if request.method == "POST":
            try:
                confirmed()
                with Session(self.engine) as db, db.begin():
                    manual_conversion(
                        db,
                        self.config,
                        g.owner_id,
                        int(self.config.bot_token.get_secret_value().split(":")[0]),
                        id,
                        key,
                    )
                flash("Конверсия внесена", "success")
                return redirect(self.url + "/partners")
            except (ValueError, ObjectUnavailable) as problem:
                error = str(problem) or "Партнёр недоступен"
        return self.editor(
            "Внести конверсию: " + name,
            [],
            {"action_key": key},
            error=error,
            help_text="Подтвердите одну проверенную конверсию. "
            "Контакт или другие данные клиента не нужны.",
        )

    @expose("/suspicious")
    def suspicious(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        bot_id = int(self.config.bot_token.get_secret_value().split(":")[0])
        with Session(self.engine) as db:
            entries = list(
                db.scalars(
                    select(ProductEvent)
                    .where(
                        ProductEvent.bot_id == bot_id, ProductEvent.name == "suspicious_activity"
                    )
                    .order_by(ProductEvent.id.desc())
                    .offset(page * 25)
                    .limit(26)
                )
            )
            rows = [[str(row.id), row.name, row.created_at.isoformat()] for row in entries[:25]]
        return self.page(
            "table.html",
            title="Подозрительная активность",
            columns=["ID", "Тип", "UTC"],
            rows=rows,
            page=page,
            more=len(entries) > 25,
            empty="Записей нет. Один слабый признак не вызывает автоматическую блокировку.",
        )
