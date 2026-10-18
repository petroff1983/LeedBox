"""Owner HTML editing, drafts and immutable publication within the existing console."""

from flask import abort, flash, g, redirect, request
from flask_admin import expose  # type: ignore[import-untyped]
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application import legal
from leedbox.application.settings import set_setting, setting
from leedbox.application.versions import fingerprint
from leedbox.infrastructure.models import ContentVersion, LegalConsentEvent
from leedbox.web.admin import OwnerView, confirmed


class LegalView(OwnerView):
    @expose("/", methods=["GET", "POST"])
    def index(self) -> str | Response:
        error = ""
        if request.method == "POST":
            confirmed()
            try:
                with Session(self.engine) as db, db.begin():
                    set_setting(
                        db,
                        self.config,
                        g.owner_id,
                        "legal_gate_enabled",
                        request.form.get("enabled") == "1",
                        request.form.get("expected", ""),
                    )
                flash("Настройка проверки согласия сохранена.", "success")
                return redirect(self.url + "/")
            except ValueError as problem:
                error = str(problem)
        with Session(self.engine) as db:
            enabled = bool(setting(db, "legal_gate_enabled"))
            docs = []
            for slug, label in legal.DOCUMENTS.items():
                live, draft = legal.current(db, slug), legal.draft(db, slug)
                docs.append(
                    dict(
                        slug=slug,
                        label=label,
                        title=draft["title"],
                        state=f"Опубликована версия {live.number}" if live else "Не опубликован",
                        changed=bool(live and draft != live.data),
                        url=legal.public_url(self.config, slug) if live else "",
                    )
                )
            rows = list(
                db.execute(
                    select(LegalConsentEvent, ContentVersion.number)
                    .join(
                        ContentVersion,
                        LegalConsentEvent.version_id == ContentVersion.id,
                    )
                    .where(
                        LegalConsentEvent.bot_id
                        == int(self.config.bot_token.get_secret_value().split(":")[0])
                    )
                    .order_by(LegalConsentEvent.id.desc())
                    .limit(50)
                )
            )
        return self.page(
            "legal-index.html",
            title="Документы и согласия",
            docs=docs,
            enabled=enabled,
            expected=fingerprint(enabled),
            error=error,
            site_url=legal.public_url(self.config),
            events=rows,
        )

    @expose("/edit", methods=["GET", "POST"])
    def edit(self) -> str | Response:
        slug = request.args.get("slug", "")
        if slug not in legal.DOCUMENTS:
            abort(404)
        with Session(self.engine) as db:
            draft, live = legal.draft(db, slug), legal.current(db, slug)
            expected = legal.editor_stamp(db, slug)
            versions = list(
                db.scalars(
                    select(ContentVersion)
                    .where(
                        ContentVersion.entity == "legal_publication",
                        ContentVersion.object_key == slug,
                    )
                    .order_by(ContentVersion.number.desc())
                    .limit(20)
                )
            )
        title, html = draft["title"], draft["html"]
        error, preview = "", ""
        if request.method == "POST":
            title, html = request.form.get("title", ""), request.form.get("html", "")
            expected = request.form.get("expected", "")
            action = request.form.get("action", "")
            try:
                if action == "import":
                    file = request.files.get("document")
                    if not file or not (file.filename or "").lower().endswith(
                        (".html", ".htm", ".txt")
                    ):
                        raise ValueError("Выберите HTML-файл или .txt с HTML-разметкой в UTF-8.")
                    raw = file.stream.read(legal.MAX_BYTES + 1)
                    if len(raw) > legal.MAX_BYTES:
                        raise ValueError("Файл больше 100 КБ.")
                    try:
                        html = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        raise ValueError("Сохраните файл в кодировке UTF-8.") from None
                if action in {"preview", "import"}:
                    html = legal.clean_html(html)
                    preview = html
                elif action in {"draft", "publish"}:
                    confirmed()
                    with Session(self.engine) as db, db.begin():
                        legal.save(
                            db,
                            self.config,
                            g.owner_id,
                            slug,
                            title,
                            html,
                            expected,
                            publish=action == "publish",
                        )
                    flash(
                        "Документ опубликован."
                        if action == "publish"
                        else "Черновик сохранён. Публичная версия не изменилась.",
                        "success",
                    )
                    return redirect(self.url + "/edit?slug=" + slug)
                else:
                    abort(400)
            except ValueError as problem:
                error = str(problem)
        return self.page(
            "legal-editor.html",
            title="Редактор документа",
            slug=slug,
            document_title=title,
            html=html,
            expected=expected,
            error=error,
            preview=Markup(preview),
            live=live,
            versions=versions,
        )
