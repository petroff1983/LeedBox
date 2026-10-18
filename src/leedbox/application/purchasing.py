"""Purchase offers: trusted prices, frozen terms and explicit buyer acceptance."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from leedbox.application.entitlements import policy, policy_data
from leedbox.application.payments import create_order, service_checkout
from leedbox.application.services import owner_request
from leedbox.application.settings import setting
from leedbox.application.versions import fingerprint, lock_content
from leedbox.domain.access import ObjectUnavailable
from leedbox.domain.payments import InvalidPayment, validate_receipt
from leedbox.domain.services import owner_contact_url
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AuditLog, PaymentOrder, ServiceRequest, User


def purchase_terms(session: Session, config: Settings | None = None) -> dict[str, str]:
    text = str(setting(session, "payment_terms")).strip()
    from leedbox.application import legal

    offer = legal.current(session, "offer") if config else None
    if offer and config:
        text += (
            ("\n\n" if text else "")
            + "Договор-оферта: "
            + legal.public_url(config, "offer", offer.number)
        )
    try:
        contact = owner_contact_url(str(setting(session, "owner_contact")))
    except ValueError:
        raise InvalidPayment("Контакт поддержки настроен неверно. Обратитесь к владельцу") from None
    if not text or not contact:
        raise InvalidPayment("Условия покупки или контакт поддержки пока не настроены")
    return {"terms": text, "support": contact, "terms_version": fingerprint([text, contact])}


def access_offer(session: Session, config: Settings, bot_id: int) -> dict[str, Any]:
    current = policy(session)
    price = setting(session, "stars_access_price")
    if not config.stars_enabled or current.mode != "paid" or type(price) is not int or price <= 0:
        raise InvalidPayment("Покупка доступа пока недоступна. Обратитесь к владельцу")
    return {
        **purchase_terms(session, config),
        "price": price,
        "period_days": current.period_days,
        "policy_version": fingerprint(policy_data(current)),
        "bot_id": bot_id,
        "test_mode": config.telegram_test_api,
    }


def buy_access(
    session: Session,
    config: Settings,
    owner_id: int,
    bot_id: int,
    offer: dict[str, Any],
    action_key: str,
    accepted: bool,
) -> PaymentOrder:
    if accepted is not True:
        raise InvalidPayment("Для покупки необходимо согласие с условиями")
    lock_content(session, "purchase_access", f"{bot_id}:{action_key}")
    # An already committed confirmation remains idempotent after a price/terms change.
    old = session.scalar(
        select(PaymentOrder).where(
            PaymentOrder.bot_id == bot_id, PaymentOrder.action_key == action_key
        )
    )
    if old is not None:
        if (
            old.owner_user_id != owner_id
            or old.kind != "access"
            or old.snapshot.get("offer") != offer
        ):
            raise ObjectUnavailable
        return old
    if access_offer(session, config, bot_id) != offer:
        raise InvalidPayment("Цена или условия изменились. Откройте покупку заново")
    return create_order(
        session,
        owner_id,
        bot_id,
        kind="access",
        provider="stars",
        account=str(bot_id),
        test_mode=config.telegram_test_api,
        amount=Decimal(offer["price"]),
        period_days=offer["period_days"],
        snapshot={
            "terms_version": offer["terms_version"],
            "offer": offer,
            "accepted": True,
            "accepted_at": datetime.now(UTC).isoformat(),
        },
        action_key=action_key,
    )


def buyer_order(session: Session, telegram_id: int, bot_id: int, number: int) -> PaymentOrder:
    row = session.scalar(
        select(PaymentOrder)
        .join(User)
        .where(
            PaymentOrder.id == number,
            PaymentOrder.bot_id == bot_id,
            User.telegram_id == telegram_id,
        )
    )
    if row is None:
        raise ObjectUnavailable
    return row


def quote_request(
    session: Session,
    config: Settings,
    actor: int,
    bot_id: int,
    request_id: int,
    amount: Decimal,
    receipt: str,
    category: str,
    action_key: str,
) -> PaymentOrder:
    request = owner_request(session, config, actor, bot_id, request_id)
    if (
        not config.robokassa_enabled
        or category != "physical_service"
        or request.consent_revoked_at is not None
        or request.status == "closed"
    ):
        raise InvalidPayment("Счёт недоступен для этой заявки или категории услуги")
    terms = purchase_terms(session, config)
    validate_receipt(receipt, amount)
    return create_order(
        session,
        request.owner_user_id,
        bot_id,
        kind="service",
        provider="robokassa",
        account=config.robokassa_merchant,
        test_mode=config.robokassa_test,
        amount=amount,
        period_days=0,
        snapshot={
            **terms,
            "request_id": request.id,
            "items": request.items,
            "category": category,
            "receipt": receipt,
        },
        action_key=action_key,
    )


def accept_service_checkout(
    session: Session,
    config: Settings,
    telegram_id: int,
    bot_id: int,
    number: int,
    terms_version: str,
    accepted: bool,
) -> str:
    order = buyer_order(session, telegram_id, bot_id, number)
    if (
        accepted is not True
        or order.snapshot.get("category") != "physical_service"
        or order.snapshot.get("terms_version") != terms_version
        or not order.snapshot.get("terms")
        or not order.snapshot.get("support")
    ):
        raise InvalidPayment("Проверьте условия покупки перед переходом к оплате")
    request = owner_request_for_checkout(session, order)
    if request.consent_revoked_at is not None or request.status == "closed":
        raise InvalidPayment("Заявка закрыта или согласие на её обработку отозвано")
    assert order.owner_user_id is not None
    url = service_checkout(session, config, order.owner_user_id, bot_id, number)
    if len(url) > 4096:
        if not config.public_base_url:
            raise InvalidPayment("Для большого чека нужен настроенный HTTPS-адрес сервиса")
        from leedbox.domain.payment_handoff import ticket

        url = (
            config.public_base_url
            + "/payments/robokassa/checkout/"
            + ticket(number, config.bot_token.get_secret_value())
        )
    session.add(
        AuditLog(
            action="purchase_terms_accepted",
            actor_telegram_id=telegram_id,
            subject_user_id=order.owner_user_id,
            details={"order_id": number, "terms_version": terms_version},
        )
    )
    return url


def owner_request_for_checkout(session: Session, order: PaymentOrder) -> ServiceRequest:
    row = session.get(ServiceRequest, order.snapshot.get("request_id"))
    if row is None or row.owner_user_id != order.owner_user_id or row.bot_id != order.bot_id:
        raise ObjectUnavailable
    return row
