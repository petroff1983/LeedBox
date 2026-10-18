"""Flask-Admin shell with explicit application commands instead of raw table editing."""

import json
import secrets
from dataclasses import fields
from datetime import UTC, datetime
from decimal import InvalidOperation
from pathlib import Path
from typing import Any, cast

from flask import Flask, abort, flash, g, redirect, request, send_file
from flask import session as browser_session
from flask_admin import Admin, AdminIndexView, BaseView, expose  # type: ignore[import-untyped]
from flask_admin.menu import MenuLink  # type: ignore[import-untyped]
from flask_admin.theme import Bootstrap4Theme  # type: ignore[import-untyped]
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application.admin_content import (
    create_catalog_entry,
    current_fingerprint,
    restore_version,
    save_policy_data,
)
from leedbox.application.entitlements import (
    access_for,
    adjust_grant_end,
    extend_access,
    policy,
    policy_data,
)
from leedbox.application.owner_overview import overview, window
from leedbox.application.partners import PartnerInput, save_partner
from leedbox.application.sections import SECTIONS, VISIBILITY_DEFAULTS, visibility
from leedbox.application.services import change_status, owner_request, request_card, save_service
from leedbox.application.settings import DEFAULTS, set_setting, setting
from leedbox.application.ui_texts import TEXTS, read_text, set_text
from leedbox.application.versions import fingerprint
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.services import ServiceInput
from leedbox.infrastructure.banner_assets import MAX_BANNER_BYTES, banner_source, save_banner
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    AccessGrant,
    AuditLog,
    ContentVersion,
    Partner,
    Service,
    ServiceRequest,
    User,
)

