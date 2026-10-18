from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from leedbox.infrastructure.database import Base


class LegalConsentEvent(Base):
    __tablename__ = "legal_consent_events"
    __table_args__ = (
        UniqueConstraint("bot_id", "telegram_user_id", "action_key"),
        CheckConstraint("action IN ('grant', 'revoke')", name="legal_consent_action"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    purpose: Mapped[str] = mapped_column(String(24))
    action: Mapped[str] = mapped_column(String(8))
    version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("content_versions.id", ondelete="RESTRICT")
    )
    policy_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("content_versions.id", ondelete="RESTRICT")
    )
    action_key: Mapped[str] = mapped_column(String(100))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(32))
    expert_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CabinetCredential(Base):
    __tablename__ = "cabinet_credentials"
    __table_args__ = (
        CheckConstraint("kind IN ('login', 'session')", name="cabinet_credential_kind"),
    )
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DzenSearch(Base):
    __tablename__ = "dzen_searches"
    __table_args__ = (UniqueConstraint("owner_user_id", "bot_id", "test_mode", "token"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    test_mode: Mapped[bool] = mapped_column(Boolean)
    token: Mapped[str] = mapped_column(String(32))
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WritingPrompt(Base):
    __tablename__ = "writing_prompts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    test_mode: Mapped[bool] = mapped_column(Boolean)
    operation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ai_operations.id", ondelete="CASCADE"), unique=True
    )
    name: Mapped[str] = mapped_column(String(80))
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AIAccount(Base):
    __tablename__ = "ai_accounts"
    __table_args__ = (
        CheckConstraint("balance >= 0 AND reserved >= 0 AND reserved <= balance", name="ai_funds"),
        CheckConstraint(
            "credit_balance >= 0 AND credit_reserved >= 0 AND credit_reserved <= credit_balance "
            "AND credit_value_rub >= 0",
            name="ai_credit_funds",
        ),
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    test_mode: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    unlimited: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    free_limits: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    credit_balance: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    credit_reserved: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    credit_value_rub: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0"), server_default="0"
    )


class AIOperation(Base):
    __tablename__ = "ai_operations"
    __table_args__ = (
        UniqueConstraint("bot_id", "action_key"),
        CheckConstraint(
            "status IN ('reserved', 'sent', 'completed', 'uncertain', 'failed')", name="ai_status"
        ),
        CheckConstraint("kind IN ('generation', 'purchase', 'grant', 'search')", name="ai_kind"),
        CheckConstraint(
            "billing_mode IN ('balance', 'trial', 'unlimited')", name="ai_billing_mode"
        ),
        CheckConstraint(
            "reserve_tokens >= 0 AND input_tokens >= 0 AND output_tokens >= 0", name="ai_usage"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    test_mode: Mapped[bool] = mapped_column(Boolean)
    action_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12))
    model: Mapped[str] = mapped_column(String(80))
    reserve_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    billing_mode: Mapped[str] = mapped_column(
        String(12), default="balance", server_default="balance"
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    balance_after: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=Decimal("0"))
    cost_rub: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    charged_credits: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    revenue_rub: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0"), server_default="0"
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContentTask(Base):
    __tablename__ = "content_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','queued','working','waiting',"
            "'completed','failed','uncertain','cancelled')",
            name="content_task_status",
        ),
    )
    token: Mapped[str] = mapped_column(String(24), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    test_mode: Mapped[bool] = mapped_column(Boolean)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), default="draft", index=True)
    stage: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    provider: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    operation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("ai_operations.id"))
    search_operation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ai_operations.id")
    )
    screen_id: Mapped[int | None] = mapped_column(BigInteger)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    ready_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ContentCatalog(Base):
    __abstract__ = True
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class Niche(ContentCatalog):
    __tablename__ = "niches"


class LeadMagnetIdea(ContentCatalog):
    __tablename__ = "lead_magnet_ideas"


class Template(ContentCatalog):
    __tablename__ = "templates"


class Objection(ContentCatalog):
    __tablename__ = "objections"


class FunnelTopic(ContentCatalog):
    __tablename__ = "funnel_topics"


class ArticleCategory(ContentCatalog):
    __tablename__ = "article_categories"


class Article(ContentCatalog):
    __tablename__ = "articles"


class Recommendation(ContentCatalog):
    __tablename__ = "recommendations"


