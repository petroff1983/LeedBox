import logging
from datetime import UTC, datetime
from uuid import uuid4

from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request
from sqlalchemy import Engine, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response as RedirectResponse

from leedbox import __version__
from leedbox.application.monetization import resolve_redirect
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import http_url
from leedbox.domain.smart_links import app_target
from leedbox.infrastructure.banner_assets import MAX_BANNER_BYTES
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.database import create_database_engine
from leedbox.infrastructure.models import LinkEvent, TrackedLink
from leedbox.web.admin import install_admin
from leedbox.web.auth import install_auth
from leedbox.web.cabinet import install_cabinet
from leedbox.web.legal import install_legal
from leedbox.web.payments import install_payments

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> Flask:
    settings = settings or Settings()
    database = engine if engine is not None else create_database_engine(settings)
    app = Flask(__name__)
    app.extensions["leedbox_database"] = database
    app.config["MAX_CONTENT_LENGTH"] = 65536

    @app.before_request
    def banner_upload_limit() -> None:
        if settings.admin_enabled and request.path == settings.admin_path + "/legal/edit":
            request.max_content_length = 200_000
        # Only the owner media form accepts files; login/payments retain their limits.
        if (
            settings.admin_enabled
            and request.path == settings.admin_path + "/settings/edit"
            and request.args.get("key") == "main_menu_banner"
        ):
            request.max_content_length = MAX_BANNER_BYTES + 65536

    install_payments(app, database, settings)
    install_legal(app, database)
    if settings.admin_enabled:
        install_auth(app, database, settings)
        install_admin(app, database, settings)
    install_cabinet(app, database, settings)

    @app.get("/go/<code>")
    def go(code: str) -> RedirectResponse:
        if (
            len(code) == 13
            and code[0] in {"a", "p"}
            and all(char in "0123456789abcdef" for char in code[1:])
        ):
            try:
                with Session(database) as session, session.begin():
                    target = resolve_redirect(session, code, request.method == "GET")
            except (ObjectUnavailable, ValueError):
                abort(404)
            response = redirect(target, code=302)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
        if len(code) != 12 or any(char not in "0123456789abcdef" for char in code):
            abort(404)
        with Session(database) as session, session.begin():
            link = session.scalar(
                select(TrackedLink)
                .where(
                    TrackedLink.code == code,
                    TrackedLink.enabled.is_(True),
                    TrackedLink.status == "active",
                    or_(
                        TrackedLink.expires_at.is_(None), TrackedLink.expires_at > datetime.now(UTC)
                    ),
                )
                .with_for_update()
            )
            if link is None:
                abort(404)
            target = http_url(link.destination)
            if request.method == "GET":
                session.add(LinkEvent(link_id=link.id, kind="click"))
        response = redirect(target, code=302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/d/<code>")
    def deeplink(code: str) -> Response:
        if len(code) != 12 or any(char not in "0123456789abcdef" for char in code):
            abort(404)
        with Session(database) as session, session.begin():
            link = session.scalar(
                select(TrackedLink)
                .where(
                    TrackedLink.code == code,
                    TrackedLink.kind == "deeplink",
                    TrackedLink.enabled.is_(True),
                    TrackedLink.status == "active",
                    or_(
                        TrackedLink.expires_at.is_(None), TrackedLink.expires_at > datetime.now(UTC)
                    ),
                )
                .with_for_update()
            )
            if link is None:
                abort(404)
            try:
                target = app_target(link.destination)
            except ValueError:
                abort(404)
            if request.method == "GET":
                session.add(LinkEvent(link_id=link.id, kind="click"))
        response = make_response(render_template("links/deeplink.html", target=target))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; script-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
        return response

    @app.get("/health")
    def health() -> Response:
        return jsonify(status="ok", version=__version__)

    @app.get("/ready")
    def ready() -> tuple[Response, int]:
        try:
            with database.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            event_id = uuid4().hex
            logger.error("database_unavailable", extra={"event_id": event_id})
            return jsonify(status="unavailable", event_id=event_id), 503
        return jsonify(status="ready"), 200

    @app.errorhandler(Exception)
    def handle_error(error: Exception) -> tuple[Response, int]:
        if isinstance(error, HTTPException):
            return jsonify(status="error", code=error.code), error.code or 500
        event_id = uuid4().hex
        logger.error("request_failed", extra={"event_id": event_id})
        return jsonify(status="error", event_id=event_id), 500

    return app
