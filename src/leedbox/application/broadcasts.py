"""Explicit owner campaigns: immutable queued content and rechecked expert preferences."""

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.materials import http_url
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import (
    AuditLog,
    ExpertNotificationPreference,
    OutboxMessage,
    OwnerBroadcast,
    User,
    WizardDraft,
)

CONSENT_VERSION = "owner-ads-1"
CONSENT_TEXT = (
    "Я согласен получать рекламные сообщения владельца LeedBox в этом боте. "
    "Согласие добровольное, не влияет на доступ к инструментам и может быть отозвано "
    "в любой момент в разделе «Профиль → Уведомления» или кнопкой под сообщением."
)


def advertising_version(session: Session) -> str:
    from leedbox.application.legal import current

    doc = current(session, "advertising")
    return "legal:" + str(doc.id) if doc else CONSENT_VERSION


def configured_bot(config: Settings) -> int:
    return int(config.bot_token.get_secret_value().split(":", 1)[0])


def enroll(session: Session, user: User, bot_id: int) -> ExpertNotificationPreference:
    if user.expert_started_at is None:
        raise ObjectUnavailable
    session.execute(
        insert(ExpertNotificationPreference)
        .values(owner_user_id=user.id, bot_id=bot_id)
        .on_conflict_do_nothing()
    )
    row = session.get(ExpertNotificationPreference, (user.id, bot_id))
    assert row is not None
    return row


def preferences(session: Session, actor: int, bot_id: int) -> ExpertNotificationPreference:
    user = session.scalar(
        select(User).where(User.telegram_id == actor, User.expert_started_at.is_not(None))
    )
    if not user:
        raise ObjectUnavailable
    return enroll(session, user, bot_id)


def change_preference(
    session: Session,
    actor: int,
    bot_id: int,
    kind: str,
    enabled: bool,
    expected_version: str | None = None,
) -> None:
    if kind not in {"notice", "advertising"}:
        raise ValueError("Неизвестный тип уведомлений")
    preferences(session, actor, bot_id)
    version = advertising_version(session)
    if (
        kind == "advertising"
        and enabled
        and (
            (expected_version is not None and expected_version != version)
            or (version.startswith("legal:") and expected_version is None)
        )
    ):
        raise ValueError("Согласие на рекламу обновилось. Откройте уведомления заново.")
    row = session.scalar(
        select(ExpertNotificationPreference)
        .join(User)
        .where(User.telegram_id == actor, ExpertNotificationPreference.bot_id == bot_id)
        .with_for_update(of=ExpertNotificationPreference)
    )
    assert row is not None
    if kind == "notice":
        row.notices = enabled
    else:
        row.advertising = enabled
        if enabled:
            row.advertising_granted_at = datetime.now(UTC)
            row.consent_version = version
        else:
            row.advertising_revoked_at = datetime.now(UTC)
    session.add(
        AuditLog(
            action="expert_notifications_changed",
            details={
                "kind": kind,
                "enabled": enabled,
                "consent_version": version if kind == "advertising" and enabled else None,
            },
            actor_telegram_id=actor,
        )
    )


def validate_payload(values: dict[str, Any], kind: str) -> dict[str, str]:
    if kind not in {"notice", "advertising"}:
        raise ValueError("Выберите новость или рекламу")
    fields = {
        "text": 3200,
        "button_label": 40,
        "button_url": 2000,
        "advertiser": 200,
        "inn": 12,
        "erid": 128,
        "marking_text": 100,
    }
    result = {}
    for key, limit in fields.items():
        value = values.get(key, "")
        if (
            not isinstance(value, str)
            or len(value.strip()) > limit
            or any(ord(c) < 32 and c not in "\n\r\t" for c in value)
        ):
            raise ValueError(f"Поле {key}: максимум {limit} символов, без служебных знаков")
        result[key] = value.strip()
    if not result["text"]:
        raise ValueError("Напишите текст сообщения")
    if bool(result["button_label"]) != bool(result["button_url"]):
        raise ValueError("Для кнопки заполните и подпись, и ссылку")
    if result["button_url"]:
        result["button_url"] = http_url(result["button_url"])
    if kind == "advertising":
        if (
            not result["advertiser"]
            or not re.fullmatch(r"(?:[0-9]{10}|[0-9]{12})", result["inn"])
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result["erid"])
        ):
            raise ValueError("Для рекламы нужны рекламодатель, ИНН (10 или 12 цифр) и erid")
        if "реклама" not in result["marking_text"].casefold():
            raise ValueError("Пометка должна содержать слово «Реклама»")
    else:
        result.update(advertiser="", inn="", erid="", marking_text="")
    return result