class RecommendationPreference(Base):
    __tablename__ = "recommendation_preferences"
    __table_args__ = (UniqueConstraint("owner_user_id", "bot_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    session_key: Mapped[str] = mapped_column(String(32))
    last_activity: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed: Mapped[bool] = mapped_column(Boolean, default=False)


class ServiceRedirect(Base):
    __tablename__ = "service_redirects"
    __table_args__ = (UniqueConstraint("bot_id", "kind", "entity_id"),)
    code: Mapped[str] = mapped_column(String(13), primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))
    entity_id: Mapped[int] = mapped_column(BigInteger)


class RecommendationEvent(Base):
    __tablename__ = "recommendation_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    partner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("partners.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(15))
    event_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PersonalObjection(Base):
    __tablename__ = "personal_objections"
    __table_args__ = (UniqueConstraint("owner_user_id", "objection_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    objection_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("objections.id", ondelete="CASCADE")
    )
    answer: Mapped[str] = mapped_column(String(1000))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class NicheSuggestion(Base):
    __tablename__ = "niche_suggestions"
    __table_args__ = (UniqueConstraint("owner_user_id", "name"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("id", "owner_user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    public_contact: Mapped[str | None] = mapped_column(String(32))
    brief: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    creation_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectSelection(Base):
    __tablename__ = "project_selections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["projects.id", "projects.owner_user_id"],
            ondelete="CASCADE",
            name="fk_selection_project_owner",
        ),
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[int] = mapped_column(BigInteger)


class WizardDraft(Base):
    __tablename__ = "wizard_drafts"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(100))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("id", "owner_user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(255))
    join_url: Mapped[str] = mapped_column(Text)
    bot_rights: Mapped[str] = mapped_column(String(15), default="unknown")
    rights_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    membership_observed_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    membership_last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    membership_gap: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Material(Base):
    __tablename__ = "materials"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id", name="uq_material_owner"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["projects.id", "projects.owner_user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "owner_user_id"],
            ["channels.id", "channels.owner_user_id"],
        ),
        CheckConstraint("delivery_mode IN ('subscribers', 'everyone')", name="material_mode"),
        CheckConstraint(
            "delivery_mode != 'subscribers' OR channel_id IS NOT NULL",
            name="material_channel_required",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    project_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    publication_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    delivery_mode: Mapped[str] = mapped_column(String(20))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MaterialVersion(Base):
    __tablename__ = "material_versions"
    __table_args__ = (
        UniqueConstraint("material_id", "number"),
        CheckConstraint(
            "content_type IN ('text', 'url', 'document', 'photo', 'video')",
            name="material_content_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    material_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("materials.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(300), default="")
    content_type: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeliveryEvent(Base):
    __tablename__ = "delivery_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'uncertain')", name="delivery_status"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    material_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("materials.id", ondelete="CASCADE"), index=True
    )
    version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("material_versions.id", ondelete="CASCADE")
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    action_key: Mapped[str] = mapped_column(String(150), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    subscription_bypassed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessPolicy(Base):
    __tablename__ = "access_policy"
    __table_args__ = (
        CheckConstraint("id = 1", name="access_policy_singleton"),
        CheckConstraint("mode IN ('free', 'paid')", name="access_policy_mode"),
        CheckConstraint("price >= 0", name="access_price_positive"),
        CheckConstraint(
            "trial_days >= 0 AND grace_days >= 0 "
            "AND activation_grace_days >= 0 AND period_days > 0",
            name="access_days",
        ),
        CheckConstraint(
            "expired_delivery IN ('bypass', 'check', 'deny')", name="access_expired_delivery"
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mode: Mapped[str] = mapped_column(String(10), default="free")
    price: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    period_days: Mapped[int] = mapped_column(Integer, default=30)
    trial_days: Mapped[int] = mapped_column(Integer, default=90)
    grace_days: Mapped[int] = mapped_column(Integer, default=0)
    activation_grace_days: Mapped[int] = mapped_column(Integer, default=0)
    paid_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_delivery: Mapped[str] = mapped_column(String(10), default="bypass")
    hide_recommendations_for_paid: Mapped[bool] = mapped_column(Boolean, default=False)


class AccessGrant(Base):
    __tablename__ = "access_grants"
    __table_args__ = (
        CheckConstraint("kind IN ('trial', 'paid', 'gift')", name="access_grant_kind"),
        CheckConstraint("ends_at >= starts_at", name="access_grant_interval"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(10))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(100))
    action_key: Mapped[str] = mapped_column(String(100), unique=True)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    __table_args__ = (
        UniqueConstraint("bot_id", "action_key"),
        CheckConstraint("kind IN ('access', 'service', 'topup')", name="payment_kind"),
        CheckConstraint("provider IN ('stars', 'robokassa')", name="payment_provider"),
        CheckConstraint(
            "status IN ('pending', 'paid', 'cancelled', 'review', 'refunded')",
            name="payment_status",
        ),
        CheckConstraint("amount > 0", name="payment_amount"),
        CheckConstraint(
            "(kind = 'access' AND provider = 'stars' AND currency = 'XTR' AND period_days > 0) "
            "OR (kind IN ('service', 'topup') AND provider = 'robokassa' AND currency = 'RUB' "
            "AND period_days = 0)",
            name="payment_route",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))
    provider: Mapped[str] = mapped_column(String(12))
    account: Mapped[str] = mapped_column(String(100))
    test_mode: Mapped[bool] = mapped_column(Boolean)
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    period_days: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    action_key: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WalletAccount(Base):
    __tablename__ = "wallet_accounts"
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    test_mode: Mapped[bool] = mapped_column(Boolean, primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal("0"))


class WalletEntry(Base):
    __tablename__ = "wallet_entries"
    __table_args__ = (
        UniqueConstraint("bot_id", "action_key"),
        CheckConstraint("kind IN ('topup', 'purchase', 'refund')", name="wallet_entry_kind"),
        CheckConstraint("amount <> 0", name="wallet_entry_amount"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    test_mode: Mapped[bool] = mapped_column(Boolean)
    kind: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    action_key: Mapped[str] = mapped_column(String(100))
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("payment_orders.id", ondelete="RESTRICT")
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentOperation(Base):
    __tablename__ = "payment_operations"
    __table_args__ = (
        UniqueConstraint("order_id", "kind"),
        CheckConstraint("kind IN ('refund', 'reconcile')", name="payment_operation_kind"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'submitted', 'done', 'failed', 'uncertain')",
            name="payment_operation_status",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("payment_orders.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12), default="pending")
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    provider_ref: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(300), default="")
    outcome: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PaymentReceipt(Base):
    __tablename__ = "payment_receipts"
    __table_args__ = (UniqueConstraint("provider", "account", "test_mode", "charge_id"),)
    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("payment_orders.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(12))
    account: Mapped[str] = mapped_column(String(100))
    test_mode: Mapped[bool] = mapped_column(Boolean)
    charge_id: Mapped[str] = mapped_column(String(200))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PaymentInbox(Base):
    __tablename__ = "payment_inbox"
    __table_args__ = (
        UniqueConstraint("bot_id", "test_mode", "update_id"),
        CheckConstraint("kind IN ('successful', 'refunded')", name="payment_inbox_kind"),
        CheckConstraint(
            "status IN ('pending', 'processed', 'review')", name="payment_inbox_status"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    test_mode: Mapped[bool] = mapped_column(Boolean)
    update_id: Mapped[int] = mapped_column(BigInteger)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(12))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(60))
    subject_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Recipient(Base):
    __tablename__ = "recipients"
    __table_args__ = (UniqueConstraint("material_id", "telegram_user_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    material_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("materials.id", ondelete="CASCADE"), index=True
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    first_delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["material_id", "owner_user_id"],
            ["materials.id", "materials.owner_user_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(100), unique=True)
    purpose: Mapped[str] = mapped_column(String(40))
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    material_id: Mapped[int] = mapped_column(BigInteger)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    text_version: Mapped[str] = mapped_column(String(40))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class YouTubeCache(Base):
    __tablename__ = "youtube_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ProductEvent(Base):
    __tablename__ = "product_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(60), index=True)
    material_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("materials.id", ondelete="SET NULL")
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ExpertNotificationPreference(Base):
    __tablename__ = "expert_notification_preferences"
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    notices: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    advertising: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    advertising_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    advertising_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_version: Mapped[str | None] = mapped_column(String(40))


class OwnerBroadcast(Base):
    __tablename__ = "owner_broadcasts"
    __table_args__ = (
        CheckConstraint("kind IN ('notice', 'advertising')", name="broadcast_kind"),
        CheckConstraint("status IN ('draft', 'queued', 'cancelled')", name="broadcast_status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(15))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(15), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', 'uncertain', 'cancelled')",
            name="outbox_status",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    material_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("materials.id", ondelete="CASCADE")
    )
    service_request_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("service_requests.id", ondelete="CASCADE", name="fk_outbox_service_request"),
        index=True,
    )
    purpose: Mapped[str] = mapped_column(String(40))
    broadcast_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("owner_broadcasts.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(150), unique=True)
    status: Mapped[str] = mapped_column(String(15), default="pending")
    ready_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(150), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=0)


class ToolResult(Base):
    __tablename__ = "tool_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["projects.id", "projects.owner_user_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    project_id: Mapped[int] = mapped_column(BigInteger)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    tool: Mapped[str] = mapped_column(String(40))
    template_version: Mapped[str] = mapped_column(String(40))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[str] = mapped_column(Text)
    action_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UtmTemplate(Base):
    __tablename__ = "utm_templates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["projects.id", "projects.owner_user_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    project_id: Mapped[int] = mapped_column(BigInteger)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(60))
    platform: Mapped[str] = mapped_column(String(10))
    tags: Mapped[dict[str, str | None]] = mapped_column(JSONB)
    action_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TrackedLink(Base):
    __tablename__ = "tracked_links"
    __table_args__ = (
        UniqueConstraint("id", "owner_user_id"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["projects.id", "projects.owner_user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["material_id", "owner_user_id"],
            ["materials.id", "materials.owner_user_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('expert', 'material', 'invite', 'short', 'deeplink')",
            name="tracked_link_kind",
        ),
        ForeignKeyConstraint(
            ["channel_id", "owner_user_id"],
            ["channels.id", "channels.owner_user_id"],
            ondelete="CASCADE",
            name="fk_tracked_links_channel_owner",
        ),
        CheckConstraint(
            "status IN ('pending', 'creating', 'active', 'failed', 'uncertain', "
            "'revoking', 'revoke_failed', 'revoke_uncertain', 'revoked')",
            name="tracked_link_status",
        ),
        CheckConstraint(
            "kind != 'invite' OR channel_id IS NOT NULL", name="invite_channel_required"
        ),
        CheckConstraint(
            "member_limit IS NULL OR member_limit BETWEEN 1 AND 99999", name="invite_member_limit"
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    project_id: Mapped[int] = mapped_column(BigInteger)
    bot_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(60))
    source: Mapped[str] = mapped_column(String(200))
    campaign: Mapped[str] = mapped_column(String(200))
    destination: Mapped[str] = mapped_column(Text)
    payload: Mapped[str | None] = mapped_column(String(64), unique=True)
    external_url: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(100))
    material_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    member_limit: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    operation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    action_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LinkEvent(Base):
    __tablename__ = "link_events"
    __table_args__ = (CheckConstraint("kind IN ('click', 'start')", name="link_event_kind"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    link_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tracked_links.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(10))
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    event_key: Mapped[str | None] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JoinEvent(Base):
    __tablename__ = "join_events"
    __table_args__ = (UniqueConstraint("bot_id", "update_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    link_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tracked_links.id", ondelete="CASCADE"), index=True
    )
    bot_id: Mapped[int] = mapped_column(BigInteger)
    update_id: Mapped[int] = mapped_column(BigInteger)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (CheckConstraint("revision > 0", name="service_revision_positive"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(80))
    audience: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    duration: Mapped[str] = mapped_column(String(150))
    price_label: Mapped[str] = mapped_column(String(150))
    executor_details: Mapped[str] = mapped_column(String(500))
    conditions: Mapped[str] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ServiceRequest(Base):
    __tablename__ = "service_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["projects.id", "projects.owner_user_id"],
            ondelete="CASCADE",
            name="fk_service_request_project_owner",
        ),
        CheckConstraint("status IN ('new', 'working', 'closed')", name="service_request_status"),
        CheckConstraint("revision > 0", name="service_request_revision_positive"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    project_id: Mapped[int] = mapped_column(BigInteger)
    bot_id: Mapped[int] = mapped_column(BigInteger, index=True)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    contact: Mapped[str] = mapped_column(String(200))
    comment: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(10), default="new")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    action_key: Mapped[str] = mapped_column(String(100), unique=True)
    consent_purpose: Mapped[str] = mapped_column(String(40), default="service_request")
    consent_version: Mapped[str] = mapped_column(String(64))
    consent_text: Mapped[str] = mapped_column(Text)
    consent_granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consent_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminAccount(Base):
    __tablename__ = "admin_accounts"
    __table_args__ = (CheckConstraint("id = 1", name="single_admin_account"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    must_change: Mapped[bool] = mapped_column(Boolean, default=True)


class AdminLoginAttempt(Base):
    __tablename__ = "admin_login_attempts"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signature_hash: Mapped[str | None] = mapped_column(String(64), unique=True)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AdminMutation(Base):
    __tablename__ = "admin_mutations"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(30))
    object_id: Mapped[int] = mapped_column(BigInteger)


class ContentVersion(Base):
    __tablename__ = "content_versions"
    __table_args__ = (UniqueConstraint("entity", "object_key", "number"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity: Mapped[str] = mapped_column(String(30))
    object_key: Mapped[str] = mapped_column(String(100))
    number: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Partner(Base):
    __tablename__ = "partners"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(Text)
    advertiser: Mapped[str] = mapped_column(String(300), default="")
    inn: Mapped[str] = mapped_column(String(12), default="")
    erid: Mapped[str] = mapped_column(String(200), default="")
    marking_text: Mapped[str] = mapped_column(String(500), default="")
    exception_reason: Mapped[str] = mapped_column(String(1000), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
