import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from leedbox.application.projects import get_project
from leedbox.infrastructure.models import (
    AIAccount,
    AIOperation,
    AuditLog,
    Consent,
    ContentVersion,
    DeliveryEvent,
    JoinEvent,
    LegalConsentEvent,
    LinkEvent,
    OutboxMessage,
    PaymentInbox,
    PaymentOrder,
    ProductEvent,
    Project,
    RateBucket,
    Recipient,
    Setting,
    User,
    WalletAccount,
    WizardDraft,
)


def update_profile(
    session: Session,
    owner_id: int,
    project_id: int,
    field: str,
    value: str,
    expected: int | None = None,
) -> None:
    project = get_project(session, owner_id, project_id, locked=True)
    if project.archived or (expected is not None and project.revision != expected):
        raise ValueError("Проект изменился или архивирован. Откройте его заново")
    value = value.strip()
    if field == "contact":
        value = value.removeprefix("@")
        if value != "-" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
            raise ValueError("Введите Telegram-имя без ссылки или «-», чтобы удалить контакт")
        project.public_contact = None if value == "-" else value
    elif field == "timezone":
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Введите часовой пояс IANA, например Europe/Moscow") from None
        if len(value) > 64:
            raise ValueError("Некорректный часовой пояс")
        project.timezone = value
    elif field == "name":
        if not 1 <= len(value) <= 100:
            raise ValueError("Название: от 1 до 100 символов")
        project.name = value
    else:
        raise ValueError("Unknown project field")
    project.revision += 1


def delete_my_data(session: Session, telegram_id: int) -> None:
    user = session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
    if user and (
        session.scalar(
            select(AIAccount.owner_user_id)
            .where(
                AIAccount.owner_user_id == user.id,
                (AIAccount.balance != 0) | (AIAccount.credit_balance != 0),
            )
            .limit(1)
        )
        or session.scalar(
            select(WalletAccount.owner_user_id)
            .where(WalletAccount.owner_user_id == user.id, WalletAccount.balance != 0)
            .limit(1)
        )
        or session.scalar(
            select(PaymentOrder.id)
            .where(
                PaymentOrder.owner_user_id == user.id,
                PaymentOrder.kind == "topup",
                PaymentOrder.status == "pending",
            )
            .limit(1)
        )
    ):
        raise ValueError(
            "Сначала обратитесь в поддержку: есть остаток баланса или незавершённое пополнение"
        )
    # Also remove recipient-side personal data without touching other recipients/materials.
    session.execute(
        delete(LegalConsentEvent).where(LegalConsentEvent.telegram_user_id == telegram_id)
    )
    for model in (Consent, DeliveryEvent, Recipient, OutboxMessage):
        field = model.telegram_user_id
        session.execute(delete(model).where(field == telegram_id))
    session.execute(
        update(ProductEvent)
        .where(ProductEvent.telegram_user_id == telegram_id)
        .values(telegram_user_id=None)
    )
    session.execute(
        update(PaymentInbox)
        .where(PaymentInbox.telegram_user_id == telegram_id)
        .values(telegram_user_id=None)
    )
    session.execute(delete(LinkEvent).where(LinkEvent.telegram_user_id == telegram_id))
    session.execute(delete(JoinEvent).where(JoinEvent.telegram_user_id == telegram_id))
    # Match exact chat/user segments, not substring IDs.
    for row in session.scalars(select(WizardDraft)):
        parts = row.key.split(":")
        if len(parts) >= 3 and parts[2] == str(telegram_id):
            session.delete(row)
    if user:
        session.execute(
            update(AIOperation)
            .where(AIOperation.owner_user_id == user.id)
            .values(snapshot={}, result=None)
        )
        session.execute(delete(RateBucket).where(RateBucket.key.like(f"yt:user:%:{user.id}")))
        project_ids = [
            str(id)
            for id in session.scalars(select(Project.id).where(Project.owner_user_id == user.id))
        ]
        session.execute(
            delete(ContentVersion).where(
                ContentVersion.entity == "project", ContentVersion.object_key.in_(project_ids)
            )
        )
        session.execute(delete(Setting).where(Setting.key == f"access_state:{user.id}"))
        session.add(AuditLog(action="user_data_deleted", details={"scope": "self"}))
        session.delete(user)