def message_text(kind: str, payload: dict[str, Any]) -> str:
    data = validate_payload(payload, kind)
    text = data["text"]
    if kind == "advertising":
        text += (
            f"\n\n{data['marking_text']}\nРекламодатель: {data['advertiser']}"
            f"\nИНН: {data['inn']}\nerid: {data['erid']}"
        )
    text += "\n\n— Владелец LeedBox"
    if len(text.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Сообщение с маркировкой слишком длинное для Telegram. Сократите текст")
    return text


def campaign(
    session: Session, config: Settings, actor: int, id: int, locked: bool = False
) -> OwnerBroadcast:
    if not config.is_owner(actor):
        raise PermissionError("Owner access required")
    query = select(OwnerBroadcast).where(
        OwnerBroadcast.id == id, OwnerBroadcast.bot_id == configured_bot(config)
    )
    row = session.scalar(query.with_for_update() if locked else query)
    if row is None:
        raise ObjectUnavailable
    return row


def save_campaign(
    session: Session,
    config: Settings,
    actor: int,
    title: str,
    kind: str,
    payload: dict[str, Any],
    id: int | None = None,
    expected: int | None = None,
) -> OwnerBroadcast:
    if not config.is_owner(actor):
        raise PermissionError("Owner access required")
    title = title.strip()
    if not 1 <= len(title) <= 100:
        raise ValueError("Название: от 1 до 100 символов")
    values = validate_payload(payload, kind)
    message_text(kind, values)
    if id is None:
        row = OwnerBroadcast(
            bot_id=configured_bot(config),
            title=title,
            kind=kind,
            payload=values,
            status="draft",
            revision=1,
        )
        session.add(row)
    else:
        row = campaign(session, config, actor, id, True)
        if row.status != "draft" or row.revision != expected:
            raise ValueError("Рассылка изменилась или уже запущена. Откройте её заново")
        row.title, row.kind, row.payload = title, kind, values
        row.revision += 1
    session.flush()
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="broadcast_saved",
            details={"id": row.id, "revision": row.revision, "kind": kind},
        )
    )
    return row


def audience_query(bot_id: int, kind: str, version: str | None = None) -> Any:
    enabled = (
        ExpertNotificationPreference.advertising
        if kind == "advertising"
        else ExpertNotificationPreference.notices
    )
    return (
        select(User.telegram_id)
        .join(ExpertNotificationPreference)
        .where(
            User.expert_started_at.is_not(None),
            ExpertNotificationPreference.bot_id == bot_id,
            enabled.is_(True),
            *(
                [ExpertNotificationPreference.consent_version == version]
                if kind == "advertising" and version
                else []
            ),
        )
    )


def audience_count(session: Session, bot_id: int, kind: str) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(
                audience_query(bot_id, kind, advertising_version(session)).subquery()
            )
        )
        or 0
    )


def enqueue(session: Session, config: Settings, actor: int, id: int, expected: int) -> int:
    row = campaign(session, config, actor, id, True)
    if row.status == "queued":
        return int(
            session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.broadcast_id == id)
            )
            or 0
        )
    if row.status != "draft" or row.revision != expected:
        raise ValueError("Рассылка изменилась. Проверьте предпросмотр заново")
    text = message_text(row.kind, row.payload)
    # Bound transaction/memory: INSERT SELECT snapshots recipients, not a Python ID list.
    from sqlalchemy import String, cast, literal

    query = audience_query(row.bot_id, row.kind, advertising_version(session)).with_only_columns(
        literal(row.bot_id),
        User.telegram_id,
        literal(id),
        literal("owner_broadcast"),
        literal(text),
        literal(f"broadcast:{id}:") + cast(User.telegram_id, String),
        literal("pending"),
        literal(0),
    )
    session.execute(
        insert(OutboxMessage)
        .from_select(
            [
                "bot_id",
                "telegram_user_id",
                "broadcast_id",
                "purpose",
                "text",
                "dedupe_key",
                "status",
                "attempts",
            ],
            query,
        )
        .on_conflict_do_nothing(index_elements=[OutboxMessage.dedupe_key])
    )
    count = int(
        session.scalar(
            select(func.count()).select_from(OutboxMessage).where(OutboxMessage.broadcast_id == id)
        )
        or 0
    )
    row.status, row.queued_at, row.revision = "queued", datetime.now(UTC), row.revision + 1
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="broadcast_queued",
            details={"id": id, "recipients": count, "kind": row.kind},
        )
    )
    return count


def cancel(session: Session, config: Settings, actor: int, id: int) -> None:
    row = campaign(session, config, actor, id, True)
    row.status = "cancelled"
    row.revision += 1
    session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.broadcast_id == id, OutboxMessage.status == "pending")
        .values(status="cancelled")
    )
    session.add(AuditLog(actor_telegram_id=actor, action="broadcast_cancelled", details={"id": id}))


def can_deliver(session: Session, row: OutboxMessage) -> bool:
    item = session.get(OwnerBroadcast, row.broadcast_id) if row.broadcast_id else None
    if not item or item.bot_id != row.bot_id or item.status != "queued":
        return False
    allowed = session.scalar(
        audience_query(row.bot_id, item.kind, advertising_version(session)).where(
            User.telegram_id == row.telegram_user_id
        )
    )
    draft = session.get(
        WizardDraft, f"{row.bot_id}:{row.telegram_user_id}:{row.telegram_user_id}:None:None:default"
    )
    return allowed is not None and not (draft and draft.data.get("mode") == "recipient")


def counts(session: Session, id: int) -> dict[str, int]:
    return {
        status: count
        for status, count in session.execute(
            select(OutboxMessage.status, func.count())
            .where(OutboxMessage.broadcast_id == id)
            .group_by(OutboxMessage.status)
        ).all()
    }