SERVICE_LABELS = {
    "title": "Название",
    "audience": "Для кого",
    "description": "Что входит",
    "duration": "Срок",
    "price_label": "Цена или «от»",
    "executor_details": "Исполнитель и реквизиты",
    "conditions": "Условия",
    "sort_order": "Порядок",
    "enabled": "Опубликована",
}
PARTNER_LABELS = {
    "name": "Название",
    "description": "Описание",
    "url": "Ссылка HTTP/HTTPS",
    "advertiser": "Рекламодатель",
    "inn": "ИНН",
    "erid": "erid",
    "marking_text": "Пометка о рекламе",
    "exception_reason": "Документированное основание отсутствия маркировки",
    "enabled": "Опубликован",
}
SETTING_LABELS = {
    "ai_credits_enabled": "Фиксированные кредиты: одноразовый переход со старых токенов",
    "ai_credit_price_rub": "Цена покупки одного кредита, ₽",
    "finance_fee_percent": "Расчётная комиссия приёма платежей, % (0 — не учтена)",
    "finance_tax_percent": "Расчётный налог с поступлений, % (0 — не учтён)",
    "finance_server_monthly_rub": "Сервер, ₽ за 30 дней (0 — не учтён)",
    "zenstat_package_price_rub": "Zenstat: оплаченная цена пакета, ₽",
    "zenstat_package_requests": "Zenstat: запросов в оплаченном пакете",
    "zenstat_low_requests": "Zenstat: уведомлять при остатке запросов не выше",
    "zenstat_balance_check_hours": "Zenstat: интервал фоновой проверки остатка, часов",
    "youtube_quota_limit": "YouTube: рабочий дневной предел приложения (не квота Google)",
    **{
        "ai_credit_cost_" + key: "Цена в кредитах: " + label
        for key, label in {
            "dzen_search": "поиск Дзен",
            "dzen": "5 заголовков",
            "writing_profile": "промпт статьи",
            "image_prompts": "5 промптов картинок",
            "lead_magnet": "5 тем ЛМ",
            "lead_magnet_revision": "критик и доработка ЛМ",
            "content_analysis": "анализ паттернов",
            "content_post": "один пост",
            "youtube_search": "YouTube",
            "content_search": "Telegram: анализ конкурента",
        }.items()
    },
    "ai_lead_magnet_enabled": "Агент лид-магнитов: включён",
    "ai_lead_magnet_generate_prompt": "Лид-магниты: инструкция создания (без переменных)",
    "ai_lead_magnet_critic_prompt": "Лид-магниты: инструкция критика (без переменных)",
    "ai_lead_magnet_revise_prompt": "Лид-магниты: инструкция доработки (без переменных)",
    "ai_content_enabled": "Контент-аналитик: включён",
    "ai_content_plan_prompt": "Контент-аналитик: паттерны и план (без переменных)",
    "ai_content_draft_prompt": "Контент-аналитик: черновики (без переменных)",
    "ai_content_edit_prompt": "Контент-аналитик: редактура (без переменных)",
    "ai_content_search_tokens": "Контент-аналитик: цена сбора через Apify, токенов (не расход ИИ)",
    "ai_content_apify_budget_usd": "Контент-аналитик: бюджет Apify на задачу, USD",
    "ai_images_enabled": "Агент картинок: включён",
    "ai_images_prompt": "Агент картинок: инструкция ИИ (без переменных)",
    "ai_dzen_enabled": "Агент Дзен: включён",
    "ai_dzen_prompt": "Агент Дзен: инструкция ИИ (без переменных)",
    "ai_dzen_article_template": "Агент Дзен: основа промпта канала ({{title}} и {{inputs}})",
    "ai_token_packages": "Пакеты AI-токенов (JSON)",
    "ai_free_dzen": "Дзен: бесплатных генераций заголовков за всё время",
    "ai_free_image_prompts": "Картинки: бесплатных генераций за всё время",
    "ai_free_lead_magnet": "Темы лид-магнитов: бесплатных генераций за всё время",
    "ai_free_content_search": "Конкуренты Telegram: бесплатных попыток за всё время",
    "ai_free_youtube_search": "YouTube: бесплатных анализов за всё время",
    "ai_youtube_search_tokens": "YouTube: цена повторного анализа в токенах (не расход ИИ)",
    "ai_provider_low_usd": "DeepSeek: уведомлять при остатке менее, USD",
    "ai_provider_low_cny": "DeepSeek: уведомлять при остатке менее, CNY",
    "ai_dzen_search_tokens": "Дзен: цена поиска топ-5 в AI-токенах (не расход модели)",
    "zenstat_request_cost_rub": "Zenstat: себестоимость запроса, ₽ (по вашему пакету)",
    "ai_input_usd_per_million": "ИИ: USD за миллион входных токенов (оценка)",
    "ai_output_usd_per_million": "ИИ: USD за миллион выходных токенов (оценка)",
    "ai_usd_rub": "ИИ: расчётный курс USD/RUB",
    "main_menu_banner": "Главное меню: картинка (загрузить PNG/JPEG)",
    "products_enabled": "Публикация товаров",
    "consultations_enabled": "Запись на консультации и обучение",
    "stars_access_price": "Цена периода доступа в Telegram Stars (0 — покупка отключена)",
    "wallet_visible": "Показывать «Баланс» в меню и профиле",
    "wallet_enabled": "Разрешить покупку AI-токенов и подписки с рублёвого баланса",
    "ai_token_price_rub": "Цена одного AI-токена в рублях (например 0.0003)",
    "ai_token_purchase_min_rub": "Минимальная сумма покупки AI-токенов в рублях",
    "wallet_topup_min_rub": "Минимальное пополнение рублёвого баланса",
    "wallet_topup_enabled": "Разрешить пополнение через Robokassa/СБП",
    "wallet_receipt_template": "Чек пополнения: JSON с одной позицией (сумма подставляется сама)",
    "wallet_sbp_alias": "СБП: Alias способа оплаты из настроек вашего магазина Robokassa",
    "payment_terms": "Условия покупки и возврата (заполняет владелец)",
    "offer_ad_limit": "Лимит символов оффера для объявления",
    "offer_button_limit": "Лимит символов текста кнопки",
    "active_material_limit": "Лимит активных материалов",
    "notification_interval_days": "Интервал уведомлений автора, дней",
    "rate_requests": "Запросов за окно",
    "rate_window_seconds": "Окно rate limit, секунд",
    "subscription_cooldown_seconds": "Пауза проверки подписки, секунд",
    "materials_enabled": "Публикация материалов",
    "services_enabled": "Приём заявок на услуги",
    "recommendations_enabled": "Рекомендации",
    "owner_contact": "Telegram контакт владельца",
    "service_contact_url": "Услуги: «Связаться» (HTTP/HTTPS или @username; "
    "пусто — контакт владельца)",
    "service_about_url": "Услуги: ссылка «Обо мне» (HTTP/HTTPS)",
    "owner_details": "Получатель данных заявки",
    "service_consent_text": "Согласие на заявку",
    "author_consent_version": "Версия согласия автора",
    "author_consent_text": "Согласие на обновления материала",
}
POLICY_LABELS = {
    "mode": "Режим сервиса",
    "price": "Цена периода",
    "currency": "Валюта",
    "period_days": "Оплачиваемый период, дней",
    "trial_days": "Пробный период, дней",
    "grace_days": "Льгота после окончания, дней",
    "activation_grace_days": "Льгота при включении платного режима, дней",
    "expired_delivery": "Выдача после окончания",
    "hide_for_paid": "Скрывать рекомендации у платящих",
}


def numeric(value: str, maximum: int = 2**63 - 1) -> int:
    if (
        not value.isascii()
        or not value.isdecimal()
        or len(value) > 19
        or not 0 <= int(value) <= maximum
    ):
        raise ValueError("Некорректный идентификатор или страница")
    return int(value)


