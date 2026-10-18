"""Catalog ownership, scoped requests, explicit consent, transactional notifications."""

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.projects import get_project
from leedbox.application.settings import product_event, setting
from leedbox.application.versions import record_version
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.links import text_field
from leedbox.domain.services import STATUS_LABELS, ServiceInput, request_contact, service_text
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AuditLog, OutboxMessage, Service, ServiceRequest, User


def owner_required(config: Settings, actor: int) -> None:
    if not config.is_owner(actor):
        raise PermissionError("Owner access required")


def save_service(
    session: Session,
    config: Settings,
    actor: int,
    values: ServiceInput,
    service_id: int | None = None,
    expected_revision: int | None = None,
) -> Service:
    owner_required(config, actor)
    fields = values.validated()
    row = (
        session.scalar(select(Service).where(Service.id == service_id).with_for_update())
        if service_id
        else None
    )
    if service_id and row is None:
        raise ObjectUnavailable
    if row and row.revision != expected_revision:
        raise ValueError("Услуга изменилась. Откройте её заново")
    before = {key: getattr(row, key) for key in fields} if row else None
    if row is None:
        row = Service(**fields)
        session.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
        row.revision += 1
    session.flush()
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="service_saved",
            details={
                "service_id": row.id,
                "revision": row.revision,
                "before": before,
                "after": fields,
            },
        )
    )
    record_version(session, "service", str(row.id), actor, fields, before)
    return row


def catalog(session: Session, page: int = 0) -> list[Service]:
    if not 0 <= page <= 10000:
        raise ValueError("Некорректная страница")
    if not setting(session, "services_enabled"):
        return []
    return list(
        session.scalars(
            select(Service)
            .where(Service.enabled.is_(True))
            .order_by(Service.sort_order, Service.id)
            .offset(page * 5)
            .limit(6)
        )
    )


def selected_services(session: Session, ids: list[int]) -> list[Service]:
    if not setting(session, "services_enabled"):
        raise ValueError("Приём заявок временно отключён")
    if not ids or len(ids) > 10 or len(set(ids)) != len(ids):
        raise ValueError("Выберите от одной до десяти услуг")
    rows = list(
        session.scalars(
            select(Service)
            .where(Service.id.in_(ids), Service.enabled.is_(True))
            .order_by(Service.id)
            .with_for_update()
        )
    )
    if len(rows) != len(ids):
        raise ValueError("Выбранная услуга больше недоступна. Обновите выбор")
    return rows


def service_snapshot(row: Service) -> dict[str, Any]:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "revision",
            "title",
            "audience",
            "description",
            "duration",
            "price_label",
            "executor_details",
            "conditions",
        )
    }


def consent_document(session: Session, config: Settings) -> tuple[str, str]:
    details = str(setting(session, "owner_details")).strip()
    text = str(setting(session, "service_consent_text")).strip()
    if not config.owner_ids or not details or not text:
        raise ValueError("Приём заявок ещё не настроен владельцем")
    if len(details) > 500 or len(text) > 1500:
        raise ValueError("Текст согласия требует корректировки владельцем")
    full = f"Получатель данных: {details}\n\n{text}"
    return sha256(full.encode()).hexdigest(), full


