from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.materials import owned_material
from leedbox.application.settings import setting
from leedbox.domain.access import ObjectUnavailable
from leedbox.infrastructure.models import AuditLog, Consent, Material, OutboxMessage, Recipient


def consent_for(
    session: Session,
    bot_id: int,
    material_id: int,
    user_id: int,
    granted: bool,
    now: datetime | None = None,
    expected_text_version: str | None = None,
) -> None:
    now = now or datetime.now(UTC)
    material = session.get(Material, material_id)
    if material is None or material.bot_id != bot_id:
        raise ObjectUnavailable
    if not session.scalar(
        select(Recipient.id).where(
            Recipient.material_id == material_id, Recipient.telegram_user_id == user_id
        )
    ):
        raise ObjectUnavailable
    key = f"author:{material_id}:{user_id}"
    existing = session.scalar(select(Consent).where(Consent.scope_key == key).with_for_update())
    if not granted:
        if existing and existing.revoked_at is None:
            existing.revoked_at = now
            session.add(
                AuditLog(
                    actor_telegram_id=user_id,
                    action="consent_revoked",
                    details={
                        "scope": key,
                        "purpose": "author_updates",
                        "text_version": existing.text_version,
                    },
                )
            )
        return
    version = str(setting(session, "author_consent_version"))
    if expected_text_version is not None and expected_text_version != version:
        raise ValueError("Текст согласия изменился. Откройте согласие заново")
    if existing and existing.revoked_at is None and existing.text_version == version:
        return
    session.add(
        AuditLog(
            actor_telegram_id=user_id,
            action="consent_granted",
            details={
                "scope": key,
                "purpose": "author_updates",
                "text_version": version,
            },
        )
    )
    session.execute(
        insert(Consent)
        .values(
            scope_key=key,
            purpose="author_updates",
            owner_user_id=material.owner_user_id,
            material_id=material_id,
            telegram_user_id=user_id,
            text_version=version,
            granted_at=now,
        )
        .on_conflict_do_update(
            index_elements=[Consent.scope_key],
            set_={"text_version": version, "granted_at": now, "revoked_at": None},
        )
    )


def queue_update(
    session: Session,
    owner_id: int,
    bot_id: int,
    material_id: int,
    expected_version: int,
    username: str,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(UTC)
    material = owned_material(session, owner_id, material_id)
    if (
        material.bot_id != bot_id
        or material.archived
        or material.current_version != expected_version
    ):
        raise ObjectUnavailable
    days = int(setting(session, "notification_interval_days"))
    if material.last_notified_at and now < material.last_notified_at + timedelta(days=days):
        raise ValueError(f"Уведомлять можно не чаще одного раза в {days} дней")
    consents = session.scalars(
        select(Consent).where(
            Consent.material_id == material_id,
            Consent.purpose == "author_updates",
            Consent.revoked_at.is_(None),
        )
    ).all()
    count = 0
    for consent in consents:
        row = session.scalar(
            insert(OutboxMessage)
            .values(
                bot_id=bot_id,
                telegram_user_id=consent.telegram_user_id,
                material_id=material_id,
                purpose="author_updates",
                text=f"Автор обновил материал.\nhttps://t.me/{username}?start={material.slug}",
                dedupe_key=f"update:{material_id}:{expected_version}:{consent.telegram_user_id}",
            )
            .on_conflict_do_nothing(index_elements=[OutboxMessage.dedupe_key])
            .returning(OutboxMessage.id)
        )
        count += int(row is not None)
    if count:
        material.last_notified_at = now
    return count
