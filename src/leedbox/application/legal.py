"""Versioned public documents and explicit, purpose-specific consent evidence."""

import re
from html import unescape
from typing import Any

import nh3
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from leedbox.application.settings import setting
from leedbox.application.versions import fingerprint, lock_content, record_version
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import ContentVersion, LegalConsentEvent, Setting, User

DOCUMENTS = {
    "privacy": "Политика обработки персональных данных",
    "personal-data": "Согласие на обработку персональных данных",
    "offer": "Договор-оферта LeedBox",
    "advertising": "Согласие на рекламную рассылку",
}
MAX_BYTES = 100_000
TAGS = {
    "p",
    "br",
    "h2",
    "h3",
    "h4",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "blockquote",
    "ul",
    "ol",
    "li",
    "a",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "td",
    "th",
    "caption",
    "span",
    "sub",
    "sup",
    "pre",
    "code",
}


def attribute(tag: str, attr: str, value: str) -> str | None:
    if attr == "class":
        clean = [
            c
            for c in value.split()
            if re.fullmatch(r"ql-(?:align-(?:center|right|justify)|indent-[1-8])", c)
        ]
        return " ".join(clean) or None
    if attr in {"colspan", "rowspan"}:
        return value if value.isascii() and value.isdigit() and 1 <= int(value) <= 20 else None
    if attr == "href":
        # No relative links to privileged actions, credentials, trackers or data URLs.
        return value if re.match(r"^(?:https?://|mailto:|#[A-Za-z])", value, re.I) else None
    return value


def clean_html(raw: str) -> str:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Документ — до 100 КБ HTML в UTF-8.")
    result = nh3.clean(
        raw,
        tags=TAGS,
        attributes={
            "*": {"class"},
            "a": {"href", "title"},
            "td": {"colspan", "rowspan"},
            "th": {"colspan", "rowspan"},
        },
        clean_content_tags={
            "script",
            "style",
            "iframe",
            "object",
            "svg",
            "math",
            "form",
            "template",
        },
        url_schemes={"http", "https", "mailto"},
        attribute_filter=attribute,
        strip_comments=True,
        link_rel="noopener noreferrer nofollow",
    ).strip()
    if len(result.encode()) > MAX_BYTES:
        raise ValueError("После очистки документ превышает 100 КБ.")
    return result


def plain(html: str) -> str:
    return unescape(nh3.clean(html, tags=set())).strip()


def checked_slug(slug: str) -> str:
    if slug not in DOCUMENTS:
        raise ValueError("Неизвестный документ")
    return slug


def draft(db: Session, slug: str) -> dict[str, Any]:
    checked_slug(slug)
    row = db.get(Setting, "legal_draft:" + slug)
    return dict(row.value) if row else {"title": DOCUMENTS[slug], "html": ""}


def version(db: Session, slug: str, number: int) -> ContentVersion | None:
    checked_slug(slug)
    return db.scalar(
        select(ContentVersion).where(
            ContentVersion.entity == "legal_publication",
            ContentVersion.object_key == slug,
            ContentVersion.number == number,
        )
    )


def current(db: Session, slug: str) -> ContentVersion | None:
    checked_slug(slug)
    row = db.get(Setting, "legal_live:" + slug)
    doc = db.get(ContentVersion, row.value.get("id")) if row else None
    return doc if doc and doc.entity == "legal_publication" and doc.object_key == slug else None


def editor_stamp(db: Session, slug: str) -> str:
    live = current(db, slug)
    return fingerprint([draft(db, slug), live.id if live else None])


