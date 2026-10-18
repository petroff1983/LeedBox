"""Campaign preparation and explicit, CSRF-protected release to the bot outbox."""

from flask import abort, flash, g, redirect, request
from flask_admin import expose  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application import broadcasts
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import OwnerBroadcast
from leedbox.web.admin import OwnerView, confirmed, input_field, numeric

STATUS = {"draft": "Черновик", "queued": "В очереди / отправлена", "cancelled": "Остановлена"}
FIELDS = {
    "text": "Текст сообщения (до 3 200 символов)",
    "button_label": "Подпись кнопки (необязательно)",
    "button_url": "Ссылка кнопки HTTP/HTTPS",
    "advertiser": "Рекламодатель (для рекламы)",
    "inn": "ИНН рекламодателя",
    "erid": "erid",
    "marking_text": "Пометка о рекламе",
}
OUTCOMES = {
    "pending": "В очереди",
    "processing": "Отправляются",
    "sent": "Доставлены",
    "failed": "Ошибки / бот заблокирован",
    "uncertain": "Доставка не подтверждена",
    "cancelled": "Отменены / отписались",
}


class BroadcastView(OwnerView):
    @expose("/")
    def index(self) -> str:
        page = numeric(request.args.get("page", "0"), 10000)
        with Session(self.engine) as db:
            entries = list(
                db.scalars(
                    select(OwnerBroadcast)
                    .where(OwnerBroadcast.bot_id == broadcasts.configured_bot(self.config))
                    .order_by(OwnerBroadcast.id.desc())
                    .offset(page * 20)
                    .limit(21)
                )
            )
            rows = [
                [
                    row.title,
                    "Реклама" if row.kind == "advertising" else "Новость сервиса",
                    STATUS[row.status],
                    {"label": "Открыть", "url": self.url + "/open?id=" + str(row.id)},
                ]
                for row in entries[:20]
            ]
        links = [("Создать рассылку", self.url + "/edit")]
        if page:
            links.append(("← Предыдущие", self.url + "/?page=" + str(page - 1)))
        if len(entries) > 20:
            links.append(("Следующие →", self.url + "/?page=" + str(page + 1)))
        return self.page(
            "table.html",
            title="Рассылки владельца",
            columns=["Название", "Тип", "Статус", "Действие"],
            rows=rows,
            links=links,
        )

    @expose("/edit", methods=["GET", "POST"])
    def edit(self) -> str | Response:
        id = numeric(request.args["id"]) if "id" in request.args else None
        initial = {}
        title, kind, revision = "", "notice", 0
        if id is not None:
            try:
                with Session(self.engine) as db:
                    row = broadcasts.campaign(db, self.config, g.owner_id, id)
                    if row.status != "draft":
                        return redirect(self.url + "/open?id=" + str(id))
                    title, kind, revision, initial = row.title, row.kind, row.revision, row.payload
            except ObjectUnavailable:
                abort(404)
        title = request.form.get("title", title)
        kind = request.form.get("kind", kind)
        values = {
            key: request.form.get(key, initial.get(key, "Реклама" if key == "marking_text" else ""))
            for key in FIELDS
        }
        expected = request.form.get("expected", str(revision))
        error, preview = "", ""
        if request.method == "POST":
            try:
                confirmed()
                if request.form.get("action") == "preview":
                    preview = broadcasts.message_text(kind, values)
                elif request.form.get("action") == "save":
                    with Session(self.engine) as db, db.begin():
                        saved = broadcasts.save_campaign(
                            db, self.config, g.owner_id, title, kind, values, id, numeric(expected)
                        )
                        saved_id = saved.id
                    flash(
                        "Черновик сохранён. Проверьте текст и адресатов перед отправкой.", "success"
                    )
                    return redirect(self.url + "/open?id=" + str(saved_id))
                else:
                    raise ValueError("Выберите предпросмотр или сохранение черновика")
            except ValueError as problem:
                error = str(problem)
        return self.editor(
            "Черновик рассылки",
            [
                input_field("title", "Название (видно только вам)", title),
                input_field(
                    "kind",
                    "Тип сообщения",
                    kind,
                    "select",
                    choices=[("notice", "Новость сервиса"), ("advertising", "Реклама")],
                ),
                *[
                    input_field(key, label, values[key], "textarea" if key == "text" else "text")
                    for key, label in FIELDS.items()
                ],
            ],
            {"expected": expected},
            error=error,
            preview=preview,
            previewable=True,
            submit_label="Сохранить черновик",
            help_text=(
                "Только для клиентов-экспертов этого бота, не для получателей подарков. "
                "Новость — информация о работе сервиса, без рекламы; "
                "рекламные предложения оформляйте как рекламу. "
                "Для рекламы нужны маркировка и отдельное согласие адресата. "
                "HTML не используется. Сохранение и предпросмотр ничего не отправляют."
            ),
        )

    @expose("/open", methods=["GET", "POST"])
    def open(self) -> str | Response:
        id = numeric(request.args.get("id", "0"))
        error = ""
        try:
            if request.method == "POST":
                try:
                    confirmed()
                    with Session(self.engine) as db, db.begin():
                        if request.form.get("action") == "send":
                            count = broadcasts.enqueue(
                                db,
                                self.config,
                                g.owner_id,
                                id,
                                numeric(request.form.get("expected", "0")),
                            )
                            flash(
                                f"В очередь поставлено сообщений: {count}. "
                                "Бот отправляет их в фоне.",
                                "success",
                            )
                        elif request.form.get("action") == "cancel":
                            broadcasts.cancel(db, self.config, g.owner_id, id)
                            flash(
                                "Рассылка остановлена. Уже доставленные сообщения не удаляются; "
                                "отправка в процессе может успеть завершиться.",
                                "success",
                            )
                        else:
                            raise ValueError("Выберите отправку или остановку")
                    return redirect(self.url + "/open?id=" + str(id))
                except ValueError as problem:
                    error = str(problem)
            with Session(self.engine) as db:
                row = broadcasts.campaign(db, self.config, g.owner_id, id)
                return self.page(
                    "broadcast.html",
                    title=row.title,
                    kind="Реклама" if row.kind == "advertising" else "Новость сервиса",
                    status=STATUS[row.status],
                    draft=row.status == "draft",
                    stopped=row.status == "cancelled",
                    expected=row.revision,
                    error=error,
                    preview=broadcasts.message_text(row.kind, row.payload),
                    button_label=row.payload["button_label"],
                    button_url=row.payload["button_url"],
                    audience=broadcasts.audience_count(db, row.bot_id, row.kind),
                    outcomes=[
                        (OUTCOMES[key], value) for key, value in broadcasts.counts(db, id).items()
                    ],
                    edit_url=self.url + "/edit?id=" + str(id),
                    list_url=self.url + "/",
                )
        except ObjectUnavailable:
            abort(404)