def confirmed() -> None:
    if request.form.get("confirmed") != "1":
        raise ValueError("Подтвердите изменение перед сохранением")
    if any(len(request.form.getlist(key)) != 1 for key in request.form):
        raise ValueError("Форма содержит повторяющиеся поля")


def input_field(
    name: str, label: str, value: Any, kind: str = "text", **options: Any
) -> dict[str, Any]:
    return {"name": name, "label": label, "value": value, "kind": kind, **options}


class OwnerView(BaseView):
    hidden_from_navigation = False

    def is_visible(self) -> bool:
        return not self.hidden_from_navigation

    def __init__(
        self, engine: Engine, config: Settings, name: str, endpoint: str, path: str
    ) -> None:
        self.engine, self.config = engine, config
        super().__init__(name=name, endpoint=endpoint, url=config.admin_path + path)

    def is_accessible(self) -> bool:
        return bool(getattr(g, "owner_id", None) and self.config.is_owner(g.owner_id))

    def page(self, template: str, **values: Any) -> str:
        return cast(
            str,
            self.render(
                "owner/" + template,
                root=self.config.admin_path,
                csrf=browser_session.get("csrf", ""),
                **values,
            ),
        )

    def editor(
        self,
        title: str,
        items: list[dict[str, Any]],
        hidden: dict[str, Any],
        help_text: str = "Изменение применяется без перезапуска бота.",
        error: str = "",
        preview: str = "",
        previewable: bool = False,
        submit_label: str = "Сохранить изменение",
        confirmation_label: str = "Подтверждаю изменение данных сервиса",
        image_preview: str = "",
        portrait_preview: bool = False,
        lookup: bool = False,
    ) -> str:
        return self.page(
            "editor.html",
            title=title,
            fields=items,
            hidden=hidden,
            help_text=help_text,
            error=error,
            preview=preview,
            previewable=previewable,
            submit_label=submit_label,
            confirmation_label=confirmation_label,
            image_preview=image_preview,
            portrait_preview=portrait_preview,
            lookup=lookup,
        )


class CatalogView(OwnerView):
    def __init__(self, engine: Engine, config: Settings, kind: str) -> None:
        self.kind = kind
        super().__init__(
            engine,
            config,
            "Услуги" if kind == "service" else "Партнёры",
            "owner_" + kind,
            "/services" if kind == "service" else "/partners",
        )

    @expose("/")
    def index(self) -> str:
        model = Service if self.kind == "service" else Partner
        term = request.args.get("q", "")[:100]
        page = numeric(request.args.get("page", "0"), 10000)
        with Session(self.engine) as db:
            query = select(model)
            attribute = Service.title if self.kind == "service" else Partner.name
            if term:
                query = query.where(attribute.icontains(term, autoescape=True))
            if request.args.get("published") in {"0", "1"}:
                query = query.where(model.enabled.is_(request.args["published"] == "1"))
            entries = cast(
                list[Service | Partner],
                list(db.scalars(query.order_by(model.id).offset(page * 20).limit(21))),
            )
            rows = [
                [
                    str(row.id),
                    row.title if isinstance(row, Service) else row.name,
                    "Опубликовано" if row.enabled else "Черновик",
                    str(row.revision),
                    {"label": "Открыть", "url": self.url + "/edit?id=" + str(row.id)},
                ]
                for row in entries[:20]
            ]
        return self.page(
            "table.html",
            title=self.name,
            columns=["ID", "Название", "Статус", "Версия", "Действие"],
            rows=rows,
            create_url=self.url + "/edit",
            page=page,
            more=len(entries) > 20,
            search=True,
            empty=(
                "По выбранным условиям ничего не найдено. Сбросьте поиск и фильтры."
                if term or request.args.get("published") in {"0", "1"}
                else "Каталог пуст. Создайте карточку с реальными сведениями; "
                "она останется черновиком до публикации."
            ),
        )

    @expose("/edit", methods=["GET", "POST"])
    def edit(self) -> str | Response:
        dataclass = ServiceInput if self.kind == "service" else PartnerInput
        model = Service if self.kind == "service" else Partner
        labels = SERVICE_LABELS if self.kind == "service" else PARTNER_LABELS
        id = numeric(request.args["id"]) if request.args.get("id") else None
        with Session(self.engine) as db:
            row = cast(Service | Partner | None, db.get(model, id)) if id else None
            if id and row is None:
                abort(404)
            values: dict[str, Any] = {
                field.name: getattr(row, field.name)
                if row
                else (False if field.name == "enabled" else 0 if field.name == "sort_order" else "")
                for field in fields(dataclass)
            }
            revision = row.revision if row else 0
        expected = request.form.get("expected", "") if request.method == "POST" else str(revision)
        action_key = (
            request.form.get("action_key", "")
            if request.method == "POST"
            else secrets.token_hex(16)
        )
        error, preview = "", ""
        if request.method == "POST":
            values = {
                key: request.form.get(key) == "1" if key == "enabled" else request.form.get(key, "")
                for key in values
            }
            try:
                if self.kind == "service":
                    values["sort_order"] = int(values["sort_order"])
                payload = dataclass(**values)
                payload.validated()
                if request.form.get("action") == "preview":
                    preview = "\n\n".join(
                        f"{labels[key]}: {value}" for key, value in values.items()
                    )
                else:
                    confirmed()
                    with Session(self.engine) as db, db.begin():
                        if id is None:
                            if expected != "0":
                                raise ValueError("Обновите форму перед созданием карточки")
                            create_catalog_entry(
                                db, self.config, g.owner_id, self.kind, payload, action_key
                            )
                        elif self.kind == "service":
                            save_service(
                                db,
                                self.config,
                                g.owner_id,
                                ServiceInput(**values),
                                id,
                                int(expected),
                            )
                        else:
                            save_partner(
                                db,
                                self.config,
                                g.owner_id,
                                PartnerInput(**values),
                                id,
                                int(expected),
                            )
                    flash(
                        "Карточка сохранена. Бот увидит изменение при следующем открытии.",
                        "success",
                    )
                    return redirect(self.url + "/")
            except (ValueError, ObjectUnavailable) as problem:
                error = str(problem) or "Карточка недоступна"
        items = [
            input_field(
                key,
                labels[key],
                value,
                "checkbox"
                if key == "enabled"
                else "number"
                if key == "sort_order"
                else "textarea"
                if key
                in {
                    "description",
                    "audience",
                    "conditions",
                    "executor_details",
                    "exception_reason",
                    "marking_text",
                }
                else "text",
            )
            for key, value in values.items()
        ]
        return self.editor(
            "Услуга" if self.kind == "service" else "Партнёр",
            items,
            {"expected": expected, "action_key": action_key},
            help_text="Цена, сроки, реквизиты и условия — только из реальных данных владельца. "
            "Для публикации партнёра нужна маркировка "
            "или документированное основание её отсутствия.",
            error=error,
            preview=preview,
            previewable=True,
        )


