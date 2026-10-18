"""Owner credentials: no passwords or hashes in audit/analytics."""

import re

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from leedbox.application.versions import lock_content
from leedbox.infrastructure.config import Settings
from leedbox.infrastructure.models import AdminAccount, AdminSession, AuditLog

HASH_METHOD = "scrypt:131072:8:1"


def username(value: str) -> str:
    clean = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", clean):
        raise ValueError("Логин: 3–64 латинских буквы, цифры, точка, дефис или подчёркивание")
    return clean


def password(value: str) -> str:
    if not 15 <= len(value) <= 128 or "\x00" in value:
        raise ValueError("Пароль: от 15 до 128 символов; можно использовать пробелы и Unicode")
    return value


def bootstrap(
    db: Session, config: Settings, actor: int, login: str, secret: str, *, must_change: bool = True
) -> AdminAccount:
    if not config.is_owner(actor):
        raise ValueError("Учётная запись должна быть связана с ID владельца из конфигурации")
    lock_content(db, "admin_credentials", "owner")
    if db.get(AdminAccount, 1) is not None:
        raise ValueError("Учётная запись уже создана; используйте смену или восстановление")
    row = AdminAccount(
        id=1,
        username=username(login),
        password_hash=generate_password_hash(password(secret), method=HASH_METHOD),
        telegram_id=actor,
        revision=1,
        must_change=must_change,
    )
    db.add(row)
    db.execute(delete(AdminSession))
    db.add(AuditLog(actor_telegram_id=actor, action="admin_credentials_initialized", details={}))
    db.flush()
    return row


def reset(db: Session, config: Settings, actor: int, login: str, secret: str) -> None:
    """Explicit server-console recovery, never exposed as an unauthenticated route."""
    if not config.is_owner(actor):
        raise ValueError("ID владельца не разрешён конфигурацией")
    lock_content(db, "admin_credentials", "owner")
    row = db.scalar(select(AdminAccount).where(AdminAccount.id == 1).with_for_update())
    if row is None:
        raise ValueError("Сначала создайте учётную запись")
    row.username = username(login)
    row.password_hash = generate_password_hash(password(secret), method=HASH_METHOD)
    row.telegram_id = actor
    row.revision += 1
    row.must_change = True
    db.execute(delete(AdminSession))
    db.add(AuditLog(actor_telegram_id=actor, action="admin_credentials_reset", details={}))


def change(
    db: Session,
    actor: int,
    expected: int,
    current_password: str,
    login: str,
    new_password: str,
    confirmation: str,
) -> None:
    row = db.scalar(select(AdminAccount).where(AdminAccount.id == 1).with_for_update())
    if row is None or row.telegram_id != actor or row.revision != expected:
        raise ValueError("Учётные данные изменились. Войдите заново")
    if not 1 <= len(current_password) <= 128 or not check_password_hash(
        row.password_hash, current_password
    ):
        raise ValueError("Текущий пароль неверен")
    next_login = username(login)
    if new_password != confirmation:
        raise ValueError("Новый пароль и подтверждение не совпадают")
    password(new_password)
    if check_password_hash(row.password_hash, new_password):
        raise ValueError("Новый пароль должен отличаться от текущего")
    row.username = next_login
    row.password_hash = generate_password_hash(new_password, method=HASH_METHOD)
    row.revision += 1
    row.must_change = False
    db.execute(delete(AdminSession))
    db.add(AuditLog(actor_telegram_id=actor, action="admin_credentials_changed", details={}))
