"""Private read-only agent history and literal prompt exports on the existing domain."""

from flask import Flask, abort, g, make_response, render_template, request
from sqlalchemy import Engine, or_, select
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from leedbox.application import dzen_history, writing_library
from leedbox.application.agents import operation, test_mode
from leedbox.application.cabinet_access import bot_id
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.content_agent import competitor_markdown
from leedbox.domain.content_agent import markdown as content_markdown
from leedbox.domain.dzen import article_prompt
from leedbox.domain.image_prompts import markdown
from leedbox.domain.lead_magnet_agent import markdown as lead_magnet_markdown
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AIOperation


def install_agent_reports(app: Flask, engine: Engine, config: Settings) -> None:
    @app.get("/cabinet/agents")
    def cabinet_agents() -> str:
        try:
            index = int(request.args.get("prompts_page", "0"))
            results_page = int(request.args.get("results_page", "0"))
            if not 0 <= index <= 100000 or not 0 <= results_page <= 100000:
                abort(400)
        except ValueError:
            abort(400)
        with Session(engine) as db, db.begin():
            writing_library.ensure(db, g.expert_owner, bot_id(config), test_mode(config))
            prompts = writing_library.page(
                db, g.expert_owner, bot_id(config), test_mode(config), index
            )
            rows = list(
                db.scalars(
                    select(AIOperation)
                    .where(
                        AIOperation.owner_user_id == g.expert_owner,
                        AIOperation.bot_id == bot_id(config),
                        AIOperation.test_mode == test_mode(config),
                        or_(
                            AIOperation.kind == "generation",
                            AIOperation.snapshot["workflow"].astext == "competitor",
                        ),
                        AIOperation.status == "completed",
                        AIOperation.result.is_not(None),
                    )
                    .order_by(AIOperation.id.desc())
                    .offset(results_page * 50)
                    .limit(51)
                )
            )
            return render_template(
                "cabinet/agents.html",
                rows=rows[:50],
                results_page=results_page,
                results_next=len(rows) > 50,
                searches=dzen_history.latest(db, g.expert_owner, bot_id(config), test_mode(config)),
                prompts=prompts[:7],
                prompts_page=index,
                prompts_next=len(prompts) > 7,
                csrf=g.expert_csrf,
                view="agents",
            )

    @app.get("/cabinet/agents/search/<token>")
    def cabinet_dzen_search(token: str) -> str:
        with Session(engine) as db:
            try:
                search = dzen_history.get(
                    db, g.expert_owner, bot_id(config), test_mode(config), token
                )
            except ObjectUnavailable:
                abort(404)
            results = [
                dzen_history.headlines(
                    db, g.expert_owner, bot_id(config), test_mode(config), token, post
                )
                for post in search.report.get("posts", [])
            ]
            return render_template(
                "cabinet/dzen-search.html",
                search=search,
                results=results,
                csrf=g.expert_csrf,
                view="agents",
            )

    @app.get("/cabinet/agents/<int:number>")
    def cabinet_agent_report(number: int) -> str:
        with Session(engine) as db:
            try:
                row = operation(db, g.expert_owner, bot_id(config), test_mode(config), number)
            except ObjectUnavailable:
                abort(404)
            result = (row.result or {}) if row.status == "completed" else {}
            return render_template(
                "cabinet/agent-report.html",
                row=row,
                result=result,
                csrf=g.expert_csrf,
                view="agents",
            )

    @app.get("/cabinet/agents/<int:number>/prompt")
    def cabinet_agent_prompt(number: int) -> Response:
        with Session(engine) as db:
            try:
                row = operation(db, g.expert_owner, bot_id(config), test_mode(config), number)
            except ObjectUnavailable:
                abort(404)
            if row.snapshot.get("workflow") == "competitor":
                if row.status != "completed" or not (row.result or {}).get("source_links"):
                    abort(404)
                response = make_response(competitor_markdown(row.result or {}))
                response.headers["Content-Type"] = "text/markdown; charset=utf-8"
                response.headers["Content-Disposition"] = (
                    'attachment; filename="competitor-links.md"'
                )
                return response
            if row.snapshot.get("purpose") in {"lead_magnet", "lead_magnet_revision"}:
                if row.status != "completed" or not (row.result or {}).get("lead_magnets"):
                    abort(404)
                response = make_response(lead_magnet_markdown(row.result or {}))
                response.headers["Content-Type"] = "text/markdown; charset=utf-8"
                response.headers["Content-Disposition"] = 'attachment; filename="lead-magnets.md"'
                return response
            if row.snapshot.get("purpose") == "content":
                result = row.result or {}
                if row.status != "completed" or not (
                    result.get("posts") or result.get("analysis_only")
                ):
                    abort(404)
                response = make_response(content_markdown(row.result or {}))
                response.headers["Content-Type"] = "text/markdown; charset=utf-8"
                filename = (
                    "content-analysis.md" if result.get("analysis_only") else "content-posts.md"
                )
                response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
                return response
            if row.snapshot.get("purpose") == "image_prompts":
                if row.status != "completed" or not (row.result or {}).get("images"):
                    abort(404)
                response = make_response(markdown(row.result or {}))
                response.headers["Content-Type"] = "text/markdown; charset=utf-8"
                response.headers["Content-Disposition"] = 'attachment; filename="image-prompts.md"'
                return response
            if row.snapshot.get("purpose") == "writing_profile":
                prompt = (row.result or {}).get("prompt")
                if row.status != "completed" or not prompt:
                    abort(404)
                response = make_response(prompt)
                response.headers["Content-Type"] = "text/markdown; charset=utf-8"
                response.headers["Content-Disposition"] = 'attachment; filename="my-dzen-prompt.md"'
                return response
            # Keep historic download links valid; new UI never creates three prompts.
            variant = request.args.get("variant", "")
            chosen = next(
                (v for v in (row.result or {}).get("variants", []) if v["id"] == variant), None
            )
            if chosen is None:
                abort(404)
            prompt = article_prompt(
                row.snapshot["article_template"], chosen["title"], row.snapshot["inputs"]
            )
            response = make_response(prompt)
            response.headers["Content-Type"] = "text/markdown; charset=utf-8"
            response.headers["Content-Disposition"] = (
                f'attachment; filename="dzen-prompt-{variant}.md"'
            )
            return response