class SectionsView(OwnerView):
    @expose("/", methods=["GET", "POST"])
    def index(self) -> str | Response:
        with Session(self.engine) as db:
            values = visibility(db)
        expected = (
            request.form.get("expected", "") if request.method == "POST" else fingerprint(values)
        )
        error = ""
        if request.method == "POST":
            submitted = {code: request.form.get(code) == "1" for code in SECTIONS}
            try:
                confirmed()
                if set(request.form) - {*SECTIONS, "confirmed", "expected", "csrf_token", "action"}:
                    raise ValueError("Форма содержит неизвестный раздел")
                with Session(self.engine) as db, db.begin():
                    # Serialize the whole form; concurrent edits must not overwrite another owner.
                    from leedbox.application.versions import lock_content

                    lock_content(db, "section_visibility", "main_menu")
                    if not expected or expected != fingerprint(visibility(db)):
                        raise ValueError(
                            "Разделы уже изменились. Обновите страницу перед сохранением"
                        )
                    for code, enabled in submitted.items():
                        set_setting(db, self.config, g.owner_id, f"section_{code}_visible", enabled)
                flash("Видимость разделов сохранена", "success")
                return redirect(self.url + "/")
            except ValueError as problem:
                error = str(problem)
                values = submitted
        return self.editor(
            "Разделы бота",
            [
                input_field(code, label, values[code], "checkbox")
                for code, label in SECTIONS.items()
            ],
            {"expected": expected},
            help_text="Галочка — раздел виден в главном меню. Снимите её, чтобы скрыть раздел. "
            "Изменения видны при следующем открытии /menu, без перезапуска. "
            "Данные и черновики сохраняются; выдача подарков "
            "по опубликованным ссылкам продолжается. "
            "Скрытый раздел нельзя открыть старой кнопкой или командой. "
            "Управление оплатой, удалением данных и отказом от уведомлений остаётся доступным.",
            error=error,
            submit_label="Сохранить видимость",
            confirmation_label="Применить видимость разделов в меню бота",
        )


