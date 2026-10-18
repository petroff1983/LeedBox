"""Validated current templates, copied into each durable questionnaire."""

from sqlalchemy.orm import Session

from leedbox.application.settings import setting
from leedbox.domain import ai_prompts


def current_template(session: Session, tool: str) -> str:
    body = setting(session, "prompt_template_" + tool)
    try:
        return ai_prompts.validate_template(tool, body)
    except ValueError:
        # A manually corrupted database override must not break prompt generation.
        return ai_prompts.default_template(tool)
