"""THESIS: inspect a frozen order before an explicit money-changing command.
OWN-WORLD: existing Bootstrap4 tables and one-column editors, no card-entry UI.
STORY: inspect request, quote exact sum/receipt, inspect status, confirm operation.
FIRST VIEWPORT: task heading, safety explanation, frozen data before action.
FORM: precisely scoped extension of the existing owner console; no new identity.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish
review, the verdict, DESIGN.md, and every shipping raster carrying its provenance.
"""

import secrets
from decimal import Decimal, InvalidOperation

from flask import flash, g, redirect, request
from flask_admin import expose  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application.payment_operations import command, order_version, owner_order
from leedbox.application.purchasing import quote_request
from leedbox.application.services import owner_request, request_card
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import PaymentOperation, PaymentOrder, ServiceRequest
from leedbox.telegram.payments import STATUS
from leedbox.web.admin import OwnerView, confirmed, input_field, numeric


class PaymentsView(OwnerView):
    def bot_id(self) -> int:
        return int(self.config.bot_token.get_secret_value().split(":")[0])

    @expose("/")
    def index(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        with Session(self.engine) as db:
            query = select(PaymentOrder).where(PaymentOrder.bot_id == self.bot_id())
            state = request.args.get("status", "")
            if state in STATUS:
                query = query.where(PaymentOrder.status == state)
            entries = list(
                db.scalars(query.order_by(PaymentOrder.id.desc()).offset(page * 20).limit(21))
            )
            rows = [
                [
                    str(o.id),
                    o.provider,
                    "Тест" if o.test_mode else "Основной",
                    f"{o.amount} {o.currency}",
                    STATUS.get(o.status, o.status),
                    {"label": "Открыть", "url": self.url + "/detail?id=" + str(o.id)},
                ]
                for o in entries[:20]
            ]
        return self.page(
            "table.html",
            title="Платежи",
            columns=["Заказ", "Провайдер", "Режим", "Сумма", "Статус", "Действие"],
            rows=rows,
            page=page,
            more=len(entries) > 20,
            empty="Заказов пока нет. Счёт услуги создаётся из заявки; "
            "доступ покупается в Telegram.",
        )

    @expose("/quote", methods=["GET", "POST"])
    def quote(self) -> str | Response:
        id = numeric(request.args.get("request_id", ""))
        with Session(self.engine) as db:
            row = owner_request(db, self.config, g.owner_id, self.bot_id(), id)
            preview = request_card(row) if row.consent_revoked_at is None else "Согласие отозвано"
            revision = row.revision
        error = ""
        values = {
            "amount": request.form.get("amount", ""),
            "receipt": request.form.get("receipt", ""),
            "physical": request.form.get("physical") == "1",
        }
        action_key = (
            request.form.get("action_key", "")
            if request.method == "POST"
            else secrets.token_hex(16)
        )
        if request.method == "POST":
            try:
                confirmed()
                with Session(self.engine) as db, db.begin():
                    locked = db.scalar(
                        select(ServiceRequest)
                        .where(ServiceRequest.id == id, ServiceRequest.bot_id == self.bot_id())
                        .with_for_update()
                    )
                    if locked is None or locked.revision != numeric(
                        request.form.get("revision", "")
                    ):
                        raise ValueError("Заявка изменилась. Откройте форму заново")
                    order = quote_request(
                        db,
                        self.config,
                        g.owner_id,
                        self.bot_id(),
                        id,
                        Decimal(str(values["amount"]).replace(",", ".")),
                        str(values["receipt"]),
                        "physical_service" if values["physical"] else "unclassified",
                        action_key,
                    )
                    number = order.id
                flash(
                    "Счёт сохранён. Покупатель увидит его в «Мои заказы»; "
                    "сообщения автоматически не отправлены.",
                    "success",
                )
                return redirect(self.url + "/detail?id=" + str(number))
            except (ValueError, InvalidOperation, ObjectUnavailable) as problem:
                error = str(problem) or "Счёт недоступен"
        return self.editor(
            f"Счёт по заявке №{id}",
            [
                input_field(
                    "amount", "Полная сумма в RUB (до двух знаков после запятой)", values["amount"]
                ),
                input_field(
                    "receipt",
                    "Receipt JSON: реальные позиции, суммы и налоговые коды",
                    values["receipt"],
                    "textarea",
                ),
                input_field(
                    "physical",
                    "Подтверждаю: все позиции — допустимые нецифровые услуги, "
                    "не доступ к инструментам",
                    values["physical"],
                    "checkbox",
                ),
            ],
            {"revision": request.form.get("revision", revision), "action_key": action_key},
            help_text="Проверьте состав заявки, условия покупки и фискальные настройки. "
            "Цифровые услуги через Robokassa не выставляются. "
            "Цена карточки не используется как сумма счёта.",
            error=error,
            preview=preview,
            submit_label="Сохранить счёт",
            confirmation_label="Подтверждаю сумму, состав чека и условия счёта",
        )

    @expose("/detail", methods=["GET", "POST"])
    def detail(self) -> str | Response:
        id = numeric(request.args.get("id", ""))
        error = ""
        if request.method == "POST":
            try:
                confirmed()
                with Session(self.engine) as db, db.begin():
                    command(
                        db,
                        self.config,
                        g.owner_id,
                        self.bot_id(),
                        id,
                        request.form.get("operation", ""),
                        request.form.get("expected", ""),
                        request.form.get("reason", ""),
                    )
                flash(
                    "Действие записано. Возврат подтверждается только ответом провайдера; "
                    "при неопределённости не повторяйте его вслепую.",
                    "success",
                )
                return redirect(self.url + "/detail?id=" + str(id))
            except (ValueError, ObjectUnavailable) as problem:
                error = str(problem) or "Заказ недоступен"
        with Session(self.engine) as db:
            order = owner_order(db, self.config, g.owner_id, self.bot_id(), id)
            version = order_version(order)
            operations = list(
                db.scalars(select(PaymentOperation).where(PaymentOperation.order_id == id))
            )
            text = f"Заказ №{id}\n{order.amount} {order.currency}\n{STATUS[order.status]}\n" + (
                "ТЕСТОВЫЙ РЕЖИМ\n" if order.test_mode else "ОСНОВНОЙ РЕЖИМ\n"
            )
            labels = {
                "pending": "Ожидает обработки",
                "processing": "В обработке",
                "submitted": "Провайдер обрабатывает возврат",
                "done": "Подтверждено",
                "failed": "Не подтверждено — проверьте настройки и провайдера",
                "uncertain": "Неопределённый результат — не повторяйте возврат",
            }
            text += "\n".join(
                f"{'Возврат' if op.kind == 'refund' else 'Сверка'}: {labels[op.status]}"
                for op in operations
            )
            if order.snapshot.get("request_id"):
                text += "\nЗаявка №" + str(order.snapshot["request_id"])
        return self.editor(
            f"Платёж №{id}",
            [
                input_field(
                    "operation",
                    "Действие",
                    request.form.get("operation", "reconcile"),
                    "select",
                    choices=[
                        ("reconcile", "Сверить с провайдером"),
                        ("cancel", "Отменить неоплаченный заказ"),
                        ("refund", "Полный возврат"),
                    ],
                ),
                input_field(
                    "reason",
                    "Причина для операционного журнала (без контактов и секретов)",
                    request.form.get("reason", ""),
                    "textarea",
                ),
            ],
            {"expected": request.form.get("expected", version)},
            preview=text,
            error=error,
            submit_label="Подтвердить выбранное действие",
            confirmation_label="Подтверждаю выбранное действие; "
            "полный возврат изменяет деньги и доступ",
            help_text="Операции выполняет один bot-процесс. Полный возврат отменяет оплаченный "
            "этим заказом период; пробные и подаренные интервалы сохраняются. "
            "Сверка Stars проверяет до 10000 записей, неполная история не подтверждает оплату. "
            "При неопределённом результате "
            "проверьте провайдера, затем сверку: автоматического повторного возврата нет.",
        )