class SettingsView(OwnerView):
    @expose("/banner-image")
    def banner_image(self) -> Response:
        with Session(self.engine) as db:
            source = banner_source(
                str(setting(db, "main_menu_banner")), self.config.owner_media_directory
            )
        if not isinstance(source, Path):
            abort(404)
        return send_file(source.resolve(), max_age=0)

    @expose("/")
    def index(self) -> str:
        with Session(self.engine) as db:
            rows = [
                [
                    SETTING_LABELS.get(key, key),
                    str(setting(db, key)),
                    {"label": "Изменить", "url": self.url + "/edit?key=" + key},
                ]
                for key in DEFAULTS
                if not key.startswith("prompt_template_")
                and not key.startswith(("ai_", "zenstat_"))
                and not key.startswith("consultation_")
                and key not in VISIBILITY_DEFAULTS
                and key
                not in {
                    "store_products",
                    "storefront_seed_version",
                    "offer_ad_limit",
                    "offer_button_limit",
                }
            ]
        return self.page(
            "table.html",
            title="Настройки",
            columns=["Настройка", "Значение", "Действие"],
            rows=rows,
        )

    @expose("/edit", methods=["GET", "POST"])
    def edit(self) -> str | Response:
        key = request.args.get("key", "")
        if key not in DEFAULTS:
            abort(404)
        if key.startswith("consultation_"):
            return redirect(self.config.admin_path + "/consultations/")
        if key in VISIBILITY_DEFAULTS:
            return redirect(self.config.admin_path + "/sections/")
        if key == "store_products":
            return redirect(self.config.admin_path + "/products/")
        with Session(self.engine) as db:
            value = setting(db, key)
        local_preview = key == "main_menu_banner" and isinstance(
            banner_source(str(value), self.config.owner_media_directory), Path
        )
        expected = (
            request.form.get("expected", "") if request.method == "POST" else fingerprint(value)
        )
        kind = (
            "checkbox"
            if type(DEFAULTS[key]) is bool
            else "number"
            if type(DEFAULTS[key]) is int
            else "textarea"
        )
        error = ""
        if request.method == "POST":
            value = (
                request.form.get("value") == "1"
                if kind == "checkbox"
                else request.form.get("value", "")
            )
            try:
                confirmed()
                parsed = int(value) if kind == "number" else value
                if key == "ai_token_packages":
                    parsed = json.loads(str(value))
                if key == "main_menu_banner":
                    uploads = request.files.getlist("banner_file")
                    if len(uploads) > 1:
                        raise ValueError("Загрузите только одну картинку")
                    if uploads and uploads[0].filename:
                        parsed = save_banner(
                            uploads[0].read(MAX_BANNER_BYTES + 1), self.config.owner_media_directory
                        )
                with Session(self.engine) as db, db.begin():
                    set_setting(db, self.config, g.owner_id, key, parsed, expected)
                flash("Настройка сохранена", "success")
                return redirect(self.url + "/")
            except ValueError as problem:
                error = str(problem)
        items = [input_field("value", "Значение", value, kind)]
        if key == "main_menu_banner":
            items.append(
                input_field(
                    "banner_file",
                    "Новая картинка PNG/JPEG (до 5 МБ)",
                    "",
                    "file",
                    accept="image/png,image/jpeg",
                )
            )
        return self.editor(
            SETTING_LABELS.get(key, key),
            items,
            {"expected": expected},
            error=error,
            help_text=(
                "Загрузите картинку с компьютера и подтвердите сохранение. "
                "Или укажите URL/file_id; default — стандартный баннер, off — без картинки. "
                "Рекомендуемый формат — горизонтальный 2,5:1. "
                "Изменение видно при следующем открытии меню."
                if key == "main_menu_banner"
                else "Изменение применяется без перезапуска бота."
            ),
            image_preview=self.url + "/banner-image" if local_preview else "",
        )


class TextsView(OwnerView):
    @expose("/")
    def index(self) -> str:
        with Session(self.engine) as db:
            rows = [
                [
                    key,
                    read_text(db, key),
                    {"label": "Изменить", "url": self.url + "/edit?key=" + key},
                ]
                for key in TEXTS
            ]
        return self.page(
            "table.html",
            title="Тексты интерфейса",
            columns=["Экран", "Текст", "Действие"],
            rows=rows,
        )

    @expose("/edit", methods=["GET", "POST"])
    def edit(self) -> str | Response:
        key = request.args.get("key", "")
        if key not in TEXTS:
            abort(404)
        with Session(self.engine) as db:
            value = read_text(db, key)
        expected = (
            request.form.get("expected", "") if request.method == "POST" else fingerprint(value)
        )
        error, preview = "", ""
        if request.method == "POST":
            value = request.form.get("value", "")
            if request.form.get("action") == "preview":
                preview = value
            else:
                try:
                    confirmed()
                    with Session(self.engine) as db, db.begin():
                        set_text(db, self.config, g.owner_id, key, value, expected)
                    flash("Текст сохранён", "success")
                    return redirect(self.url + "/")
                except ValueError as problem:
                    error = str(problem)
        return self.editor(
            "Текст: " + key,
            [input_field("value", "Сообщение бота", value, "textarea")],
            {"expected": expected},
            error=error,
            preview=preview,
            previewable=True,
        )


