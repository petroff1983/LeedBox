"""Shared-domain expert cabinet, independent of owner administration."""

import hashlib
import hmac
from secrets import token_urlsafe
from typing import Any
from urllib.parse import urlsplit

from flask import (
    Flask,
    abort,
    g,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.wrappers import Response

from leedbox.application.audience_dashboard import PAGE_SIZE, Dashboard, period_for
from leedbox.application.cabinet_access import (
    SESSION_SECONDS,
    available,
    bot_id,
    consume_ticket,
    digest,
    session_owner,
)
from leedbox.application.limits import take
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import CabinetCredential

COOKIE = "leedbox_expert"
NONCE_COOKIE = "leedbox_expert_login"


def install_cabinet(app: Flask, engine: Engine, settings: Settings) -> None:
    secret = settings.web_secret_key.get_secret_value().encode()
    if not settings.admin_enabled and settings.trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0)  # type: ignore[method-assign]

    def signature(purpose: str, value: str) -> str:
        return hmac.new(secret, f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()

    def set_cookie(response: Response, name: str, value: str, seconds: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=seconds,
            httponly=True,
            secure=settings.environment == "production",
            samesite="Lax",
            path="/cabinet",
        )

    def login_page(error: str = "", status: int = 200) -> Response:
        nonce = token_urlsafe(32)
        response = make_response(
            render_template(
                "cabinet/login.html",
                error=error,
                csrf=signature("login", nonce),
                next_page="agents" if request.values.get("next") == "agents" else "",
                report=request.values.get("report", "")
                if request.values.get("report", "").isdigit()
                and len(request.values.get("report", "")) < 19
                else "",
            ),
            status,
        )
        set_cookie(response, NONCE_COOKIE, nonce, 600)
        return response

    @app.before_request
    def guard_cabinet() -> Response | None:
        if not (request.path == "/cabinet" or request.path.startswith("/cabinet/")):
            return None
        if not available(settings):
            return login_page("Кабинет пока не настроен владельцем сервиса.", 503)
        host = urlsplit(request.url_root).hostname
        allowed_hosts = (
            {urlsplit(settings.public_base_url).hostname} if settings.public_base_url else set()
        )
        if settings.environment != "production":
            allowed_hosts.update({"localhost", "127.0.0.1", "::1"})
        if host not in allowed_hosts:
            abort(400)
        if not request.is_secure and (
            settings.environment == "production"
            or urlsplit(request.url_root).hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            abort(403)
        if request.path == "/cabinet/login":
            return None
        raw = request.cookies.get(COOKIE, "")
        with Session(engine) as db:
            owner = session_owner(db, settings, raw)
        if owner is None:
            return redirect(url_for("cabinet_login"))
        g.expert_owner = owner
        from leedbox.application.agents import test_mode
        from leedbox.application.ai_tokens import available as ai_available

        with Session(engine) as db:
            g.ai_balance = ai_available(db, owner, bot_id(settings), test_mode(settings))
            from leedbox.application.credits import enabled

            g.ai_credit_mode = enabled(db)
        g.expert_csrf = signature("session", raw)
        if request.method == "POST":
            values = request.form.getlist("csrf")
            if len(values) != 1 or not hmac.compare_digest(values[0], g.expert_csrf):
                abort(403)
        return None

    @app.after_request
    def cabinet_headers(response: Response) -> Response:
        if request.path == "/cabinet" or request.path.startswith("/cabinet/"):
            response.headers.update(
                {
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "X-Robots-Tag": "noindex, nofollow",
                    "Content-Security-Policy": (
                        "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; "
                        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                    ),
                }
            )
            if settings.environment == "production" and request.is_secure:
                response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.route("/cabinet/login", methods=["GET", "POST"])
    def cabinet_login() -> Response:
        if request.method == "GET":
            return login_page()
        nonce, values, tokens = (
            request.cookies.get(NONCE_COOKIE, ""),
            request.form.getlist("csrf"),
            request.form.getlist("token"),
        )
        if (
            len(nonce) != 43
            or len(values) != 1
            or len(tokens) != 1
            or not hmac.compare_digest(values[0], signature("login", nonce))
        ):
            return login_page("Ссылка входа не подтверждена. Откройте новую ссылку из бота.", 403)
        with Session(engine) as db, db.begin():
            ip_key = signature("ip", request.remote_addr or "unknown")
            if not take(db, "cabinet-login:" + ip_key, 15, 900):
                return login_page("Слишком много попыток. Повторите вход через 15 минут.", 429)
            try:
                raw = consume_ticket(db, settings, tokens[0])
            except ObjectUnavailable:
                return login_page(
                    "Ссылка истекла или уже использована. Нажмите «Моя аудитория» в боте ещё раз.",
                    401,
                )
        response = redirect(
            url_for("cabinet_agents" if request.form.get("next") == "agents" else "cabinet_index"),
            code=303,
        )
        report = request.form.get("report", "")
        if request.form.get("next") == "agents" and report.isdigit() and len(report) < 19:
            response = redirect(url_for("cabinet_agent_report", number=int(report)), code=303)
        set_cookie(response, COOKIE, raw, SESSION_SECONDS)
        response.delete_cookie(NONCE_COOKIE, path="/cabinet")
        return response

    @app.post("/cabinet/logout")
    def cabinet_logout() -> Response:
        with Session(engine) as db, db.begin():
            db.execute(
                delete(CabinetCredential).where(
                    CabinetCredential.token_hash == digest(request.cookies.get(COOKIE, "")),
                    CabinetCredential.owner_user_id == g.expert_owner,
                    CabinetCredential.bot_id == bot_id(settings),
                    CabinetCredential.kind == "session",
                )
            )
        response = redirect(url_for("cabinet_login"), code=303)
        response.delete_cookie(COOKIE, path="/cabinet")
        return response

    @app.get("/cabinet/")
    def cabinet_index() -> Response:
        view = request.args.get("view", "overview")
        if view not in {"overview", "materials", "sources", "people"}:
            abort(404)
        period = request.args.get("period", "30")
        params = {
            key: request.args.get(key, "") for key in ("start", "end", "material", "source", "q")
        }
        sort = request.args.get("sort", "people")
        error = ""
        try:
            selected_period = period_for(period, params["start"], params["end"])
            page = int(request.args.get("page", "0"))
            material = int(params["material"]) if params["material"] else None
            if (
                not 0 <= page <= 10000
                or material is not None
                and not 0 < material < 2**63
                or len(params["source"]) > 200
                or len(params["q"]) > 19
            ):
                raise ValueError("Некорректный фильтр. Сбросьте фильтры и повторите выбор")
        except ValueError:
            error, page, material = (
                "Некорректный фильтр. Сбросьте фильтры и повторите выбор",
                0,
                None,
            )
            period, selected_period = "30", period_for("30")
            params = {key: "" for key in params}
        with Session(engine) as db:
            try:
                dashboard = Dashboard(
                    db, g.expert_owner, bot_id(settings), selected_period, material
                )
                options, platforms = dashboard.options()
                if params["source"] and params["source"] not in platforms:
                    raise ValueError("Выберите источник из списка")
                rows, top_materials, top_sources, summary = [], [], [], {}
                if view == "overview":
                    summary = dashboard.summary()
                    top_materials = dashboard.materials(sort, limit=5)[:5]
                    top_sources = dashboard.sources(limit=5)[:5]
                elif view == "materials":
                    rows = dashboard.materials(sort, page)
                elif view == "sources":
                    rows = dashboard.sources(params["source"], sort, page)
                else:
                    rows = dashboard.people(params["source"], params["q"], page)
            except ObjectUnavailable:
                abort(404)
            except ValueError as exc:
                error, rows, top_materials, top_sources, summary = str(exc), [], [], [], {}

        def link(**changes: Any) -> str:
            values: dict[str, Any] = {"view": view, "period": period, "sort": sort, **params}
            values.update(changes)
            return url_for(
                "cabinet_index", **{key: value for key, value in values.items() if value != ""}
            )

        return make_response(
            render_template(
                "cabinet/index.html",
                view=view,
                period=period,
                selected_period=selected_period,
                params=params,
                sort=sort,
                page=page,
                error=error,
                options=options,
                platforms=platforms,
                summary=summary,
                rows=rows[:PAGE_SIZE],
                has_next=len(rows) > PAGE_SIZE,
                top_materials=top_materials,
                top_sources=top_sources,
                csrf=g.expert_csrf,
                link=link,
            )
        )

    from leedbox.web.agents import install_agent_reports

    install_agent_reports(app, engine, settings)