def submit_request(
    session: Session,
    config: Settings,
    owner_id: int,
    project_id: int,
    bot_id: int,
    ids: list[int],
    revisions: dict[str, int],
    contact: str,
    comment: str,
    consent: bool,
    consent_version: str,
    action_key: str,
) -> ServiceRequest:
    user = session.scalar(select(User).where(User.id == owner_id).with_for_update())
    if not user or user.expert_started_at is None:
        raise ObjectUnavailable
    existing = session.scalar(select(ServiceRequest).where(ServiceRequest.action_key == action_key))
    if existing:
        if existing.owner_user_id != owner_id or existing.bot_id != bot_id:
            raise ObjectUnavailable
        return existing
    project = get_project(session, owner_id, project_id)
    if project.archived:
        raise ObjectUnavailable
    if consent is not True:
        raise ValueError("Для отправки нужно отдельное согласие на передачу данных")
    version, text = consent_document(session, config)
    if consent_version != version:
        raise ValueError("Текст согласия изменился. Прочитайте и подтвердите заново")
    rows = selected_services(session, ids)
    if any(revisions.get(str(row.id)) != row.revision for row in rows):
        raise ValueError("Условия услуги изменились. Обновите выбор и подтвердите заново")
    comment = service_text(comment, 1000, required=False)
    record = ServiceRequest(
        owner_user_id=owner_id,
        project_id=project_id,
        bot_id=bot_id,
        items=[service_snapshot(row) for row in rows],
        contact=request_contact(contact),
        comment=comment,
        action_key=text_field(action_key, limit=100),
        consent_version=version,
        consent_text=text,
        consent_granted_at=datetime.now(UTC),
    )
    session.add(record)
    session.flush()
    for actor in sorted(config.owner_ids):
        session.add(
            OutboxMessage(
                bot_id=bot_id,
                telegram_user_id=actor,
                service_request_id=record.id,
                purpose="service_request",
                text=f"Новая заявка на услуги №{record.id}. Откройте карточку для обработки.",
                dedupe_key=f"service_request:{record.id}:{actor}",
            )
        )
    product_event(session, bot_id, user.telegram_id, "service_requested")
    session.add(
        AuditLog(
            action="service_consent_granted",
            subject_user_id=owner_id,
            details={"request_id": record.id, "version": version, "purpose": "service_request"},
        )
    )
    return record


def get_request(
    session: Session, bot_id: int, request_id: int, owner_id: int | None = None
) -> ServiceRequest:
    query = select(ServiceRequest).where(
        ServiceRequest.id == request_id, ServiceRequest.bot_id == bot_id
    )
    if owner_id is not None:
        query = query.where(ServiceRequest.owner_user_id == owner_id)
    row = session.scalar(query)
    if row is None:
        raise ObjectUnavailable
    return row


def owner_request(
    session: Session, config: Settings, actor: int, bot_id: int, request_id: int
) -> ServiceRequest:
    owner_required(config, actor)
    return get_request(session, bot_id, request_id)


def change_status(
    session: Session,
    config: Settings,
    actor: int,
    bot_id: int,
    request_id: int,
    expected_revision: int,
    target: str,
) -> ServiceRequest:
    owner_required(config, actor)
    row = session.scalar(
        select(ServiceRequest)
        .where(ServiceRequest.id == request_id, ServiceRequest.bot_id == bot_id)
        .with_for_update()
    )
    if row is None:
        raise ObjectUnavailable
    if row.status == target:
        return row
    if row.revision != expected_revision:
        raise ValueError("Заявка изменилась. Откройте карточку заново")
    if (row.status, target) not in {("new", "working"), ("new", "closed"), ("working", "closed")}:
        raise ValueError("Недопустимый переход статуса")
    previous = row.status
    row.status, row.revision = target, row.revision + 1
    session.add(
        AuditLog(
            actor_telegram_id=actor,
            action="service_status_changed",
            subject_user_id=row.owner_user_id,
            details={"request_id": row.id, "before": previous, "after": target},
        )
    )
    product_event(session, bot_id, None, "service_status_changed", previous=previous, state=target)
    return row


def request_card(row: ServiceRequest) -> str:
    names = "\n".join(f"• {item['title']} — {item['price_label']}" for item in row.items)
    return (
        f"Заявка №{row.id} · {STATUS_LABELS[row.status]}\n\n{names}\n\n"
        f"Контакт: {row.contact}\nКомментарий: {row.comment or '—'}\n\n"
        "Оплата вне бота, после согласования."
    )


def withdraw_consent(session: Session, bot_id: int, request_id: int, owner_id: int) -> None:
    row = session.scalar(
        select(ServiceRequest)
        .where(
            ServiceRequest.id == request_id,
            ServiceRequest.bot_id == bot_id,
            ServiceRequest.owner_user_id == owner_id,
        )
        .with_for_update()
    )
    if row is None:
        raise ObjectUnavailable
    if row.consent_revoked_at is not None:
        return
    row.consent_revoked_at = datetime.now(UTC)
    row.revision += 1
    from sqlalchemy import update

    session.execute(
        update(OutboxMessage)
        .where(OutboxMessage.service_request_id == row.id, OutboxMessage.status == "pending")
        .values(status="cancelled")
    )
    session.add(
        AuditLog(
            action="service_consent_revoked",
            subject_user_id=owner_id,
            details={"request_id": row.id, "purpose": "service_request"},
        )
    )
