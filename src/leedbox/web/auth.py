"""Permanent password login, database sessions and CSRF for owner operations."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, redirect, render_template, request, session, url_for
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.wrappers import Response as WerkzeugResponse

from leedbox.application.admin_credentials import HASH_METHOD, change
from leedbox.application.limits import take
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AdminAccount, AdminSession, AuditLog


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def install_auth(app: Flask, engine: Engine, config: Settings) -> None:
    root = config.admin_path
    app.secret_key = config.web_secret_key.get_secret_value()
    dummy_hash = generate_password_hash(secrets.token_urlsafe(32), method=HASH_METHOD)
    app.config.update(
        SESSION_COOKIE_NAME="leedbox_owner",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.environment == "production",
        SESSION_COOKIE_PATH=root,
        MAX_CONTENT_LENGTH=65536,
    )
    if config.public_base_url:
        app.config["TRUSTED_HOSTS"] = [urlsplit(config.public_base_url).hostname] + (
            ["localhost", "127.0.0.1", "::1"] if config.environment != "production" else []
        )
    if config.trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)  # type: ignore[method-assign]

    def csrf_valid() -> bool:
        values = request.form.getlist("csrf_token")
        csrf = session.get("csrf", "")
        return (
            len(values) == 1
            and isinstance(csrf, str)
            and bool(csrf)
            and hmac.compare_digest(values[0], csrf)
            and all(len(request.form.getlist(key)) == 1 for key in request.form)
        )

    def attempts(db: Session, scope: str, limit: int) -> bool:
        # Do not trust X-Forwarded-For. Global bound remains effective behind proxies.
        peer = hmac.new(
            config.web_secret_key.get_secret_value().encode(),
            (request.remote_addr or "unknown").encode(),
            hashlib.sha256,
        ).hexdigest()
        globally = take(db, "admin:" + scope + ":global", limit, 900)
        locally = take(db, "admin:" + scope + ":peer:" + peer, limit, 900)
        return globally and locally

    @app.before_request
    def guard() -> WerkzeugResponse | None:
        if request.path != root and not request.path.startswith(root + "/"):
            return None
        g.csp_nonce = secrets.token_urlsafe(24)
        local = config.environment != "production" and request.host.split(":")[0] in {
            "localhost",
            "127.0.0.1",
        }
        if not request.is_secure and not local:
            abort(403)
        if request.path in {root + "/login", root + "/callback"} or request.path.startswith(
            root + "/static/"
        ):
            return None
        sid = session.get("sid", "")
        with Session(engine) as db:
            row = db.get(AdminSession, token_hash(sid)) if isinstance(sid, str) and sid else None
            account = db.get(AdminAccount, 1) if row else None
            if (
                not row
                or row.expires_at <= datetime.now(UTC)
                or not account
                or row.telegram_id != account.telegram_id
                or session.get("credential_revision") != account.revision
                or not config.is_owner(account.telegram_id)
            ):
                session.clear()
                if request.method != "GET":
                    abort(401)
                return redirect(url_for("owner_login"))
            g.owner_id = account.telegram_id
            g.credential_revision = account.revision
            if account.must_change and request.path not in {root + "/security", root + "/logout"}:
                return redirect(url_for("owner_security"))
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not csrf_valid():
            abort(403)
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        if request.path == root or request.path.startswith(root + "/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            nonce = getattr(g, "csp_nonce", "")
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self'; img-src 'self' data:; font-src 'self'; "
                "frame-src 'none'; form-action 'self'"
            )
            if config.environment == "production" and request.is_secure:
                response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.route(root + "/login", methods=["GET", "POST"])
    def owner_login() -> str | tuple[str, int] | WerkzeugResponse:
        error, status = "", 200
        login = ""
        if request.method == "GET":
            session.clear()
            session["csrf"] = secrets.token_urlsafe(32)
        else:
            if not csrf_valid() or set(request.form) - {"csrf_token", "username", "password"}:
                abort(403)
            raw_login = request.form.get("username", "")
            login = raw_login[:64].strip().lower()
            secret = request.form.get("password", "")
            now = datetime.now(UTC)
            with Session(engine) as db, db.begin():
                permitted = attempts(db, "login", 10)
                if not permitted:
                    error, status = "Слишком много попыток. Повторите вход через 15 минут.", 429
                else:
                    account = db.scalar(
                        select(AdminAccount).where(AdminAccount.id == 1).with_for_update()
                    )
                    valid = (
                        len(raw_login) <= 64
                        and 1 <= len(secret) <= 128
                        and check_password_hash(
                            account.password_hash if account else dummy_hash, secret
                        )
                    )
                    if (
                        not account
                        or not valid
                        or account.username != login
                        or not config.is_owner(account.telegram_id)
                    ):
                        error, status = "Неверный логин или пароль.", 401
                        db.add(AuditLog(action="admin_login_failed", details={}))
                    else:
                        db.execute(delete(AdminSession).where(AdminSession.expires_at <= now))
                        sid = secrets.token_urlsafe(48)
                        db.add(
                            AdminSession(
                                token_hash=token_hash(sid),
                                telegram_id=account.telegram_id,
                                expires_at=now + timedelta(seconds=config.admin_session_seconds),
                            )
                        )
                        db.add(
                            AuditLog(
                                actor_telegram_id=account.telegram_id,
                                action="admin_logged_in",
                                details={"method": "password"},
                            )
                        )
                        revision = account.revision
                        must_change = account.must_change
            if not error:
                session.clear()
                session["sid"] = sid
                session["csrf"] = secrets.token_urlsafe(32)
                session["credential_revision"] = revision
                return redirect(root + ("/security" if must_change else "/"))
        return render_template(
            "owner/login.html",
            csrf=session.get("csrf", ""),
            username=login,
            error=error,
            changed=request.args.get("changed") == "1",
        ), status

    @app.get(root + "/callback")
    def retired_telegram_callback() -> tuple[Response, int]:
        # No alternate authentication path or legacy Telegram session acceptance.
        return app.response_class("Telegram Login отключён", mimetype="text/plain"), 410

    @app.route(root + "/security", methods=["GET", "POST"])
    def owner_security() -> str | WerkzeugResponse:
        error = ""
        with Session(engine) as db:
            account = db.get(AdminAccount, 1)
            assert account is not None
            login = account.username
            revision = account.revision
            initial = account.must_change
        if request.method == "POST":
            login = request.form.get("username", "")
            with Session(engine) as db, db.begin():
                permitted = attempts(db, "credentials", 10)
                if not permitted:
                    error = "Слишком много попыток. Повторите через 15 минут."
                else:
                    try:
                        if request.form.get("confirmed") != "1" or set(request.form) - {
                            "csrf_token",
                            "username",
                            "current_password",
                            "new_password",
                            "confirmation",
                            "revision",
                            "confirmed",
                        }:
                            raise ValueError("Подтвердите смену учётных данных")
                        raw_revision = request.form.get("revision", "")
                        if (
                            not raw_revision.isascii()
                            or not raw_revision.isdecimal()
                            or len(raw_revision) > 10
                        ):
                            raise ValueError("Устаревшая форма. Откройте раздел заново")
                        expected = int(raw_revision)
                        with db.begin_nested():
                            change(
                                db,
                                g.owner_id,
                                expected,
                                request.form.get("current_password", ""),
                                login,
                                request.form.get("new_password", ""),
                                request.form.get("confirmation", ""),
                            )
                    except ValueError as exc:
                        error = str(exc)
            if not error:
                session.clear()
                return redirect(root + "/login?changed=1")
        return cast(
            str,
            app.extensions["leedbox_admin"].index_view.render(
                "owner/security.html",
                root=root,
                csrf=session.get("csrf", ""),
                username=login,
                revision=revision,
                initial=initial,
                error=error,
            ),
        )

    @app.post(root + "/logout")
    def owner_logout() -> WerkzeugResponse:
        with Session(engine) as db, db.begin():
            db.execute(
                delete(AdminSession).where(
                    AdminSession.token_hash == token_hash(session.get("sid", ""))
                )
            )
            db.add(AuditLog(actor_telegram_id=g.owner_id, action="admin_logged_out", details={}))
        session.clear()
        return redirect(url_for("owner_login"))