def save(
    db: Session,
    config: Settings,
    actor: int,
    slug: str,
    title: str,
    html: str,
    expected: str,
    publish: bool = False,
) -> ContentVersion | None:
    if not config.is_owner(actor):
        raise PermissionError("Owner access required")
    checked_slug(slug)
    lock_content(db, "legal", slug)
    if editor_stamp(db, slug) != expected:
        raise ValueError(
            "Документ уже изменён. Откройте новую вкладку и сравните версии; "
            "ваш текст ниже сохранён."
        )
    title = title.strip()
    if not 3 <= len(title) <= 160:
        raise ValueError("Название документа: 3–160 символов.")
    value = {"title": title, "html": clean_html(html)}
    if publish and len(plain(value["html"])) < 20:
        raise ValueError("Нельзя публиковать пустой документ. Добавьте полный текст.")
    db.execute(
        insert(Setting)
        .values(key="legal_draft:" + slug, value=value)
        .on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": value},
        )
    )
    if not publish:
        return None
    live = current(db, slug)
    if not live or live.data != value:
        record_version(db, "legal_publication", slug, actor, value)
        live = db.scalar(
            select(ContentVersion)
            .where(
                ContentVersion.entity == "legal_publication",
                ContentVersion.object_key == slug,
            )
            .order_by(ContentVersion.number.desc())
            .limit(1)
        )
        assert live is not None
        db.execute(
            insert(Setting)
            .values(key="legal_live:" + slug, value={"id": live.id})
            .on_conflict_do_update(
                index_elements=[Setting.key],
                set_={"value": {"id": live.id}},
            )
        )
    return live


def public_url(config: Settings, slug: str | None = None, number: int | None = None) -> str:
    base = (config.public_base_url or f"http://localhost:{config.web_port}").rstrip("/")
    path = "/legal" + ("/" + checked_slug(slug) if slug else "")
    if number is not None:
        path += f"/v/{number}"
    return base + path


def require_ready(db: Session, config: Settings) -> None:
    if not current(db, "privacy") or not current(db, "personal-data"):
        raise ValueError("Сначала опубликуйте политику и отдельное согласие на обработку данных.")
    if config.environment == "production" and not config.public_base_url:
        raise ValueError("Сначала настройте публичный HTTPS-адрес сайта.")


def last_event(
    db: Session, actor: int, bot: int, purpose: str = "personal_data"
) -> LegalConsentEvent | None:
    return db.scalar(
        select(LegalConsentEvent)
        .where(
            LegalConsentEvent.telegram_user_id == actor,
            LegalConsentEvent.bot_id == bot,
            LegalConsentEvent.purpose == purpose,
        )
        .order_by(LegalConsentEvent.id.desc())
        .limit(1)
    )


def accepted(db: Session, actor: int, bot: int) -> bool:
    doc = current(db, "personal-data")
    last = last_event(db, actor, bot)
    return bool(doc and last and last.action == "grant" and last.version_id == doc.id)


def needed(db: Session, config: Settings, actor: int, bot: int) -> bool:
    return (
        bool(setting(db, "legal_gate_enabled"))
        and not config.is_owner(actor)
        and not accepted(db, actor, bot)
    )


def require_expert(db: Session, config: Settings, owner: int, bot: int) -> None:
    if not setting(db, "legal_gate_enabled"):
        return
    user = db.get(User, owner)
    if not user or needed(db, config, user.telegram_id, bot):
        raise ValueError(
            "Подтвердите актуальное согласие: /start или /documents. Запрос не запущен."
        )


def grant(
    db: Session,
    config: Settings,
    actor: int,
    bot: int,
    document: int,
    policy: int,
    action_key: str,
    message_id: int,
) -> None:
    lock_content(db, "legal_consent", f"{bot}:{actor}")
    require_ready(db, config)
    doc, privacy = current(db, "personal-data"), current(db, "privacy")
    if not doc or not privacy or (doc.id, privacy.id) != (document, policy):
        raise ValueError("Документы обновились. Откройте /start и прочитайте актуальную версию.")
    db.execute(
        insert(LegalConsentEvent)
        .values(
            bot_id=bot,
            telegram_user_id=actor,
            purpose="personal_data",
            action="grant",
            version_id=document,
            policy_version_id=policy,
            action_key=action_key,
            message_id=message_id,
        )
        .on_conflict_do_nothing()
    )


def revoke(db: Session, actor: int, bot: int, action_key: str, message_id: int) -> None:
    lock_content(db, "legal_consent", f"{bot}:{actor}")
    last = last_event(db, actor, bot)
    if last and last.action == "grant":
        db.execute(
            insert(LegalConsentEvent)
            .values(
                bot_id=bot,
                telegram_user_id=actor,
                purpose="personal_data",
                action="revoke",
                version_id=last.version_id,
                policy_version_id=last.policy_version_id,
                action_key=action_key,
                message_id=message_id,
            )
            .on_conflict_do_nothing()
        )
