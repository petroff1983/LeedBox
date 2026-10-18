"""Validated owner content restoration, never raw model mass assignment."""

from decimal import Decimal
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import configure_policy, policy, policy_data
from leedbox.application.partners import PartnerInput, save_partner
from leedbox.application.profile import update_profile
from leedbox.application.projects import set_archived
from leedbox.application.services import owner_required, save_service
from leedbox.application.settings import set_setting, setting
from leedbox.application.ui_texts import read_text, set_text
from leedbox.application.versions import fingerprint, lock_content, record_version
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.services import ServiceInput
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    AdminMutation,
    AuditLog,
    ContentVersion,
    Partner,
    Project,
    Service,
)


def save_project_metadata(
    session: Session, config: Settings, actor: int, id: int, values: dict[str, Any], expected: int
) -> None:
    owner_required(config, actor)
    if (
        set(values) != {"name", "timezone", "contact", "archived"}
        or type(values["archived"]) is not bool
    ):
        raise ValueError("Проверьте поля проекта")
    if any(not isinstance(values[key], str) for key in ("name", "timezone", "contact")):
        raise ValueError("Проверьте текстовые поля проекта")
    if not 0 < id <= 2**63 - 1:
        raise ObjectUnavailable
    project = session.scalar(
        select(Project)
        .where(Project.id == id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ObjectUnavailable
    if project.revision != expected:
        raise ValueError("Проект изменился. Откройте новую форму")
    before = {
        "name": project.name,
        "timezone": project.timezone,
        "contact": project.public_contact or "-",
        "archived": project.archived,
    }
    if project.archived:
        set_archived(session, project.owner_user_id, id, project.revision, False)
    for key in ("name", "timezone", "contact"):
        update_profile(session, project.owner_user_id, id, key, values[key], project.revision)
    set_archived(session, project.owner_user_id, id, project.revision, values["archived"])
    after = {
        "name": project.name,
        "timezone": project.timezone,
        "contact": project.public_contact or "-",
        "archived": project.archived,
    }
    record_version(session, "project", str(id), actor, after, before)
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="owner_project_changed",
            subject_user_id=project.owner_user_id,
            details={"id": id, "revision": project.revision},
        )
    )


def create_catalog_entry(
    session: Session,
    config: Settings,
    actor: int,
    kind: str,
    payload: ServiceInput | PartnerInput,
    action_key: str,
) -> int:
    owner_required(config, actor)
    if len(action_key) != 32 or any(char not in "0123456789abcdef" for char in action_key):
        raise ValueError("Обновите форму перед созданием карточки")
    fields = payload.validated()
    key = fingerprint([actor, kind, action_key])
    lock_content(session, "catalog_create", key)
    receipt = session.get(AdminMutation, key)
    digest = fingerprint(fields)
    if receipt:
        if receipt.payload_hash != digest:
            raise ValueError("Эта форма уже сохранена. Откройте карточку для изменения")
        return receipt.object_id
    if kind == "service" and isinstance(payload, ServiceInput):
        id = save_service(session, config, actor, payload).id
    elif kind == "partner" and isinstance(payload, PartnerInput):
        id = save_partner(session, config, actor, payload).id
    else:
        raise ValueError("Неизвестный каталог")
    session.add(AdminMutation(key=key, payload_hash=digest, entity=kind, object_id=id))
    session.flush()
    return id


def current_fingerprint(session: Session, entity: str, key: str) -> str:
    if entity == "setting":
        return fingerprint(setting(session, key))
    if entity == "ui":
        return fingerprint(read_text(session, key))
    if entity == "policy":
        return fingerprint(policy_data(policy(session)))
    if entity == "project":
        if not key.isascii() or not key.isdecimal() or not 0 < int(key) <= 2**63 - 1:
            raise ObjectUnavailable
        project = session.get(Project, int(key))
        if project is None:
            raise ObjectUnavailable
        return str(project.revision)
    from leedbox.application.content import CATALOGS

    if entity in CATALOGS:
        if not key.isascii() or not key.isdecimal() or not 0 < int(key) <= 2**63 - 1:
            raise ObjectUnavailable
        catalog = session.get(CATALOGS[entity], int(key))
        if catalog is None:
            raise ObjectUnavailable
        return str(catalog.revision)
    model = Service if entity == "service" else Partner if entity == "partner" else None
    if model is None or not key.isdecimal():
        raise ObjectUnavailable
    row = cast(Service | Partner | None, session.get(model, int(key)))
    if row is None:
        raise ObjectUnavailable
    return str(row.revision)


def save_policy_data(
    session: Session, config: Settings, actor: int, values: dict[str, Any], expected: str
) -> None:
    configure_policy(
        session,
        config,
        actor,
        mode=str(values["mode"]),
        price=Decimal(str(values["price"])),
        currency=str(values["currency"]),
        period_days=int(values["period_days"]),
        trial_days=int(values["trial_days"]),
        grace_days=int(values["grace_days"]),
        activation_grace_days=int(values["activation_grace_days"]),
        expired_delivery=str(values["expired_delivery"]),
        hide_for_paid=bool(values["hide_for_paid"]),
        expected=expected,
    )


def restore_version(
    session: Session, config: Settings, actor: int, version_id: int, expected: str
) -> None:
    owner_required(config, actor)
    version = session.get(ContentVersion, version_id)
    if version is None:
        raise ObjectUnavailable
    fields = version.data
    if version.entity == "setting":
        set_setting(session, config, actor, version.object_key, fields["value"], expected)
    elif version.entity == "ui":
        set_text(session, config, actor, version.object_key, fields["value"], expected)
    elif version.entity == "service":
        save_service(
            session, config, actor, ServiceInput(**fields), int(version.object_key), int(expected)
        )
    elif version.entity == "partner":
        save_partner(
            session, config, actor, PartnerInput(**fields), int(version.object_key), int(expected)
        )
    elif version.entity == "policy":
        save_policy_data(session, config, actor, fields, expected)
    elif version.entity == "project":
        save_project_metadata(
            session, config, actor, int(version.object_key), fields, int(expected)
        )
    elif version.entity in {
        "niche",
        "idea",
        "template",
        "objection",
        "topic",
        "category",
        "article",
        "recommendation",
    }:
        from leedbox.application.content import save_catalog

        save_catalog(
            session, config, actor, version.entity, fields, int(version.object_key), int(expected)
        )
    else:
        raise ObjectUnavailable
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="content_restored",
            details={
                "version_id": version.id,
                "entity": version.entity,
                "object_key": version.object_key,
            },
        )
    )
