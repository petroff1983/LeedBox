"""Owner product editor with legal publication gate, audit and optimistic locking."""

from dataclasses import asdict
from uuid import uuid4

from flask import abort, flash, g, redirect, request
from flask_admin import expose  # type: ignore[import-untyped]
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application.partners import PartnerInput
from leedbox.application.settings import set_setting, setting
from leedbox.application.storefront import validate_products
from leedbox.application.versions import fingerprint
from leedbox.web.admin import PARTNER_LABELS, OwnerView, confirmed, input_field


class ProductsView(OwnerView):
    @expose("/")
    def index(self) -> str:
        with Session(self.engine) as db:
            values = sorted(setting(db, "store_products"), key=lambda row: row["sort_order"])
        rows = [
            [
                row["name"],
                row["sort_order"],
                "Опубликован" if row["enabled"] else "Черновик",
                {"label": "Редактировать", "url": self.url + "/edit?code=" + row["code"]},
            ]
            for row in values
        ]
        return self.page(
            "table.html",
            title="Проверенные товары",
            columns=["Товар", "Порядок", "Публикация", "Действие"],
            rows=rows,
            create_url=self.url + "/edit",
            links=[
                ("Тексты и консультации", self.config.admin_path + "/texts/"),
                ("Контакт и видимость", self.config.admin_path + "/settings/"),
                ("История и откат", self.config.admin_path + "/history/"),
            ],
            empty="Добавьте товар. Без маркировки или документированного основания "
            "он останется черновиком.",
        )

    @expose("/edit", methods=["GET", "POST"])
    def edit(self) -> str | Response:
        code = request.args.get("code", "")
        with Session(self.engine) as db:
            original = setting(db, "store_products")
        row = next((item for item in original if item["code"] == code), None)
        if code and row is None:
            abort(404)
        if row is None:
            row = {**asdict(PartnerInput("", "", "")), "sort_order": len(original)}
        fields = [*PartnerInput.__dataclass_fields__, "sort_order"]
        values = {key: row[key] for key in fields}
        expected = (
            request.form.get("expected", "") if request.method == "POST" else fingerprint(original)
        )
        identity = code or request.form.get("code", "") or uuid4().hex
        error, preview = "", ""
        if request.method == "POST":
            values = {
                key: request.form.get(key) == "1" if key == "enabled" else request.form.get(key, "")
                for key in fields
            }
            try:
                confirmed()
                if request.form.get("action") not in {"save", "preview"}:
                    raise ValueError("Выберите сохранение или предпросмотр")
                values["sort_order"] = int(values["sort_order"])
                checked = validate_products([{**values, "code": identity}])[0]
                if request.form.get("action") == "preview":
                    preview = f"{checked['name']}\n\n{checked['description']}\n\n{checked['url']}"
                else:
                    updated = [item for item in original if item["code"] != identity] + [checked]
                    with Session(self.engine) as db, db.begin():
                        set_setting(
                            db, self.config, g.owner_id, "store_products", updated, expected
                        )
                    flash("Товар сохранён", "success")
                    return redirect(self.url + "/")
            except ValueError as problem:
                error = str(problem)
        items = [
            input_field(
                key,
                PARTNER_LABELS.get(key, "Порядок"),
                value,
                "checkbox"
                if key == "enabled"
                else "number"
                if key == "sort_order"
                else "textarea"
                if key in {"description", "exception_reason", "marking_text"}
                else "text",
            )
            for key, value in values.items()
        ]
        return self.editor(
            "Товар",
            items,
            {"expected": expected, "code": identity},
            error=error,
            preview=preview,
            previewable=True,
            help_text="Название используется и на кнопке ссылки. "
            "Меньший порядок — выше в каталоге. "
            "Для публикации заполните маркировку или документированное основание её отсутствия. "
            "Черновик виден только владельцу в боте. Изменения применяются без перезапуска.",
        )
