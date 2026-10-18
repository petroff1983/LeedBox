"""Existing expert menu sections; hiding never deletes data or stops gift delivery."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.infrastructure.models import Setting

SECTIONS = {
    "materials": "🎁 Лид-магниты",
    "calc": "🧮 Расчёты",
    "links": "🔗 Ссылки",
    "texts": "✍️ Промпты",
    "services": "🛍 Товары и услуги",
    "profile": "👤 Профиль",
    "consultations": "🎓 Консультации и обучение",
    "audience": "👥 Моя аудитория",
    "youtube": "📊 Анализ YouTube (в разделе «Агенты»)",
    "agents": "🤖 Агенты",
}
VISIBILITY_DEFAULTS = {f"section_{code}_visible": True for code in SECTIONS}


def visibility(session: Session) -> dict[str, bool]:
    overrides = {
        row.key: row.value
        for row in session.scalars(select(Setting).where(Setting.key.in_(VISIBILITY_DEFAULTS)))
    }
    return {code: overrides.get(f"section_{code}_visible", True) is True for code in SECTIONS}


def visible_sections(session: Session) -> set[str]:
    shown = {code for code, enabled in visibility(session).items() if enabled}
    # YouTube is a child of Agents. Keep its individual switch, but never let
    # old buttons or /youtube bypass a hidden parent section.
    if "agents" not in shown:
        shown.discard("youtube")
    row = session.get(Setting, "wallet_visible")
    if row and row.value is True:
        shown.add("wallet")
    return shown
