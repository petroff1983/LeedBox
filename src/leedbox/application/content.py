"""Validated, versioned owner catalogs and frozen inputs for text tools."""

import json
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.services import owner_required
from leedbox.application.versions import fingerprint, lock_content, record_version
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.content import validate_catalog
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    AdminMutation,
    Article,
    ArticleCategory,
    AuditLog,
    ContentCatalog,
    FunnelTopic,
    LeadMagnetIdea,
    Niche,
    NicheSuggestion,
    Objection,
    Recommendation,
    Template,
)

CATALOGS: dict[str, type[ContentCatalog]] = {
    "niche": Niche,
    "idea": LeadMagnetIdea,
    "template": Template,
    "objection": Objection,
    "topic": FunnelTopic,
    "category": ArticleCategory,
    "article": Article,
    "recommendation": Recommendation,
}


def snapshot(row: ContentCatalog) -> dict[str, Any]:
    return {
        "id": row.id,
        "revision": row.revision,
        "slug": row.slug,
        "title": row.title,
        "payload": deepcopy(row.payload),
        "enabled": row.enabled,
    }


def save_catalog(
    session: Session,
    config: Settings,
    actor: int,
    kind: str,
    values: dict[str, Any],
    id: int | None = None,
    expected: int = 0,
    action_key: str = "",
) -> ContentCatalog:
    owner_required(config, actor)
    if kind not in CATALOGS:
        raise ObjectUnavailable
    try:
        if len(json.dumps(values, ensure_ascii=False)) > 60000:
            raise ValueError("Карточка слишком большая")
        fields = validate_catalog(kind, values)
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Проверьте структуру полей каталога") from None
    model = CATALOGS[kind]
    lock_content(session, kind, fields["slug"])
    before = None
    receipt_key = ""
    if kind == "template" and fields["enabled"]:
        # Serialize publication as well as individual edits: one active version per model.
        lock_content(session, "template_publication", fields["payload"]["tool"])
    if id is not None:
        if not 0 < id <= 2**63 - 1:
            raise ObjectUnavailable
        row = session.scalar(select(model).where(model.id == id).with_for_update())
        if row is None:
            raise ObjectUnavailable
        if row.slug != fields["slug"] or row.revision != expected:
            raise ValueError("Карточка изменилась. Код существующей карточки менять нельзя")
        before = {key: snapshot(row)[key] for key in fields}
    else:
        if (
            expected != 0
            or len(action_key) != 32
            or any(c not in "0123456789abcdef" for c in action_key)
        ):
            raise ValueError("Откройте новую форму создания")
        receipt_key = fingerprint([actor, kind, action_key])
        lock_content(session, "content_create", receipt_key)
        receipt = session.get(AdminMutation, receipt_key)
        if receipt:
            if receipt.payload_hash != fingerprint(fields):
                raise ValueError("Эта форма уже сохранена")
            row = session.get(model, receipt.object_id)
            if row is None:
                raise ObjectUnavailable
            return row
        if session.scalar(select(model.id).where(model.slug == fields["slug"])) is not None:
            raise ValueError("Такой код уже есть в каталоге")
        row = model()
    if kind == "template" and fields["enabled"]:
        with session.no_autoflush:
            for active in session.scalars(select(Template).where(Template.enabled.is_(True))):
                if active.id == id or active.payload["tool"] != fields["payload"]["tool"]:
                    continue
                if fields["payload"]["tool"] == "offer" and (
                    active.slug.startswith("offer-v2-") != fields["slug"].startswith("offer-v2-")
                ):
                    # The new questionnaire and legacy landing drafts use different families.
                    continue
                if (
                    fields["payload"]["tool"] == "landing"
                    or active.payload["model"] == fields["payload"]["model"]
                ):
                    raise ValueError(
                        "Для этой модели уже опубликован шаблон. Сначала снимите его с публикации"
                    )
    if kind == "idea" and fields["payload"]["niche"] is not None:
        niche = session.scalar(select(Niche).where(Niche.slug == fields["payload"]["niche"]))
        if niche is None:
            raise ValueError("Сначала создайте нишу; для универсальной идеи niche = null")
    if kind in {"article", "recommendation"}:
        from leedbox.infrastructure.models import Partner

        if kind == "article" and (
            session.scalar(
                select(ArticleCategory.id).where(
                    ArticleCategory.slug == fields["payload"]["category"]
                )
            )
            is None
        ):
            raise ValueError("Сначала создайте категорию")
        for partner_id in fields["payload"]["partner_ids"]:
            if session.get(Partner, partner_id) is None:
                raise ValueError("Партнёр не найден")
    for key, value in fields.items():
        setattr(row, key, value)
    if before is not None:
        row.revision += 1
    session.add(row)
    session.flush()
    if receipt_key:
        session.add(
            AdminMutation(
                key=receipt_key, payload_hash=fingerprint(fields), entity=kind, object_id=row.id
            )
        )
    record_version(session, kind, str(row.id), actor, fields, before)
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="catalog_changed",
            details={"kind": kind, "id": row.id, "revision": row.revision},
        )
    )
    return row


def tool_catalog(session: Session, tool: str) -> list[dict[str, Any]]:
    if tool == "sequence":
        tool = "funnel"
    if tool == "ideas":
        entries = session.scalars(
            select(LeadMagnetIdea)
            .where(LeadMagnetIdea.enabled.is_(True))
            .order_by(LeadMagnetIdea.id)
        )
        active_niches = set(session.scalars(select(Niche.slug).where(Niche.enabled.is_(True))))
        return [
            snapshot(row)
            for row in entries
            if row.payload["niche"] is None or row.payload["niche"] in active_niches
        ]
    if tool not in {"offer", "landing", "case", "proposal", "funnel", "funnel_example"}:
        raise ObjectUnavailable
    return [
        snapshot(row)
        for row in session.scalars(
            select(Template).where(Template.enabled.is_(True)).order_by(Template.id)
        )
        if row.payload["tool"] == tool
    ]


def suggest_niche(session: Session, owner_id: int, name: str) -> None:
    from leedbox.domain.content import text

    name = text(name, 100)
    session.execute(
        insert(NicheSuggestion)
        .values(owner_user_id=owner_id, name=name, status="new")
        .on_conflict_do_nothing(index_elements=["owner_user_id", "name"])
    )


def catalog_version(catalog: list[dict[str, Any]]) -> str:
    return "content-" + fingerprint(catalog)[:32]