class AccessView(OwnerView):
    @expose("/", methods=["GET", "POST"])
    def index(self) -> str | Response:
        with Session(self.engine) as db:
            values = policy_data(policy(db))
        expected = (
            request.form.get("expected", "") if request.method == "POST" else fingerprint(values)
        )
        error = ""
        if request.method == "POST":
            values = {
                key: request.form.get(key) == "1"
                if key == "hide_for_paid"
                else request.form.get(key, "")
                for key in values
            }
            try:
                confirmed()
                with Session(self.engine) as db, db.begin():
                    save_policy_data(db, self.config, g.owner_id, values, expected)
                flash(
                    "Политика сохранена. Покупка зависит от настройки провайдеров и условий.",
                    "success",
                )
                return redirect(self.url + "/")
            except (ValueError, InvalidOperation) as problem:
                error = str(problem) or "Некорректная цена"
        items = [
            input_field(
                key,
                POLICY_LABELS[key],
                value,
                "checkbox"
                if key == "hide_for_paid"
                else "number"
                if key.endswith("days")
                else "select"
                if key in {"mode", "expired_delivery"}
                else "text",
                choices=[("free", "Бесплатный"), ("paid", "Платный")]
                if key == "mode"
                else [("bypass", "Выдавать всем без проверки подписки")],
            )
            for key, value in values.items()
        ]
        return self.editor(
            "Доступ и пробный период",
            items,
            {"expected": expected},
            error=error,
            help_text="Бесплатный режим — по умолчанию. Цена и пробный период настраиваются здесь. "
            "Stars настраиваются отдельно. Для рублёвого баланса включите настройки wallet; "
            "цена RUB и период задаются здесь. Ручное продление остаётся доступным. "
            "При включении платного режима задайте льготу для действующих экспертов. "
            "Пробы агентов и пробный период не включают проверку подписки в платном режиме. "
            "Проверку включает оплата или ручной доступ владельца. "
            "Получатели не платят. После окончания доступа эксперта опубликованные "
            "подарки выдаются всем по ссылке, без проверки подписки.",
        )

    @expose("/users")
    def users(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        query = select(User).where(User.expert_started_at.is_not(None))
        term = request.args.get("q", "")
        if term:
            query = query.where(User.telegram_id == numeric(term))
        with Session(self.engine) as db:
            entries = list(db.scalars(query.order_by(User.id).offset(page * 20).limit(21)))
            rows = [
                [
                    str(user.telegram_id),
                    access_for(db, user.id).state,
                    {"label": "Открыть доступ", "url": self.url + "/user?id=" + str(user.id)},
                ]
                for user in entries[:20]
            ]
        return self.page(
            "table.html",
            title="Доступ экспертов",
            columns=["Telegram ID", "Состояние", "Действие"],
            rows=rows,
            search=True,
            page=page,
            more=len(entries) > 20,
        )

    @expose("/user", methods=["GET", "POST"])
    def user(self) -> str:
        id = numeric(request.args.get("id", ""))
        error = ""
        if request.method == "POST":
            try:
                confirmed()
                with Session(self.engine) as db, db.begin():
                    user = db.get(User, id)
                    if not user or not user.expert_started_at:
                        raise ValueError("Эксперт не найден")
                    if request.form.get("action") == "extend":
                        action_key = request.form.get("action_key", "")
                        if len(action_key) != 32 or any(
                            char not in "0123456789abcdef" for char in action_key
                        ):
                            raise ValueError("Обновите страницу перед продлением")
                        extend_access(
                            db,
                            self.config,
                            g.owner_id,
                            id,
                            numeric(request.form.get("days", ""), 365000),
                            "web:" + action_key,
                        )
                    elif request.form.get("action") == "adjust":
                        grant = db.get(AccessGrant, numeric(request.form.get("grant_id", "")))
                        if not grant or grant.user_id != id:
                            raise ValueError("Доступ не найден")
                        end = datetime.fromisoformat(request.form.get("end", ""))
                        if end.tzinfo is None:
                            end = end.replace(tzinfo=UTC)
                        adjust_grant_end(
                            db,
                            self.config,
                            g.owner_id,
                            grant.id,
                            datetime.fromisoformat(request.form.get("expected_end", "")),
                            end,
                        )
                    else:
                        raise ValueError("Неизвестное действие")
                flash("Срок доступа изменён, действие записано в аудит", "success")
            except ValueError as problem:
                error = str(problem)
        with Session(self.engine) as db:
            user = db.get(User, id)
            if not user or not user.expert_started_at:
                abort(404)
            grants = list(
                db.scalars(
                    select(AccessGrant)
                    .where(AccessGrant.user_id == id)
                    .order_by(AccessGrant.id.desc())
                    .limit(50)
                )
            )
            items = [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "start": row.starts_at.isoformat(),
                    "end": row.ends_at.isoformat(),
                    "end_input": row.ends_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                }
                for row in grants
            ]
        return self.page(
            "access_user.html",
            title=f"Доступ эксперта {user.telegram_id}",
            grants=items,
            action_key=request.form.get("action_key") or secrets.token_hex(16),
            error=error,
        )


