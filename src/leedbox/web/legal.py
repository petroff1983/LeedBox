"""Public script-free legal publications, independent from admin authentication."""

from flask import Flask, Response, abort, make_response, render_template
from markupsafe import Markup
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from leedbox.application import legal


def install_legal(app: Flask, engine: Engine) -> None:
    def response(template: str, **values: object) -> Response:
        result = make_response(render_template(template, **values))
        result.headers["Cache-Control"] = "no-store"
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'; object-src 'none'"
        )
        return result

    @app.get("/legal")
    @app.get("/legal/")
    def legal_index() -> Response:
        with Session(engine) as db:
            docs = [
                {"slug": slug, "title": doc.data["title"], "number": doc.number}
                for slug in legal.DOCUMENTS
                if (doc := legal.current(db, slug))
            ]
        return response("legal/index.html", docs=docs, title="Документы LeedBox")

    @app.get("/legal/<slug>")
    @app.get("/legal/<slug>/v/<int:number>")
    def legal_page(slug: str, number: int | None = None) -> Response:
        if slug not in legal.DOCUMENTS:
            abort(404)
        with Session(engine) as db:
            doc = legal.version(db, slug, number) if number is not None else legal.current(db, slug)
            if doc is None:
                abort(404)
            values = dict(
                title=doc.data["title"],
                body=Markup(legal.clean_html(doc.data["html"])),
                version=doc.number,
                published_at=doc.created_at,
                archived=number is not None,
            )
        return response("legal/document.html", **values)