class RequestsView(OwnerView):
    @expose("/")
    def index(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        bot_id = int(self.config.bot_token.get_secret_value().split(":")[0])
        query = select(ServiceRequest).where(ServiceRequest.bot_id == bot_id)
        if request.args.get("status") in {"new", "working", "closed"}:
            query = query.where(ServiceRequest.status == request.args["status"])
        with Session(self.engine) as db:
            entries = list(
                db.scalars(query.order_by(ServiceRequest.id.desc()).offset(page * 20).limit(21))
            )
            rows = [
                [
                    str(row.id),
                    row.status,
                    row.created_at.isoformat(),
                    {"label": "Открыть", "url": self.url + "/detail?id=" + str(row.id)},
                ]
                for row in entries[:20]
            ]
        return self.page(
            "table.html",
            title="Заявки на услуги",
            columns=["ID", "Статус", "Создана UTC", "Действие"],
            rows=rows,
            page=page,
            more=len(entries) > 20,
            status_filter=True,
        )

    @expose("/detail", methods=["GET", "POST"])
    def detail(self) -> str | Response:
        id = numeric(request.args.get("id", ""))
        bot_id = int(self.config.bot_token.get_secret_value().split(":")[0])
        error = ""
        if request.method == "POST":
            try:
                confirmed()
                with Session(self.engine) as db, db.begin():
                    change_status(
                        db,
                        self.config,
                        g.owner_id,
                        bot_id,
                        id,
                        numeric(request.form.get("expected", "")),
                        request.form.get("target", ""),
                    )
                flash("Статус заявки сохранён", "success")
                return redirect(self.url + "/detail?id=" + str(id))
            except (ValueError, ObjectUnavailable) as problem:
                error = str(problem) or "Заявка недоступна"
        with Session(self.engine) as db:
            row = owner_request(db, self.config, g.owner_id, bot_id, id)
            text = (
                request_card(row)
                if row.consent_revoked_at is None
                else "Согласие отозвано. Контакт и комментарий скрыты."
            )
            version, status = row.revision, row.status
        return self.page(
            "request.html",
            title=f"Заявка №{id}",
            text=text,
            revision=version,
            status=status,
            error=error,
            quote_url=self.config.admin_path + "/payments/quote?request_id=" + str(id),
        )


class HistoryView(OwnerView):
    @expose("/")
    def index(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        with Session(self.engine) as db:
            entries = list(
                db.scalars(
                    select(ContentVersion)
                    .order_by(ContentVersion.id.desc())
                    .offset(page * 25)
                    .limit(26)
                )
            )
            rows = [
                [
                    row.entity,
                    row.object_key,
                    str(row.number),
                    row.created_at.isoformat(),
                    {"label": "Просмотр / откат", "url": self.url + "/restore?id=" + str(row.id)},
                ]
                for row in entries[:25]
            ]
        return self.page(
            "table.html",
            title="История контента",
            columns=["Тип", "Объект", "Версия", "UTC", "Действие"],
            rows=rows,
            page=page,
            more=len(entries) > 25,
        )

    @expose("/restore", methods=["GET", "POST"])
    def restore(self) -> str | Response:
        id = numeric(request.args.get("id", ""))
        error = ""
        if request.method == "POST":
            try:
                confirmed()
                with Session(self.engine) as db, db.begin():
                    restore_version(
                        db, self.config, g.owner_id, id, request.form.get("expected", "")
                    )
                flash("Версия восстановлена как новое изменение. История сохранена.", "success")
                return redirect(self.url + "/")
            except (ValueError, ObjectUnavailable) as problem:
                error = str(problem) or "Объект больше не существует"
        with Session(self.engine) as db:
            row = db.get(ContentVersion, id)
            if row is None:
                abort(404)
            preview = json.dumps(row.data, ensure_ascii=False, indent=2)
            try:
                expected = current_fingerprint(db, row.entity, row.object_key)
            except ObjectUnavailable:
                abort(404)
        return self.editor(
            "Восстановить версию",
            [],
            {"expected": request.form.get("expected", expected)},
            help_text="Снимок ниже заменит текущий контент. "
            "Откат проходит те же проверки и записывается как новая версия. "
            "Он не отменяет уже отправленные сообщения, заявки или предоставленный доступ.",
            preview=preview,
            error=error,
        )

    @expose("/audit")
    def audit(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        with Session(self.engine) as db:
            entries = list(
                db.scalars(
                    select(AuditLog).order_by(AuditLog.id.desc()).offset(page * 25).limit(26)
                )
            )
            rows = [
                [
                    str(row.id),
                    row.action,
                    str(row.actor_telegram_id or "—"),
                    row.created_at.isoformat(),
                ]
                for row in entries[:25]
            ]
        return self.page(
            "table.html",
            title="Аудит",
            columns=["ID", "Действие", "Telegram ID владельца", "UTC"],
            rows=rows,
            page=page,
            more=len(entries) > 25,
        )


class OwnerIndex(AdminIndexView):
    def __init__(self, engine: Engine, config: Settings) -> None:
        self.engine, self.config = engine, config
        super().__init__(name="Обзор", endpoint="owner_admin", url=config.admin_path)

    def is_accessible(self) -> bool:
        return bool(getattr(g, "owner_id", None) and self.config.is_owner(g.owner_id))

    @expose("/")
    def index(self) -> str:
        try:
            if any(len(request.args.getlist(key)) > 1 for key in ("period", "mode")):
                abort(400)
            report = window(
                request.args.get("period", "7"),
                request.args.get(
                    "mode", "live" if self.config.environment == "production" else "test"
                ),
            )
        except ValueError:
            abort(400)
        with Session(self.engine) as db:
            values = overview(db, self.config, report)
        return cast(
            str,
            self.render(
                "owner/home.html",
                root=self.config.admin_path,
                csrf=browser_session.get("csrf", ""),
                **values,
            ),
        )


def install_admin(app: Flask, engine: Engine, config: Settings) -> None:
    admin = Admin(
        app,
        name="LeedBox",
        index_view=OwnerIndex(engine, config),
        url=config.admin_path,
        endpoint="owner_admin",
        theme=Bootstrap4Theme(base_template="owner/master.html"),
        csp_nonce_generator=lambda: getattr(g, "csp_nonce", ""),
    )

    def add(view: OwnerView, category: str | None = None, hidden: bool = False) -> None:
        view.category = category
        view.hidden_from_navigation = hidden
        admin.add_view(view)

    add(CatalogView(engine, config, "service"), "Товары и услуги")
    add(RequestsView(engine, config, "Заявки", "owner_requests", "/requests"), "Клиенты")
    from leedbox.web.finance_admin import FinanceView
    from leedbox.web.payment_admin import PaymentsView

    add(FinanceView(engine, config, "Финансы и кредиты", "owner_finance", "/finance"))
    add(PaymentsView(engine, config, "Платежи", "owner_payments", "/payments"), "Клиенты")
    add(SettingsView(engine, config, "Настройки бота", "owner_settings", "/settings"), "Управление")
    add(SectionsView(engine, config, "Разделы бота", "owner_sections", "/sections"), "Управление")
    from leedbox.web.legal_admin import LegalView

    add(LegalView(engine, config, "Документы и согласия", "owner_legal", "/legal"), "Управление")
    from leedbox.web.agent_admin import AgentsView

    add(AgentsView(engine, config, "Агенты и токены", "owner_agents", "/agents"), hidden=True)
    add(TextsView(engine, config, "Сообщения бота", "owner_texts", "/texts"), "Управление")
    from leedbox.web.prompt_admin import PromptAgentsView, PromptView

    add(PromptAgentsView(engine, config, "Промпты и агенты", "owner_prompt_agents", "/ai"))
    add(PromptView(engine, config, "Шаблоны промтов", "owner_prompts", "/prompts"), hidden=True)
    from leedbox.web.broadcast_admin import BroadcastView

    add(BroadcastView(engine, config, "Рассылки", "owner_broadcasts", "/broadcasts"))
    from leedbox.web.store_admin import ProductsView

    add(ProductsView(engine, config, "Товары", "owner_products", "/products"), "Товары и услуги")
    from leedbox.web.consultation_admin import ConsultationView

    add(
        ConsultationView(
            engine, config, "Консультации и обучение", "owner_consultations", "/consultations"
        ),
        "Товары и услуги",
    )
    add(CatalogView(engine, config, "partner"), "Товары и услуги")
    add(AccessView(engine, config, "Цена и доступ", "owner_access", "/access"), "Клиенты")
    add(HistoryView(engine, config, "История и аудит", "owner_history", "/history"), "Управление")
    from leedbox.web.content_admin import ContentView, OperationsView

    for kind, name in (
        ("niche", "Ниши"),
        ("idea", "Идеи лид-магнитов"),
        ("template", "Шаблоны"),
        ("objection", "Возражения"),
        ("topic", "Темы воронки"),
        ("category", "Категории знаний"),
        ("article", "Статьи"),
        ("recommendation", "Рекомендации"),
    ):
        add(
            ContentView(engine, config, kind, name),
            "Товары и услуги" if kind == "recommendation" else "Промты",
            hidden=kind
            in {"niche", "idea", "objection", "template", "topic", "category", "article"},
        )
    add(
        OperationsView(engine, config, "Эксперты и данные", "owner_operations", "/operations"),
        "Клиенты",
    )
    app.extensions["leedbox_admin"] = admin
    admin.add_link(MenuLink(name="Безопасность", url=config.admin_path + "/security"))
